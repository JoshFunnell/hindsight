"""Tests for the ``xai-grok-cli`` SuperGrok subscription provider.

No test here touches the network, the operator's real ``~/.grok/auth.json``, or
spawns a real CLI: the credential file is written under ``tmp_path``, the HTTP
client is a hand-rolled fake (house style — the repo uses no respx/vcr), and the
CLI spawn is an injected callable whose recorded ``CliSpawnSpec`` is asserted on.

Credential values are asserted by identity against the fixture string; no test
prints or logs one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from hindsight_api.config import PROVIDER_DEFAULT_MODELS, _get_default_model_for_provider
from hindsight_api.engine.llm_wrapper import create_llm_provider, requires_api_key
from hindsight_api.engine.providers import xai_grok_cli_auth as auth_mod
from hindsight_api.engine.providers.xai_grok_cli_auth import (
    ENV_AUTH_FILE,
    ENV_CLI_BIN,
    ENV_CLIENT_VERSION,
    MIN_CLIENT_VERSION,
    CliSpawnResult,
    CliSpawnSpec,
    SessionToken,
    XaiGrokCliAuthError,
    XaiGrokCliAuthManager,
    XaiGrokCliVersionError,
    _parse_expiry,
    default_auth_file,
    no_window_creationflags,
    parse_client_version,
    read_session_token,
    resolve_cli_binary,
    resolve_client_version,
    select_entry,
)
from hindsight_api.engine.providers.xai_grok_cli_llm import (
    CLIENT_IDENTIFIER,
    TOKEN_AUTH_MODE,
    XaiGrokCliLLM,
    _ChatUsage,
    _token_counts,
)

TEST_KEY = "test-session-key-do-not-log"
TEST_VERSION = "0.1.250"
FAKE_CLI = Path("/nonexistent/grok-cli-for-tests")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeResponse:
    status_code: int
    text: str


class _FakeHttp:
    """Stand-in for ``httpx.AsyncClient`` recording every request it is given."""

    def __init__(self, replies: list[_FakeResponse]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        return self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]

    async def aclose(self) -> None:
        pass


class _FakeSpawn:
    """Injectable CLI spawn: records specs, optionally refreshes the auth file."""

    def __init__(self, auth_file: Path | None = None, *, refresh_to: float | None = None, delay: float = 0.0) -> None:
        self.specs: list[CliSpawnSpec] = []
        self._auth_file = auth_file
        self._refresh_to = refresh_to
        self._delay = delay
        self.stdout = f"grok {TEST_VERSION}"

    def __call__(self, spec: CliSpawnSpec) -> CliSpawnResult:
        self.specs.append(spec)
        if self._delay:
            time.sleep(self._delay)
        if self._auth_file is not None and self._refresh_to is not None:
            _write_auth(self._auth_file, expires_in=self._refresh_to)
        return CliSpawnResult(returncode=0, stdout=self.stdout, stderr="")

    @property
    def count(self) -> int:
        return len(self.specs)


def _never_spawn(spec: CliSpawnSpec) -> CliSpawnResult:
    raise AssertionError(f"the CLI must not be spawned, got argv={spec.argv}")


def _iso(offset_seconds: float) -> str:
    """RFC3339 with the vendor's nine fractional digits."""
    moment = time.gmtime(time.time() + offset_seconds)
    return time.strftime("%Y-%m-%dT%H:%M:%S", moment) + ".764688200Z"


def _write_auth(path: Path, *, expires_in: float = 6 * 3600, key: str = TEST_KEY) -> Path:
    path.write_text(
        json.dumps({f"https://auth.x.ai::{key[:6]}": {"key": key, "expires_at": _iso(expires_in)}}),
        encoding="utf-8",
    )
    return path


