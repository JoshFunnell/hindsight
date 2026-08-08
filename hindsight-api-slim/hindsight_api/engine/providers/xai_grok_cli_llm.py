"""
SuperGrok subscription LLM provider (``xai-grok-cli``).

Serves Hindsight's LLM lanes from a consumer SuperGrok subscription through the
Grok CLI's session credential — no per-token API key — joining the engine's
existing subscription-provider category (``claude-code``, ``openai-codex``,
``nous``).

Provenance, stated plainly
--------------------------
``https://cli-chat-proxy.grok.com/v1`` is the backend the vendor's own Grok CLI
talks to, and the auth recipe is documented *inside* that CLI ("Using auth.json
for API Access", with a curl example that omits two of the required client
headers — which is why an unmodified copy of it earns an HTTP 426). It is NOT a
separately published, stability-guaranteed API product, and rate-limit or
permitted-use terms are not published for this path. Like the ``claude-code``
provider, this is intended for local, personal use.

Why not ``OpenAICompatibleLLM``
-------------------------------
The endpoint is OpenAI-shaped on the wire but needs five custom headers per
call, routes on a header rather than the body's ``model`` field, enforces a
client-version floor, and authenticates with a rotating session token whose
refresh is a CLI spawn. None of that fits the OpenAI SDK client the compatible
provider is built on, so the transport is a hand-written ``httpx.AsyncClient``
in the ``codex_llm`` mould — protocol flow only; every multi-value internal
return here is a dataclass or Pydantic model, never a tuple or a bare dict.

Logging
-------
Bodies, credentials, and header values are never logged: only byte counts, the
model name, status codes, and durations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field

from hindsight_api.config import DEFAULT_LLM_TIMEOUT, ENV_LLM_TIMEOUT
from hindsight_api.engine.llm_interface import LLM_TOOL_CHOICE_AUTO, LLMInterface, LLMToolChoice, LLMToolChoiceMode
from hindsight_api.engine.llm_trace import LLMResponseUsage, current_trace_context, stash_response_usage
from hindsight_api.engine.providers.xai_grok_cli_auth import (
    DEFAULT_REFRESH_SKEW_SECONDS,
    SessionToken,
    XaiGrokCliAuthError,
    XaiGrokCliAuthManager,
    XaiGrokCliVersionError,
    resolve_cli_binary,
    resolve_client_version,
)
from hindsight_api.engine.response_models import LLMToolCall, LLMToolCallResult, TokenUsage
from hindsight_api.engine.structured_output import strict_json_schema
from hindsight_api.metrics import get_metrics_collector
from hindsight_api.worker.stage import set_stage

logger = logging.getLogger(__name__)

__all__ = ["XaiGrokCliLLM", "XaiGrokCliAuthError", "XaiGrokCliVersionError"]

#: Override for the upstream base URL. Provider-specific, so it wins over the
#: generic ``HINDSIGHT_API_LLM_BASE_URL`` that deployments often set for an
#: unrelated proxy — same "more specific beats more general" rule the rest of
#: Hindsight's config hierarchy follows.
ENV_BASE_URL = "HINDSIGHT_API_XAI_GROK_CLI_BASE_URL"

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"

#: Literal auth-mode selector the endpoint's middleware expects. Not a secret:
#: it tells the server "validate this as a CLI session token", and it is
#: published in the Grok CLI's own embedded documentation.
TOKEN_AUTH_MODE = "xai-grok-cli"
CLIENT_IDENTIFIER = "grok-shell"


class _PromptTokensDetails(BaseModel):
    cached_tokens: int = 0


class _CompletionTokensDetails(BaseModel):
    reasoning_tokens: int = 0


class _ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: _PromptTokensDetails | None = None
    completion_tokens_details: _CompletionTokensDetails | None = None


class _ChatToolCallFunction(BaseModel):
    name: str = ""
    arguments: str = ""


class _ChatToolCall(BaseModel):
    id: str = ""
    function: _ChatToolCallFunction = Field(default_factory=_ChatToolCallFunction)


class _ChatMessage(BaseModel):
    content: str | None = None
    tool_calls: list[_ChatToolCall] | None = None


class _ChatChoice(BaseModel):
    message: _ChatMessage | None = None
    finish_reason: str | None = None


class _ChatCompletion(BaseModel):
    """The upstream's OpenAI-shaped completion response.

    Parsed at the boundary so the rest of the provider works on typed
    attributes instead of ``dict.get`` chains. Unknown fields are ignored
    (Pydantic's default), so upstream additions do not break the lane.
    """

    choices: list[_ChatChoice] = Field(default_factory=list)
    usage: _ChatUsage | None = None


@dataclass(frozen=True, slots=True)
class _TokenCounts:
    """Normalized token counts for one response.

    ``output_tokens`` is visible-only: OpenAI-shaped providers fold reasoning
    tokens into ``completion_tokens``, but Hindsight's ``TokenUsage`` contract
    surfaces reasoning separately, so they are subtracted out here to stop the
    two fields double-counting the same billed tokens.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    thoughts_tokens: int


@dataclass(frozen=True, slots=True)
class _UpstreamReply:
    """One raw HTTP reply from the upstream. Body text is never logged."""

    status_code: int
    body_text: str


@dataclass(frozen=True, slots=True)
class _CompletionContent:
    """Message text plus the finish reason that came with it."""

    text: str
    finish_reason: str | None


class _UpstreamStatusError(RuntimeError):
    """An upstream reply the caller cannot use, and whether retrying may help.

    Covers both non-2xx statuses and success responses whose shape is unusable
    (no choices, empty content) — from a retry loop's point of view those are
    the same decision, and the message carries the distinction.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _token_counts(usage: _ChatUsage | None) -> _TokenCounts:
    """Build normalized counts from the upstream usage block.

    A streamed-shape response can omit ``prompt_tokens_details`` entirely, in
    which case cached tokens read 0 — a known instrument gap, not an error.
    """
    if usage is None:
        return _TokenCounts(input_tokens=0, output_tokens=0, total_tokens=0, cached_tokens=0, thoughts_tokens=0)

    cached = usage.prompt_tokens_details.cached_tokens if usage.prompt_tokens_details else 0
    thoughts = usage.completion_tokens_details.reasoning_tokens if usage.completion_tokens_details else 0
    output = max(0, usage.completion_tokens - thoughts) if thoughts else usage.completion_tokens
    total = max(0, usage.total_tokens - thoughts) if thoughts else usage.total_tokens
    return _TokenCounts(
        input_tokens=usage.prompt_tokens,
        output_tokens=output,
        total_tokens=total,
        cached_tokens=cached,
        thoughts_tokens=thoughts,
    )


def _strip_code_fence(content: str) -> str:
    """Unwrap a markdown-fenced JSON payload, if the model produced one."""
    if "```json" in content:
        return content.split("```json")[1].split("```")[0].strip()
    if "```" in content:
        return content.split("```")[1].split("```")[0].strip()
    return content


def _conversation_affinity_id(messages: list[dict[str, Any]]) -> str | None:
    """Stable ``x-grok-conv-id`` so the upstream's prompt cache can hit.

    xAI stores prompt-cache entries per backend server and routes requests
    carrying the same ``x-grok-conv-id`` to the same server. Without the header,
    each call of a multi-turn agentic loop can land on a cache-cold replica —
    measured on a reflect lane as a 0.08% cache-hit rate across ~27.6M input
    tokens/day, while an exact byte-repeat probe through the same endpoint hit
    98.6%. The missing piece was only the pinning.

    The id comes from the operation's trace id when there is one (every LLM call
    of a single retain/reflect/consolidation run shares it, which is exactly the
    grouping the cache wants), and otherwise from a hash of the first message —
    the system prompt, byte-identical across the calls of one loop. It is hashed
    rather than sent raw so no internal identifier leaves the process.

    Fail-open: any parse problem returns None and the request goes upstream
    without the header, exactly as before.
    """
    context = current_trace_context()
    if context is not None and context.trace_id:
        return hashlib.sha256(context.trace_id.encode("utf-8")).hexdigest()[:32]
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return None
    try:
        first = json.dumps(messages[0], sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(first.encode("utf-8")).hexdigest()[:32]


class XaiGrokCliLLM(LLMInterface):
    """LLM provider backed by a SuperGrok subscription via the Grok CLI session token."""

    def __init__(
        self,
        provider: str,
        api_key: str,  # Ignored: the credential is read from ~/.grok/auth.json
        base_url: str,
        model: str,
        reasoning_effort: str = "low",
        timeout: float | None = None,
        **kwargs: Any,
    ):
        """Initialize the provider and resolve the client-version header.

        The version is resolved eagerly, at construction: the endpoint answers
        HTTP 426 for a request without ``x-grok-client-version``, so a
        deployment that cannot produce one is misconfigured and should say so at
        startup rather than on the first memory operation.
        """
        super().__init__(provider, api_key, base_url, model, reasoning_effort, **kwargs)

        self.base_url = (os.environ.get(ENV_BASE_URL, "").strip() or self.base_url or DEFAULT_BASE_URL).rstrip("/")

        # Honour the engine-resolved per-operation timeout; fall back to the
        # same global default the OpenAI-compatible providers use.
        self.timeout = timeout if timeout is not None else float(os.getenv(ENV_LLM_TIMEOUT, str(DEFAULT_LLM_TIMEOUT)))

        cli_path = resolve_cli_binary()
        self._client_version = resolve_client_version(cli_path=cli_path)
        self._auth = XaiGrokCliAuthManager(cli_path=cli_path)
        self._client = httpx.AsyncClient(timeout=self.timeout)

        logger.info(
            "xai-grok-cli provider initialized: model=%s base_url=%s cli=%s",
            self.model,
            self.base_url,
            "present" if cli_path is not None else "absent (refresh unavailable)",
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _admission_ttl(self) -> float:
        """Minimum token life required to admit a request.

        Both bars apply: refresh EARLY (the skew, so a long batch cannot
        straddle expiry) and never admit a request the token cannot outlive (the
        request timeout). Passing the timeout alone silently defeats the skew —
        measured on the donor proxy as zero refreshes with hours of TTL left,
        because "outlives one request" was the only bar on the request path.
        """
        return max(DEFAULT_REFRESH_SKEW_SECONDS, self.timeout)

    async def _post(self, body: dict[str, Any], token: SessionToken, conv_id: str | None) -> _UpstreamReply:
        """POST one chat completion. Header values never reach the log."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token.key}",
            "X-XAI-Token-Auth": TOKEN_AUTH_MODE,
            "x-grok-client-version": self._client_version.text,
            "x-grok-client-identifier": CLIENT_IDENTIFIER,
        }
        if self.model:
            # The endpoint routes on this header, NOT on the body's "model" field.
            headers["x-grok-model-override"] = self.model
        if conv_id:
            headers["x-grok-conv-id"] = conv_id

        response = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        return _UpstreamReply(status_code=response.status_code, body_text=response.text)

    async def _request_completion(self, body: dict[str, Any], messages: list[dict[str, Any]]) -> _ChatCompletion:
        """One logical upstream call: admission, headers, 401 recovery, 426 branch.

        The 401 retry lives here rather than in the callers' retry loops because
        it is auth recovery, not backoff: it happens exactly once and does not
        consume a normal-retry budget slot. A second 401 is terminal — the
        credential is genuinely rejected and looping cannot fix it.
        """
        token = await self._auth.get_token(self._admission_ttl())
        conv_id = _conversation_affinity_id(messages)

        reply = await self._post(body, token, conv_id)
        if reply.status_code == 401:
            logger.warning("xai-grok-cli upstream rejected the session token (401); refreshing once and retrying")
            token = await self._auth.force_warm(token)
            reply = await self._post(body, token, conv_id)
            if reply.status_code == 401:
                raise XaiGrokCliAuthError(
                    "the SuperGrok upstream rejected the session token even after a refresh. "
                    "Run `grok login` on the host that owns ~/.grok/auth.json, then retry."
                )

        if reply.status_code == 426:
            # The local floor is a snapshot of a server-enforced value, so a
            # request can pass construction-time validation and still be
            # refused at the wire once the server raises it. Terminal and never
            # retried: only a CLI update (or a new pinned version) clears it.
            raise XaiGrokCliVersionError(
                f"the SuperGrok upstream refused client version {self._client_version.text} with HTTP 426 "
                "(client too old). Run `grok update` on the host, then restart Hindsight."
            )

        if reply.status_code >= 400:
            # Retry 408/429 and 5xx; other 4xx are request-shaped problems that
            # will fail identically on every attempt.
            retryable = reply.status_code in (408, 429) or reply.status_code >= 500
            raise _UpstreamStatusError(
                f"SuperGrok upstream returned HTTP {reply.status_code} ({len(reply.body_text)} bytes)",
                retryable=retryable,
            )

        try:
            return _ChatCompletion.model_validate_json(reply.body_text)
        except ValueError as exc:
            raise _UpstreamStatusError(
                f"SuperGrok upstream returned an unparseable success body ({len(reply.body_text)} bytes)",
                retryable=True,
            ) from exc

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        """Assemble the OpenAI-shaped request body common to both call paths."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            # BEHAVIOUR CHANGE, deliberate: the pre-existing route to this
            # endpoint was provider=openai through a local proxy, and
            # OpenAICompatibleLLM only sends reasoning_effort when
            # _supports_reasoning_model() matches — a model-name heuristic
            # (gpt-5/o1/o3/deepseek) that "grok-4.5" does not match. The
            # member-configured effort therefore never reached xAI on that path.
            # Sending it here is the first time that setting takes effect on
            # this lane.
            "reasoning_effort": self.reasoning_effort,
        }
        if max_completion_tokens is not None:
            # ``max_tokens``, not ``max_completion_tokens``: the field that has
            # demonstrably worked against this endpoint is the one the existing
            # provider=openai + custom-base_url lane sends, because
            # _max_tokens_param_name() resolves to ``max_tokens`` for a custom
            # base URL on a non-reasoning model. The newer name is untested here.
            body["max_tokens"] = max_completion_tokens
        if temperature is not None:
            body["temperature"] = temperature
        return body

    # ------------------------------------------------------------------
    # LLMInterface
    # ------------------------------------------------------------------

    async def verify_connection(self) -> None:
        """Verify the lane with one tiny completion."""
        try:
            logger.info("Verifying xai-grok-cli: model=%s", self.model)
            await self.call(
                messages=[{"role": "user", "content": "Say 'ok'"}],
                max_completion_tokens=16,
                max_retries=2,
                initial_backoff=0.5,
                max_backoff=2.0,
                scope="verification",
            )
            logger.info("xai-grok-cli verified: %s", self.model)
        except Exception as e:
            # An exhausted subscription is not a misconfiguration — the lane is
            # wired correctly and will serve again when the pool refills, so it
            # must not block startup (same allowance the Codex provider makes).
            if "429" in str(e):
                logger.warning("xai-grok-cli quota exhausted for %s, continuing startup: %s", self.model, e)
                return
            raise RuntimeError(f"xai-grok-cli connection verification failed for {self.model}: {e}") from e

    async def call(
        self,
        messages: list[dict[str, str]],
        response_format: Any | None = None,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
        scope: str = "memory",
        max_retries: int = 10,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        skip_validation: bool = False,
        strict_schema: bool = False,
        return_usage: bool = False,
        attempt_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> Any:
        """Make a non-streaming completion call with retry logic.

        Non-streaming is deliberate for the first implementation: the engine
        buffers every response anyway, and the endpoint serves grok-4.5
        non-streamed despite the vendor doc's "most models only support
        streaming" (measured). A streaming path would additionally lose
        ``prompt_tokens_details``, so cached-token accounting would read low.
        """
        start_time = time.time()
        body = self._build_body(list(messages), max_completion_tokens, temperature)

        if response_format is not None and hasattr(response_format, "model_json_schema"):
            schema = strict_json_schema(response_format) if strict_schema else response_format.model_json_schema()
            if strict_schema:
                # Grammar-enforced: measured against this endpoint returning
                # exactly-conforming JSON for a strict json_schema request.
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "strict": True, "schema": schema},
                }
            else:
                schema_msg = (
                    "\n\nYou must respond with valid JSON matching this schema:\n"
                    f"{json.dumps(schema, indent=2, ensure_ascii=False)}"
                )
                first = body["messages"][0] if body["messages"] else None
                if isinstance(first, dict) and isinstance(first.get("content"), str):
                    first["content"] = f"{first['content']}{schema_msg}"
                else:
                    body["messages"].append({"role": "system", "content": schema_msg.strip()})
                body["response_format"] = {"type": "json_object"}

        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with attempt_context() if attempt_context is not None else nullcontext():
                    set_stage(f"llm.xai_grok_cli.{scope}.attempt={attempt + 1}/{max_retries + 1}")
                    completion = await self._request_completion(body, body["messages"])

                counts = _token_counts(completion.usage)
                # Stash before parse/validate, which can still fail locally even
                # though the provider charged for these tokens.
                stash_response_usage(
                    LLMResponseUsage(
                        input_tokens=counts.input_tokens,
                        output_tokens=counts.output_tokens,
                        cached_tokens=counts.cached_tokens,
                    )
                )

                completion_content = self._content_of(completion, scope)
                content = completion_content.text

                if response_format is not None:
                    try:
                        json_data = json.loads(_strip_code_fence(content))
                    except json.JSONDecodeError as json_err:
                        logger.warning(
                            "xai-grok-cli JSON parse error (attempt %d/%d, scope=%s, %d chars): %s",
                            attempt + 1,
                            max_retries + 1,
                            scope,
                            len(content),
                            json_err,
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(min(initial_backoff * (2**attempt), max_backoff))
                            last_exception = json_err
                            continue
                        raise
                    result = json_data if skip_validation else response_format.model_validate(json_data)
                else:
                    result = content

                duration = time.time() - start_time
                self._record_success(scope=scope, duration=duration, counts=counts)
                self._record_span(
                    scope=scope,
                    messages=body["messages"],
                    response_content=result,
                    counts=counts,
                    duration=duration,
                    finish_reason=completion_content.finish_reason,
                )

                if return_usage:
                    return result, TokenUsage(
                        input_tokens=counts.input_tokens,
                        output_tokens=counts.output_tokens,
                        total_tokens=counts.total_tokens,
                        cached_tokens=counts.cached_tokens,
                        thoughts_tokens=counts.thoughts_tokens,
                    )
                return result

            except (_UpstreamStatusError, httpx.RequestError) as e:
                last_exception = e
                retryable = e.retryable if isinstance(e, _UpstreamStatusError) else True
                if retryable and attempt < max_retries:
                    logger.warning(
                        "xai-grok-cli call error (attempt %d/%d, scope=%s): %s",
                        attempt + 1,
                        max_retries + 1,
                        scope,
                        e,
                    )
                    await asyncio.sleep(min(initial_backoff * (2**attempt), max_backoff))
                    continue
                logger.error("xai-grok-cli call failed (scope=%s, attempt %d): %s", scope, attempt + 1, e)
                raise

        if last_exception:
            raise last_exception
        raise RuntimeError("xai-grok-cli call failed after all retries with no exception captured")

    async def call_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
        scope: str = "tools",
        max_retries: int = 5,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        tool_choice: LLMToolChoice = LLM_TOOL_CHOICE_AUTO,
        attempt_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> LLMToolCallResult:
        """Make a non-streaming tool-calling completion.

        One round of the agentic loop the caller drives: tool calls are returned
        as proposed, never executed here.
        """
        start_time = time.time()
        body = self._build_body(list(messages), max_completion_tokens, temperature)

        if tool_choice.mode is LLMToolChoiceMode.NAMED:
            forced_name = tool_choice.selected_function_name
            filtered = [tool for tool in tools if tool.get("function", {}).get("name") == forced_name]
            if len(filtered) != 1:
                raise ValueError(
                    f"Named tool_choice must reference exactly one declared tool; "
                    f"found {len(filtered)} definitions for {forced_name!r}"
                )
            body["tools"] = filtered
            body["tool_choice"] = LLMToolChoiceMode.REQUIRED.value
        else:
            body["tools"] = tools
            if tool_choice.mode is not LLMToolChoiceMode.AUTO:
                body["tool_choice"] = tool_choice.mode.value

        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with attempt_context() if attempt_context is not None else nullcontext():
                    set_stage(f"llm.xai_grok_cli.tools.attempt={attempt + 1}/{max_retries + 1}")
                    completion = await self._request_completion(body, body["messages"])

                counts = _token_counts(completion.usage)
                choice = completion.choices[0] if completion.choices else None
                message = choice.message if choice is not None else None
                content = message.content if message is not None else None
                tool_calls: list[LLMToolCall] = []
                for raw_call in (message.tool_calls or []) if message is not None else []:
                    try:
                        arguments = json.loads(raw_call.function.arguments) if raw_call.function.arguments else {}
                    except json.JSONDecodeError:
                        # Keep the raw payload so the caller can see what the
                        # model emitted rather than a silently empty tool call.
                        arguments = {"_raw": raw_call.function.arguments}
                    tool_calls.append(LLMToolCall(id=raw_call.id, name=raw_call.function.name, arguments=arguments))

                duration = time.time() - start_time
                finish_reason = choice.finish_reason if choice is not None else None
                self._record_success(scope=scope, duration=duration, counts=counts)
                self._record_span(
                    scope=scope,
                    messages=body["messages"],
                    response_content=content,
                    counts=counts,
                    duration=duration,
                    finish_reason=finish_reason,
                    tool_calls=tool_calls,
                )

                return LLMToolCallResult(
                    content=content,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    input_tokens=counts.input_tokens,
                    output_tokens=counts.output_tokens,
                    cached_tokens=counts.cached_tokens,
                    thoughts_tokens=counts.thoughts_tokens,
                )

            except (_UpstreamStatusError, httpx.RequestError) as e:
                last_exception = e
                retryable = e.retryable if isinstance(e, _UpstreamStatusError) else True
                if retryable and attempt < max_retries:
                    logger.warning(
                        "xai-grok-cli tool call error (attempt %d/%d, scope=%s): %s",
                        attempt + 1,
                        max_retries + 1,
                        scope,
                        e,
                    )
                    await asyncio.sleep(min(initial_backoff * (2**attempt), max_backoff))
                    continue
                logger.error("xai-grok-cli tool call failed (scope=%s, attempt %d): %s", scope, attempt + 1, e)
                raise

        if last_exception:
            raise last_exception
        raise RuntimeError("xai-grok-cli tool call failed after all retries with no exception captured")

    async def cleanup(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def supports_attempt_scoped_concurrency(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _content_of(self, completion: _ChatCompletion, scope: str) -> _CompletionContent:
        """Extract message content, turning shape problems into retryable errors."""
        if not completion.choices:
            raise _UpstreamStatusError(
                f"SuperGrok upstream returned no choices (model={self.model}, scope={scope})",
                retryable=True,
            )
        choice = completion.choices[0]
        content = choice.message.content if choice.message is not None else None
        if not content:
            raise _UpstreamStatusError(
                f"SuperGrok upstream returned empty message content (model={self.model}, scope={scope}, "
                f"finish_reason={choice.finish_reason})",
                retryable=True,
            )
        return _CompletionContent(text=content, finish_reason=choice.finish_reason)

    def _record_success(self, *, scope: str, duration: float, counts: _TokenCounts) -> None:
        get_metrics_collector().record_llm_call(
            provider=self.provider,
            model=self.model,
            scope=scope,
            duration=duration,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            success=True,
            cached_input_tokens=counts.cached_tokens,
            thoughts_tokens=counts.thoughts_tokens,
        )
        if duration > 10.0:
            logger.info(
                "slow llm call: scope=%s, model=%s/%s, input_tokens=%d, output_tokens=%d, cached_tokens=%d, time=%.3fs",
                scope,
                self.provider,
                self.model,
                counts.input_tokens,
                counts.output_tokens,
                counts.cached_tokens,
                duration,
            )

    def _record_span(
        self,
        *,
        scope: str,
        messages: list[dict[str, Any]],
        response_content: Any,
        counts: _TokenCounts,
        duration: float,
        finish_reason: str | None,
        tool_calls: list[LLMToolCall] | None = None,
    ) -> None:
        """Record the GenAI span. Best-effort, but never silently swallowed."""
        try:
            from hindsight_api.tracing import _serialize_for_span, get_span_recorder

            get_span_recorder().record_llm_call(
                provider=self.provider,
                model=self.model,
                scope=scope,
                messages=messages,
                response_content=_serialize_for_span(response_content),
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                duration=duration,
                finish_reason=finish_reason,
                error=None,
                cached_tokens=counts.cached_tokens,
                tool_calls=(
                    [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]
                    if tool_calls
                    else None
                ),
            )
        except Exception as span_error:
            # Tracing stays best-effort, but instrumentation bugs that would
            # otherwise silently erase spans have to be visible.
            logger.debug("xai-grok-cli span recording failed: %s", span_error, exc_info=True)