def _make_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expires_in: float = 6 * 3600,
    replies: list[_FakeResponse] | None = None,
    cli_path: Path | None = FAKE_CLI,
    spawn: Any = _never_spawn,
    timeout: float | None = 120.0,
) -> XaiGrokCliLLM:
    """Build a provider wired to a tmp credential file and a fake transport."""
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=expires_in)
    monkeypatch.setenv(ENV_AUTH_FILE, str(auth_file))
    monkeypatch.setenv(ENV_CLIENT_VERSION, TEST_VERSION)
    llm = XaiGrokCliLLM(
        provider="xai-grok-cli",
        api_key="",
        base_url="",
        model="grok-4.5",
        reasoning_effort="high",
        timeout=timeout,
    )
    llm._auth = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=cli_path, spawn=spawn)
    llm._client = _FakeHttp(replies or [_ok_reply()])  # type: ignore[assignment]
    return llm


def _ok_reply(content: str = "ok", *, usage: dict[str, Any] | None = None) -> _FakeResponse:
    body: dict[str, Any] = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }
    return _FakeResponse(status_code=200, text=json.dumps(body))


@pytest.fixture(autouse=True)
def _clear_version_cache():
    auth_mod._CLIENT_VERSION_CACHE.clear()
    yield
    auth_mod._CLIENT_VERSION_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. Header set
# ---------------------------------------------------------------------------


async def test_call_sends_every_required_header_and_logs_no_header_value(tmp_path, monkeypatch, caplog):
    llm = _make_llm(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)

    headers = llm._client.calls[0]["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert headers["X-XAI-Token-Auth"] == TOKEN_AUTH_MODE == "xai-grok-cli"
    assert headers["x-grok-client-version"] == TEST_VERSION
    assert headers["x-grok-client-identifier"] == CLIENT_IDENTIFIER == "grok-shell"
    assert headers["x-grok-model-override"] == "grok-4.5"
    assert len(headers["x-grok-conv-id"]) == 32

    # The endpoint routes on the header, so the body's model field is incidental.
    assert llm._client.calls[0]["url"] == "https://cli-chat-proxy.grok.com/v1/chat/completions"

    # No header value — above all the credential — may reach the log.
    for value in headers.values():
        assert value not in caplog.text


async def test_conv_id_is_stable_for_the_same_leading_message(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, replies=[_ok_reply(), _ok_reply()])
    await llm.call(messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "a"}], max_retries=0)
    await llm.call(messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "b"}], max_retries=0)
    first, second = (call["headers"]["x-grok-conv-id"] for call in llm._client.calls)
    assert first == second


async def test_reasoning_effort_and_token_cap_land_in_the_body(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch)
    await llm.call(messages=[{"role": "user", "content": "hi"}], max_completion_tokens=64, max_retries=0)
    body = llm._client.calls[0]["json"]
    assert body["reasoning_effort"] == "high"
    assert body["max_tokens"] == 64
    assert "max_completion_tokens" not in body


# ---------------------------------------------------------------------------
# 2. Entry selection
# ---------------------------------------------------------------------------


def test_select_entry_picks_the_latest_expiry_regardless_of_key_name(tmp_path):
    path = tmp_path / "auth.json"
    data = {
        "https://accounts.x.ai/sign-in": {"key": "old", "expires_at": _iso(60)},
        "https://auth.x.ai::abc": {"key": "new", "expires_at": _iso(9999)},
        "some-other-entry": {"key": "middle", "expires_at": _iso(500)},
    }
    assert select_entry(data, path).key == "new"
    # Insertion order must not decide it.
    assert select_entry(dict(reversed(list(data.items()))), path).key == "new"


def test_select_entry_prefers_a_known_expiry_over_an_unparseable_one(tmp_path):
    path = tmp_path / "auth.json"
    data = {
        "a": {"key": "unparseable", "expires_at": "not-a-timestamp"},
        "b": {"key": "known", "expires_at": _iso(120)},
    }
    assert select_entry(data, path).key == "known"


def test_select_entry_skips_entries_without_a_usable_key(tmp_path):
    path = tmp_path / "auth.json"
    data = {"a": {"expires_at": _iso(600)}, "b": {"key": "   "}, "c": "not-a-dict", "d": {"key": "real"}}
    assert select_entry(data, path).key == "real"


def test_missing_key_field_raises_with_the_login_remediation(tmp_path):
    path = tmp_path / "auth.json"
    with pytest.raises(XaiGrokCliAuthError, match="grok login"):
        select_entry({"a": {"expires_at": _iso(600)}}, path)


def test_missing_auth_file_raises_with_the_login_remediation_and_no_host_path(tmp_path):
    missing = tmp_path / "nested" / "auth.json"
    with pytest.raises(XaiGrokCliAuthError) as excinfo:
        read_session_token(missing)
    assert "grok login" in str(excinfo.value)
    assert str(missing) not in str(excinfo.value)


def test_auth_file_override_is_read_side_only(tmp_path, monkeypatch):
    override = tmp_path / "elsewhere" / "auth.json"
    monkeypatch.setenv(ENV_AUTH_FILE, str(override))
    assert default_auth_file() == override
    monkeypatch.delenv(ENV_AUTH_FILE)
    assert default_auth_file() == Path.home() / ".grok" / "auth.json"


# ---------------------------------------------------------------------------
# 3. Expiry parsing
# ---------------------------------------------------------------------------


def test_nine_digit_fraction_rfc3339_parses():
    """The vendor writes nine fractional digits; stdlib parsing needs six."""
    expected = datetime(2026, 8, 8, 18, 12, 27, tzinfo=timezone.utc).timestamp() + 0.764688
    assert _parse_expiry("2026-08-08T18:12:27.764688200Z") == pytest.approx(expected, abs=1e-6)


def test_offset_timezones_parse():
    assert _parse_expiry("2026-08-08T18:12:27+02:00") == _parse_expiry("2026-08-08T16:12:27Z")
    assert _parse_expiry("2026-08-08T18:12:27+0200") == _parse_expiry("2026-08-08T16:12:27Z")


def test_unparseable_expiry_is_treated_as_expired_never_valid():
    for raw in ("", "yesterday", None, 12345, "2026-13-45T99:99:99Z"):
        assert _parse_expiry(raw) is None
    token = SessionToken(key="k", expires_at=None)
    assert token.seconds_left() == float("-inf")
    assert token.seconds_left() < 0


async def test_unparseable_expiry_triggers_a_refresh(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"a": {"key": TEST_KEY, "expires_at": "garbage"}}), encoding="utf-8")
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)

    token = await manager.get_token(900.0)

    assert spawn.count == 1
    assert token.seconds_left() > 900.0


# ---------------------------------------------------------------------------
# 4. Admission
# ---------------------------------------------------------------------------


async def test_short_ttl_refreshes_before_the_request(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    llm = _make_llm(tmp_path, monkeypatch, expires_in=300, spawn=spawn)

    await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)

    assert spawn.count == 1
    # The re-read credential — not the stale one — is what went on the wire.
    assert llm._client.calls[0]["headers"]["Authorization"] == f"Bearer {TEST_KEY}"


async def test_admission_bar_is_max_of_skew_and_timeout(tmp_path, monkeypatch):
    """A TTL above the 900s skew but below a longer timeout must still refresh."""
    auth_file = tmp_path / "auth.json"
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    llm = _make_llm(tmp_path, monkeypatch, expires_in=1200, spawn=spawn, timeout=1800.0)

    assert llm._admission_ttl() == 1800.0
    await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)
    assert spawn.count == 1


async def test_a_short_timeout_does_not_defeat_the_refresh_skew(tmp_path, monkeypatch):
    """The donor's measured defect: passing the timeout ALONE as the admission bar.

    With a 120s timeout and a token holding 600s, "outlives one request" is
    satisfied, so a timeout-only bar admits the request and never refreshes —
    observed on the donor proxy as zero refreshes with hours of TTL left, which
    silently defeated the 900s skew that exists so a long batch cannot straddle
    expiry. Both bars must apply, so this must still refresh.
    """
    auth_file = tmp_path / "auth.json"
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    llm = _make_llm(tmp_path, monkeypatch, expires_in=600, spawn=spawn, timeout=120.0)

    assert llm._admission_ttl() == 900.0
    await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)
    assert spawn.count == 1


async def test_fresh_token_does_not_spawn(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, expires_in=6 * 3600, spawn=_never_spawn)
    await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)
    assert llm._client.calls


# ---------------------------------------------------------------------------
# 5. 401 recovery
# ---------------------------------------------------------------------------


async def test_first_401_refreshes_once_and_retries_once(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    llm = _make_llm(
        tmp_path,
        monkeypatch,
        spawn=spawn,
        replies=[_FakeResponse(401, '{"error":"unauthorized"}'), _ok_reply("recovered")],
    )

    result = await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=3)

    assert result == "recovered"
    assert len(llm._client.calls) == 2
    assert spawn.count == 1


async def test_second_401_is_terminal_with_no_third_attempt(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    llm = _make_llm(
        tmp_path,
        monkeypatch,
        spawn=spawn,
        replies=[_FakeResponse(401, "{}"), _FakeResponse(401, "{}")],
    )

    with pytest.raises(XaiGrokCliAuthError, match="grok login"):
        await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=5)

    assert len(llm._client.calls) == 2  # never a third
    assert spawn.count == 1


# ---------------------------------------------------------------------------
# 6. Single-flight
# ---------------------------------------------------------------------------


async def test_concurrent_stale_callers_produce_one_refresh(tmp_path):
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=60)
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600, delay=0.05)
    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)

    tokens = await asyncio.gather(manager.get_token(900.0), manager.get_token(900.0))

    assert spawn.count == 1  # the second caller waited on the lock and reused the result
    assert all(token.seconds_left() > 900.0 for token in tokens)


# ---------------------------------------------------------------------------
# 7. Cooldown
# ---------------------------------------------------------------------------


async def test_failed_refresh_is_not_respawned_inside_the_cooldown(tmp_path):
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=-60)  # already expired
    spawn = _FakeSpawn(auth_file)  # refreshes nothing — simulates a failed vendor refresh
    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)

    with pytest.raises(XaiGrokCliAuthError, match="still expired"):
        await manager.get_token(900.0)
    assert spawn.count == 1

    with pytest.raises(XaiGrokCliAuthError) as excinfo:
        await manager.get_token(900.0)
    assert spawn.count == 1  # no respawn inside the cooldown
    assert "grok login" in str(excinfo.value)
    assert "refresh was attempted" in str(excinfo.value)


async def test_cooldown_serves_a_live_token_rather_than_respawning(tmp_path):
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=300)
    spawn = _FakeSpawn(auth_file)  # returns without extending the expiry
    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)

    await manager.get_token(900.0)
    assert spawn.count == 1
    token = await manager.get_token(900.0)
    assert spawn.count == 1
    assert token.key == TEST_KEY


# ---------------------------------------------------------------------------
# 8. CLI absent
# ---------------------------------------------------------------------------


async def test_cli_absent_with_a_fresh_token_still_serves(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, expires_in=6 * 3600, cli_path=None, spawn=_never_spawn)
    assert await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0) == "ok"


async def test_cli_absent_with_an_expired_token_raises_remediation(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, expires_in=-60, cli_path=None, spawn=_never_spawn)
    with pytest.raises(XaiGrokCliAuthError) as excinfo:
        await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)
    message = str(excinfo.value)
    assert "grok login" in message
    assert ENV_CLI_BIN in message


def test_resolve_cli_binary_treats_a_bad_configured_path_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_CLI_BIN, str(tmp_path / "no-such-binary"))
    assert resolve_cli_binary() is None


def test_resolve_cli_binary_uses_the_configured_path(tmp_path, monkeypatch):
    binary = tmp_path / "grok"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(ENV_CLI_BIN, str(binary))
    assert resolve_cli_binary() == binary


# ---------------------------------------------------------------------------
# 9. Version floor
# ---------------------------------------------------------------------------


def test_env_pinned_version_is_used_without_probing(monkeypatch):
    monkeypatch.setenv(ENV_CLIENT_VERSION, "1.2.3")
    resolved = resolve_client_version(cli_path=FAKE_CLI, spawn=_never_spawn)
    assert resolved.text == "1.2.3"


def test_sub_floor_pinned_version_fails_construction_mentioning_426(tmp_path, monkeypatch):
    below = ".".join(str(part) for part in (MIN_CLIENT_VERSION[0], MIN_CLIENT_VERSION[1], MIN_CLIENT_VERSION[2] - 1))
    monkeypatch.setenv(ENV_AUTH_FILE, str(_write_auth(tmp_path / "auth.json")))
    monkeypatch.setenv(ENV_CLIENT_VERSION, below)
    with pytest.raises(XaiGrokCliVersionError, match="426"):
        XaiGrokCliLLM(provider="xai-grok-cli", api_key="", base_url="", model="grok-4.5")


def test_sub_floor_probed_version_fails_mentioning_426_and_grok_update(monkeypatch):
    monkeypatch.delenv(ENV_CLIENT_VERSION, raising=False)
    spawn = _FakeSpawn()
    spawn.stdout = "grok 0.1.201"
    with pytest.raises(XaiGrokCliVersionError) as excinfo:
        resolve_client_version(cli_path=FAKE_CLI, spawn=spawn)
    assert "426" in str(excinfo.value)
    assert "grok update" in str(excinfo.value)


def test_probe_result_is_cached_so_the_cli_is_spawned_once(monkeypatch):
    monkeypatch.delenv(ENV_CLIENT_VERSION, raising=False)
    spawn = _FakeSpawn()
    first = resolve_client_version(cli_path=FAKE_CLI, spawn=spawn)
    second = resolve_client_version(cli_path=FAKE_CLI, spawn=spawn)
    assert first == second == parse_client_version(TEST_VERSION)
    assert spawn.count == 1
    assert spawn.specs[0].argv == [str(FAKE_CLI), "--version"]


def test_no_version_and_no_cli_is_a_hard_error_never_a_headerless_request(monkeypatch):
    monkeypatch.delenv(ENV_CLIENT_VERSION, raising=False)
    with pytest.raises(XaiGrokCliVersionError) as excinfo:
        resolve_client_version(cli_path=None, spawn=_never_spawn)
    assert "426" in str(excinfo.value)
    assert ENV_CLIENT_VERSION in str(excinfo.value)


def test_unparseable_probe_output_is_a_hard_error(monkeypatch):
    monkeypatch.delenv(ENV_CLIENT_VERSION, raising=False)
    spawn = _FakeSpawn()
    spawn.stdout = "grok (dev build)"
    with pytest.raises(XaiGrokCliVersionError, match="426"):
        resolve_client_version(cli_path=FAKE_CLI, spawn=spawn)


async def test_runtime_426_is_terminal_and_names_grok_update(tmp_path, monkeypatch):
    """The local floor is a snapshot: the server can raise it after construction."""
    llm = _make_llm(tmp_path, monkeypatch, replies=[_FakeResponse(426, "outdated")])
    with pytest.raises(XaiGrokCliVersionError) as excinfo:
        await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=5)
    assert "grok update" in str(excinfo.value)
    assert len(llm._client.calls) == 1  # never retried


# ---------------------------------------------------------------------------
# 10. Registration
# ---------------------------------------------------------------------------


def test_provider_does_not_require_an_api_key():
    assert requires_api_key("xai-grok-cli") is False


def test_default_model_resolves_to_grok_4_5():
    assert PROVIDER_DEFAULT_MODELS["xai-grok-cli"] == "grok-4.5"
    assert _get_default_model_for_provider("xai-grok-cli") == "grok-4.5"


def test_factory_builds_the_provider_and_threads_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_AUTH_FILE, str(_write_auth(tmp_path / "auth.json")))
    monkeypatch.setenv(ENV_CLIENT_VERSION, TEST_VERSION)
    provider = create_llm_provider(
        provider="xai-grok-cli",
        api_key="",
        base_url="",
        model="grok-4.5",
        reasoning_effort="high",
        timeout=321.0,
    )
    assert isinstance(provider, XaiGrokCliLLM)
    assert provider.base_url == "https://cli-chat-proxy.grok.com/v1"
    assert provider.timeout == 321.0
    # The configured timeout must reach the HTTP client, not just the object.
    assert provider._client.timeout.read == 321.0
    assert provider.supports_attempt_scoped_concurrency() is True


def test_llm_provider_validation_accepts_the_provider(tmp_path, monkeypatch):
    from hindsight_api.engine.llm_wrapper import LLMProvider

    monkeypatch.setenv(ENV_AUTH_FILE, str(_write_auth(tmp_path / "auth.json")))
    monkeypatch.setenv(ENV_CLIENT_VERSION, TEST_VERSION)
    wrapper = LLMProvider(provider="xai-grok-cli", api_key="", base_url="", model="grok-4.5")
    assert wrapper.provider == "xai-grok-cli"


def test_provider_specific_base_url_override_wins(tmp_path, monkeypatch):
    from hindsight_api.engine.providers.xai_grok_cli_llm import ENV_BASE_URL

    monkeypatch.setenv(ENV_AUTH_FILE, str(_write_auth(tmp_path / "auth.json")))
    monkeypatch.setenv(ENV_CLIENT_VERSION, TEST_VERSION)
    monkeypatch.setenv(ENV_BASE_URL, "https://staging.example.com/v1/")
    llm = XaiGrokCliLLM(provider="xai-grok-cli", api_key="", base_url="https://ignored.example.com/v1", model="g")
    assert llm.base_url == "https://staging.example.com/v1"


# ---------------------------------------------------------------------------
# 11. Windows spawn flags
# ---------------------------------------------------------------------------


def test_creationflags_are_create_no_window_on_windows_and_zero_elsewhere():
    win32_flag = 0x08000000  # subprocess.CREATE_NO_WINDOW
    with patch("sys.platform", "win32"), patch.object(subprocess, "CREATE_NO_WINDOW", win32_flag, create=True):
        assert no_window_creationflags() == win32_flag
    with patch("sys.platform", "linux"):
        assert no_window_creationflags() == 0


async def test_refresh_spawn_carries_the_windowless_flag_and_the_donor_argv(tmp_path):
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=60)
    spawn = _FakeSpawn(auth_file, refresh_to=6 * 3600)
    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)

    win32_flag = 0x08000000
    with patch("sys.platform", "win32"), patch.object(subprocess, "CREATE_NO_WINDOW", win32_flag, create=True):
        await manager.get_token(900.0)

    spec = spawn.specs[0]
    assert spec.creationflags == win32_flag
    assert spec.argv == [
        str(FAKE_CLI),
        "-p",
        "ok",
        "--output-format",
        "json",
        "--no-memory",
        "--no-subagents",
        "--no-plan",
        "--max-turns",
        "1",
    ]
    # The child inherits the environment with the CLI's extras switched off.
    assert spec.env is not None
    assert spec.env["GROK_MEMORY"] == "0"
    assert spec.env["GROK_SUBAGENTS"] == "0"


async def test_refresh_never_writes_the_auth_file_itself(tmp_path):
    auth_file = _write_auth(tmp_path / "auth.json", expires_in=60)
    before = auth_file.read_bytes()

    spawn = _FakeSpawn(auth_file)  # runs, but writes nothing back

    manager = XaiGrokCliAuthManager(auth_file=auth_file, cli_path=FAKE_CLI, spawn=spawn)
    await manager.get_token(900.0)  # 60s left, so this really does run a refresh

    assert spawn.count == 1
    assert auth_file.read_bytes() == before


# ---------------------------------------------------------------------------
# 12. Usage parsing
# ---------------------------------------------------------------------------


def test_cached_tokens_are_read_when_prompt_tokens_details_is_present():
    usage = _ChatUsage.model_validate(
        {
            "prompt_tokens": 4543,
            "completion_tokens": 373,
            "total_tokens": 4916,
            "prompt_tokens_details": {"cached_tokens": 4480},
        }
    )
    counts = _token_counts(usage)
    assert counts.input_tokens == 4543
    assert counts.cached_tokens == 4480
    assert counts.output_tokens == 373
    assert counts.thoughts_tokens == 0


def test_missing_prompt_tokens_details_reads_zero_cached_without_error():
    """A streamed-shape response omits the details block — low, not broken."""
    usage = _ChatUsage.model_validate({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    counts = _token_counts(usage)
    assert counts.cached_tokens == 0
    assert counts.input_tokens == 100
    assert _token_counts(None).input_tokens == 0


def test_reasoning_tokens_are_subtracted_from_visible_output():
    usage = _ChatUsage.model_validate(
        {
            "prompt_tokens": 10,
            "completion_tokens": 90,
            "total_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 60},
        }
    )
    counts = _token_counts(usage)
    assert counts.thoughts_tokens == 60
    assert counts.output_tokens == 30
    assert counts.total_tokens == 40


async def test_return_usage_surfaces_cached_tokens(tmp_path, monkeypatch):
    reply = _ok_reply(
        usage={
            "prompt_tokens": 4543,
            "completion_tokens": 373,
            "total_tokens": 4916,
            "prompt_tokens_details": {"cached_tokens": 4480},
        }
    )
    llm = _make_llm(tmp_path, monkeypatch, replies=[reply])
    _, usage = await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0, return_usage=True)
    assert usage.cached_tokens == 4480
    assert usage.input_tokens == 4543


# ---------------------------------------------------------------------------
# Structured output and tool calls
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    answer: str


async def test_structured_output_uses_strict_json_schema_when_requested(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, replies=[_ok_reply('{"answer": "42"}')])
    result = await llm.call(
        messages=[{"role": "user", "content": "hi"}],
        response_format=_Answer,
        strict_schema=True,
        max_retries=0,
    )
    body = llm._client.calls[0]["json"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert result.answer == "42"


async def test_structured_output_falls_back_to_schema_in_prompt(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, replies=[_ok_reply('```json\n{"answer": "42"}\n```')])
    result = await llm.call(
        messages=[{"role": "system", "content": "You are terse."}, {"role": "user", "content": "hi"}],
        response_format=_Answer,
        max_retries=0,
    )
    body = llm._client.calls[0]["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert "matching this schema" in body["messages"][0]["content"]
    assert result.answer == "42"  # markdown fences are unwrapped


async def test_call_with_tools_returns_proposed_tool_calls(tmp_path, monkeypatch):
    reply = _FakeResponse(
        200,
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [{"id": "c1", "function": {"name": "recall", "arguments": '{"query": "x"}'}}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        ),
    )
    llm = _make_llm(tmp_path, monkeypatch, replies=[reply])
    result = await llm.call_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "recall", "parameters": {}}}],
        max_retries=0,
    )
    assert result.finish_reason == "tool_calls"
    assert [call.name for call in result.tool_calls] == ["recall"]
    assert result.tool_calls[0].arguments == {"query": "x"}
    assert llm._client.calls[0]["json"]["tools"][0]["function"]["name"] == "recall"


async def test_retryable_upstream_error_is_retried_then_succeeds(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, replies=[_FakeResponse(503, "busy"), _ok_reply("second")])
    result = await llm.call(
        messages=[{"role": "user", "content": "hi"}], max_retries=2, initial_backoff=0.0, max_backoff=0.0
    )
    assert result == "second"
    assert len(llm._client.calls) == 2


async def test_non_retryable_upstream_error_fails_immediately(tmp_path, monkeypatch):
    llm = _make_llm(tmp_path, monkeypatch, replies=[_FakeResponse(400, "bad request")])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        await llm.call(
            messages=[{"role": "user", "content": "hi"}], max_retries=3, initial_backoff=0.0, max_backoff=0.0
        )
    assert len(llm._client.calls) == 1


async def test_upstream_error_body_is_not_echoed_into_the_error(tmp_path, monkeypatch):
    secret_ish = "sensitive-upstream-payload"
    llm = _make_llm(tmp_path, monkeypatch, replies=[_FakeResponse(400, secret_ish)])
    with pytest.raises(RuntimeError) as excinfo:
        await llm.call(messages=[{"role": "user", "content": "hi"}], max_retries=0)
    assert secret_ish not in str(excinfo.value)
