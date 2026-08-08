"""
Session-credential manager for the ``xai-grok-cli`` provider.

The SuperGrok CLI chat proxy (``https://cli-chat-proxy.grok.com/v1``) is the
backend the vendor's own Grok CLI talks to. It authenticates with a short-lived
session token — not a static API key — that the CLI writes to
``~/.grok/auth.json`` and rotates roughly every six hours, and it refuses any
request whose ``x-grok-client-version`` header is missing or below a
server-enforced floor (HTTP 426).

Split out of ``xai_grok_cli_llm.py`` following the ``codex_llm`` /
``codex_auth`` precedent: the provider owns the wire protocol, this module owns
the credential lifecycle (read, expiry math, refresh, CLI discovery, version
floor).

Refresh strategy — the one place this deliberately differs from Codex/Nous
--------------------------------------------------------------------------
Codex and Nous refresh natively against published OAuth endpoints. xAI has no
known published refresh endpoint for this credential, and ``auth.json`` is
SHARED with the operator's live Grok CLI: a rotation bug here could log them out
of their own tooling. So a refresh is performed by spawning the vendor's own CLI
once with a throwaway prompt and letting its code refresh the credential, then
re-reading the file. This module only ever READS ``auth.json``; it never writes
it.

Safety invariants
-----------------
* The session token is never logged, never returned in an error message, and
  never placed in a metric or span attribute — only TTLs and counters are.
* CLI absence is a soft state: while the token is fresh the provider keeps
  serving (the operator's own interactive use refreshes it). Refresh becomes
  unavailable, so an expiry then surfaces as a remediation error rather than a
  crash.
* Every spawn is windowless on Windows: the CLI is a console-subsystem binary
  and popped a real terminal on the operator's desktop when spawned without
  ``CREATE_NO_WINDOW``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------

#: Read-side override for the credential file. Points the READER at a different
#: ``auth.json`` (tests, or a read-only mount inside a container). It does NOT
#: relocate where a spawned CLI WRITES on refresh, so this is not the
#: read-and-write relocation ``CODEX_HOME`` provides for the Codex provider: a
#: long-running service that sets this must keep the file fresh by other means
#: (see the deployment matrix in the docs).
ENV_AUTH_FILE = "HINDSIGHT_API_XAI_GROK_CLI_AUTH_FILE"

#: Path to the Grok CLI binary used for the version probe and token refresh.
ENV_CLI_BIN = "HINDSIGHT_API_XAI_GROK_CLI_BIN"

#: Pins ``x-grok-client-version`` instead of probing the CLI for it. Required
#: for deployments that carry no CLI at all (the endpoint answers 426 without
#: the header, so there is no "omit it" fallback).
ENV_CLIENT_VERSION = "HINDSIGHT_API_XAI_GROK_CLI_CLIENT_VERSION"


# ---------------------------------------------------------------------------
# Protocol constants (measured against the live endpoint, 2026-08-01)
# ---------------------------------------------------------------------------

#: Server-enforced client-version floor. Below this the endpoint answers HTTP
#: 426 ("Your Grok CLI version (none) is outdated") instead of serving. This is
#: a snapshot of a server-side value, so a runtime 426 is handled separately —
#: the server can raise the floor after a request passes this local gate.
MIN_CLIENT_VERSION = (0, 1, 202)

#: Refresh margin. Observed token TTL is ~6h; warming early keeps the admission
#: rule ("the token must outlive one whole request") from rejecting traffic at
#: the expiry boundary.
DEFAULT_REFRESH_SKEW_SECONDS = 900.0

#: Minimum gap between refreshes. Without it, a refresh that returns a token
#: still inside the skew window makes every later request spawn the CLI again.
DEFAULT_WARM_COOLDOWN_SECONDS = 120.0

#: Ceiling for one refresh spawn. A cold CLI start is ~30s.
DEFAULT_WARM_TIMEOUT_SECONDS = 240.0

#: Ceiling for the ``--version`` probe.
DEFAULT_VERSION_TIMEOUT_SECONDS = 60.0

#: ``auth.json`` is rewritten in place by the vendor's refresh, so a reader can
#: land mid-write. A parse/IO failure is retried briefly before being an error.
AUTH_READ_ATTEMPTS = 4
AUTH_READ_BACKOFF_SECONDS = 0.15

#: The refresh call exists only for its auth side effect, so every discovery and
#: compatibility feature of the CLI is dead weight on it.
_WARM_ARGV_TAIL = (
    "-p",
    "ok",
    "--output-format",
    "json",
    "--no-memory",
    "--no-subagents",
    "--no-plan",
    "--max-turns",
    "1",
)

_WARM_CHILD_ENV_OVERRIDES = {
    "GROK_CLAUDE_RULES_ENABLED": "0",
    "GROK_CLAUDE_SKILLS_ENABLED": "0",
    "GROK_CLAUDE_MCPS_ENABLED": "0",
    "GROK_CLAUDE_AGENTS_ENABLED": "0",
    "GROK_CLAUDE_HOOKS_ENABLED": "0",
    "GROK_CURSOR_SKILLS_ENABLED": "0",
    "GROK_CURSOR_RULES_ENABLED": "0",
    "GROK_MEMORY": "0",
    "GROK_SUBAGENTS": "0",
}

_LOGIN_REMEDIATION = "Run `grok login` on the host that owns ~/.grok/auth.json, then retry."
_UPDATE_REMEDIATION = "Run `grok update` on the host, or pin a known-good version via " + ENV_CLIENT_VERSION + "."

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_EXPIRY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")

#: One ``--version`` probe per binary per process, keyed by resolved path. The
#: value is a validated, above-floor version — a failure is never cached, so a
#: `grok update` that fixes the floor takes effect on the next construction.
_CLIENT_VERSION_CACHE: dict[str, ClientVersion] = {}


class XaiGrokCliAuthError(RuntimeError):
    """The SuperGrok session credential is unusable and needs operator action.

    Always carries a remediation sentence. Never carries the credential itself,
    a full host path, or any upstream response body.
    """


class XaiGrokCliVersionError(RuntimeError):
    """The Grok CLI version is unknown or below the server-enforced floor.

    Terminal: the endpoint answers HTTP 426 rather than serving, and no amount
    of retrying changes that — the CLI has to be updated (or the version pinned).
    """


@dataclass(frozen=True, slots=True)
class SessionToken:
    """One credential entry read from ``auth.json``.

    Only ``key`` and ``expires_at`` are taken. The entry's own name is
    deliberately not carried: it is auth-mode dependent
    (``https://auth.x.ai::<uuid>`` under OIDC, a plain sign-in URL in the
    vendor's own doc example), so entries are never matched or reported by name.
    """

    key: str
    expires_at: float | None

    def seconds_left(self, now: float | None = None) -> float:
        """Seconds until expiry; ``-inf`` when the expiry could not be read.

        An unreadable expiry means "refresh it", never "assume valid" — the
        opposite default would serve a request against a credential we know
        nothing about.
        """
        if self.expires_at is None:
            return float("-inf")
        return self.expires_at - (now if now is not None else time.time())


@dataclass(frozen=True, slots=True)
class ClientVersion:
    """A parsed ``x-grok-client-version`` value plus its ordering key."""

    text: str
    parts: tuple[int, int, int]

    def below_floor(self) -> bool:
        return self.parts < MIN_CLIENT_VERSION


@dataclass(frozen=True)
class CliSpawnSpec:
    """Everything one CLI spawn needs, as data.

    Passing a spec (rather than positional args) is what makes the spawn
    injectable: tests substitute a callable that records the spec and assert on
    ``argv`` and ``creationflags`` without ever starting a process.
    """

    argv: list[str]
    timeout_s: float
    creationflags: int
    #: Child environment, or None to inherit this process's environment
    #: unchanged. Genuinely dynamic keys, hence a plain mapping.
    env: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class CliSpawnResult:
    """Captured outcome of one completed CLI spawn."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class WarmOutcome:
    """Whether a refresh spawn ran to completion, and a loggable detail.

    ``ran`` is False only when the spawn could not run at all (timeout, OSError).
    A non-zero exit code still counts as ran: the vendor's refresh happens during
    CLI startup, so a later failure does not mean the credential was not renewed.
    """

    ran: bool
    detail: str


CliSpawn = Callable[[CliSpawnSpec], CliSpawnResult]


# ---------------------------------------------------------------------------
# Credential file
# ---------------------------------------------------------------------------


def default_auth_file() -> Path:
    """Return the credential path, honouring the read-side override.

    Resolved on each call rather than cached at import so the environment is
    read at the point of use.
    """
    override = os.environ.get(ENV_AUTH_FILE, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok" / "auth.json"


def _parse_expiry(raw: Any) -> float | None:
    """Parse ``expires_at`` (RFC3339) to a POSIX timestamp, or None.

    The vendor writes nine fractional digits (``...T18:12:27.764688200Z``), which
    ``datetime.fromisoformat`` rejects on some versions, so the fraction is
    truncated to microseconds before parsing. Returns None when unparseable —
    callers treat an unknown expiry as "refresh it", never as "assume valid".
    """
    if not isinstance(raw, str):
        return None
    match = _EXPIRY_RE.match(raw.strip())
    if not match:
        return None
    base, frac, tz = match.group(1), match.group(2) or "", match.group(3) or "Z"
    micros = (frac + "000000")[:6]
    tz_norm = "+00:00" if tz in ("Z", "z") else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
    from datetime import datetime

    try:
        return datetime.fromisoformat(f"{base}.{micros}{tz_norm}").timestamp()
    except ValueError:
        return None


def select_entry(data: Any, auth_path: Path) -> SessionToken:
    """Pick the usable credential entry with the latest expiry.

    Every entry is considered regardless of its key name (there is no mode field
    to select on), and only ``key`` + ``expires_at`` are read out of it. An entry
    with an unparseable expiry sorts below any entry with a real one, so a
    readable credential always wins over an unknown-expiry sibling.
    """
    if not isinstance(data, dict) or not data:
        raise XaiGrokCliAuthError(f"{auth_path.name} held no credential entries. {_LOGIN_REMEDIATION}")

    best: SessionToken | None = None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        candidate = SessionToken(key=key.strip(), expires_at=_parse_expiry(entry.get("expires_at")))
        if best is None or candidate.seconds_left(0.0) > best.seconds_left(0.0):
            best = candidate

    if best is None:
        raise XaiGrokCliAuthError(f"{auth_path.name} had no usable 'key' field. {_LOGIN_REMEDIATION}")
    return best


def read_session_token(auth_path: Path) -> SessionToken:
    """Read the best credential entry from ``auth_path``.

    Blocking: callers on the event loop run this through ``asyncio.to_thread``.

    A parse/IO failure is RETRIED because the vendor's refresh rewrites this file
    in place — a reader that lands mid-write would otherwise turn the moment the
    credential is being fixed into a hard lane failure. Absence is not a
    mid-write race, so it fails immediately. Messages carry the file's basename
    only, never the full host path.
    """
    for attempt in range(1, AUTH_READ_ATTEMPTS + 1):
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise XaiGrokCliAuthError(f"{auth_path.name} not found. {_LOGIN_REMEDIATION}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            if attempt < AUTH_READ_ATTEMPTS:
                time.sleep(AUTH_READ_BACKOFF_SECONDS)
                continue
            raise XaiGrokCliAuthError(
                f"cannot read {auth_path.name} after {attempt} attempts ({type(exc).__name__}). {_LOGIN_REMEDIATION}"
            ) from exc
        else:
            return select_entry(data, auth_path)
    # Unreachable: the loop above either returns or raises on its last attempt.
    raise XaiGrokCliAuthError(f"cannot read {auth_path.name}. {_LOGIN_REMEDIATION}")


# ---------------------------------------------------------------------------
# CLI discovery, spawning, version floor
# ---------------------------------------------------------------------------


def no_window_creationflags() -> int:
    """Return ``CREATE_NO_WINDOW`` on Windows and 0 everywhere else.

    Written as a statement-guard rather than a ternary on purpose: the
    ``sys.platform == "win32"`` comparison is what narrows the platform for type
    checkers running on Linux, where ``subprocess.CREATE_NO_WINDOW`` does not
    exist. A ternary reads the attribute unconditionally and fails to check.

    This is not cosmetic: the Grok CLI is a console-subsystem binary, so a spawn
    without this flag pops a real terminal window on the operator's desktop for
    the duration of the call.
    """
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW
    return 0


def run_cli(spec: CliSpawnSpec) -> CliSpawnResult:
    """Run one CLI spawn synchronously. Blocking — call via ``asyncio.to_thread``.

    Lets ``OSError`` / ``subprocess.TimeoutExpired`` propagate so each caller can
    decide what a failed spawn means (a soft warm failure vs a hard version
    error).
    """
    completed = subprocess.run(  # noqa: S603 - argv is built from a resolved binary path, never a shell string
        spec.argv,
        capture_output=True,
        text=True,
        timeout=spec.timeout_s,
        env=spec.env,
        creationflags=spec.creationflags,
        check=False,
    )
    return CliSpawnResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def resolve_cli_binary() -> Path | None:
    """Locate the Grok CLI, or return None when it is absent.

    Order: the configured path, then ``grok``/``agent`` on PATH, then the
    vendor's default install location. ``shutil.which`` applies PATHEXT on
    Windows, which is what picks up ``grok.exe``.

    None is a supported state, not an error: with a fresh token the provider
    still serves, it just cannot refresh (see ``XaiGrokCliAuthManager``).
    """
    configured = os.environ.get(ENV_CLI_BIN, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
        # Explicit misconfiguration: say so once rather than silently falling
        # back to a different binary than the operator asked for.
        logger.warning("%s points at %s, which is not a file; treating the Grok CLI as absent", ENV_CLI_BIN, candidate)
        return None

    for name in ("grok", "agent"):
        found = shutil.which(name)
        if found:
            return Path(found)

    default = Path.home() / ".grok" / "bin" / ("agent.exe" if sys.platform == "win32" else "agent")
    return default if default.is_file() else None


def parse_client_version(text: str) -> ClientVersion | None:
    """Extract the first ``N.N.N`` version from CLI output, or None."""
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return ClientVersion(text=match.group(0), parts=(int(match.group(1)), int(match.group(2)), int(match.group(3))))


def resolve_client_version(*, cli_path: Path | None, spawn: CliSpawn = run_cli) -> ClientVersion:
    """Resolve ``x-grok-client-version``: env pin, else one cached CLI probe.

    Failure is loud and terminal rather than "send nothing": a request without
    the header earns an HTTP 426 whose message ("Your Grok CLI version (none) is
    outdated") is far less obvious at 3am than this error.

    The probe result is cached per binary path for the process lifetime, so
    constructing several members of a multi-LLM chain costs one spawn, not one
    per member.
    """
    floor = ".".join(str(part) for part in MIN_CLIENT_VERSION)

    pinned = os.environ.get(ENV_CLIENT_VERSION, "").strip()
    if pinned:
        parsed = parse_client_version(pinned)
        if parsed is None:
            raise XaiGrokCliVersionError(
                f"{ENV_CLIENT_VERSION}={pinned!r} is not an N.N.N version; the upstream requires a valid "
                "x-grok-client-version header and answers HTTP 426 without one."
            )
        if parsed.below_floor():
            raise XaiGrokCliVersionError(
                f"{ENV_CLIENT_VERSION}={parsed.text} is below the upstream floor {floor}; the endpoint would "
                f"only answer HTTP 426. {_UPDATE_REMEDIATION}"
            )
        return parsed

    if cli_path is None:
        raise XaiGrokCliVersionError(
            "cannot determine the Grok CLI version: no CLI found on this host and "
            f"{ENV_CLIENT_VERSION} is unset. The upstream rejects requests without an "
            f"x-grok-client-version header (HTTP 426), so set {ENV_CLIENT_VERSION} "
            f"(floor {floor}) or install the Grok CLI."
        )

    cached = _CLIENT_VERSION_CACHE.get(str(cli_path))
    if cached is not None:
        return cached

    spec = CliSpawnSpec(
        argv=[str(cli_path), "--version"],
        timeout_s=DEFAULT_VERSION_TIMEOUT_SECONDS,
        creationflags=no_window_creationflags(),
    )
    try:
        result = spawn(spec)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise XaiGrokCliVersionError(
            f"cannot read the Grok CLI version from {cli_path.name} ({type(exc).__name__}); the upstream "
            f"rejects requests without an x-grok-client-version header (HTTP 426). Set {ENV_CLIENT_VERSION} "
            "to pin it instead."
        ) from exc

    parsed = parse_client_version(f"{result.stdout}\n{result.stderr}")
    if parsed is None:
        raise XaiGrokCliVersionError(
            f"could not parse a version from `{cli_path.name} --version`; refusing to start because the "
            f"upstream requires an x-grok-client-version header (HTTP 426). Set {ENV_CLIENT_VERSION} to pin it."
        )
    if parsed.below_floor():
        raise XaiGrokCliVersionError(
            f"Grok CLI version {parsed.text} is below the upstream floor {floor}; the endpoint would only "
            f"answer HTTP 426. {_UPDATE_REMEDIATION}"
        )

    _CLIENT_VERSION_CACHE[str(cli_path)] = parsed
    logger.info("xai-grok-cli client version %s (floor %s)", parsed.text, floor)
    return parsed


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


class XaiGrokCliAuthManager:
    """Serves a session token with a caller-specified minimum remaining life.

    Single-flight: concurrent coroutines that all see a stale token produce one
    refresh, not N — the ``asyncio.Lock`` is held across the spawn precisely so
    the others wait for its result instead of each starting their own CLI.

    A cooldown bounds how often refreshing can happen. Without it, a refresh that
    returns a token still inside the skew window would put a ~30s CLI start on
    the critical path of every subsequent request, forever.

    The blocking work (file reads, the spawn) is offloaded with
    ``asyncio.to_thread`` so the engine's event loop is never stalled.
    """

    def __init__(
        self,
        *,
        auth_file: Path | None = None,
        cli_path: Path | None = None,
        refresh_skew_s: float = DEFAULT_REFRESH_SKEW_SECONDS,
        warm_cooldown_s: float = DEFAULT_WARM_COOLDOWN_SECONDS,
        warm_timeout_s: float = DEFAULT_WARM_TIMEOUT_SECONDS,
        spawn: CliSpawn = run_cli,
    ) -> None:
        self.auth_file = auth_file or default_auth_file()
        self.cli_path = cli_path
        self.refresh_skew_s = refresh_skew_s
        self.warm_cooldown_s = warm_cooldown_s
        self.warm_timeout_s = warm_timeout_s
        self._spawn = spawn
        self._lock = asyncio.Lock()
        self._warms = 0
        self._last_warm_monotonic = 0.0

    async def get_token(self, min_ttl_s: float) -> SessionToken:
        """Return a token with at least ``min_ttl_s`` of life left."""
        token = await asyncio.to_thread(read_session_token, self.auth_file)
        if token.seconds_left() > min_ttl_s:
            return token
        return await self._refresh_and_reread(token, threshold=min_ttl_s, forced=False)

    async def force_warm(self, rejected: SessionToken) -> SessionToken:
        """Refresh regardless of local expiry, for an upstream 401.

        The file can look fresh while the server has already invalidated the
        session (clock skew, revocation). The cooldown still applies, so a 401
        storm cannot become a CLI-spawn storm.
        """
        return await self._refresh_and_reread(rejected, threshold=None, forced=True)

    async def _refresh_and_reread(self, stale: SessionToken, *, threshold: float | None, forced: bool) -> SessionToken:
        async with self._lock:
            # Re-read under the lock: another coroutine, or the operator's own
            # Grok session, may have refreshed while we queued.
            token = await asyncio.to_thread(read_session_token, self.auth_file)
            if threshold is not None and token.seconds_left() > threshold:
                return token
            if forced and token.key != stale.key:
                # Someone already replaced the token the upstream rejected.
                return token

            since = time.monotonic() - self._last_warm_monotonic
            if self._last_warm_monotonic and since < self.warm_cooldown_s:
                # Inside the cooldown: serve what we have if it is alive at all,
                # rather than spawning again or failing outright.
                if token.seconds_left() > 0:
                    logger.info(
                        "xai-grok-cli refresh skipped (cooldown): since_s=%.0f cooldown_s=%.0f",
                        since,
                        self.warm_cooldown_s,
                    )
                    return token
                raise XaiGrokCliAuthError(
                    f"the SuperGrok session token is expired and a refresh was attempted {since:.0f}s ago "
                    f"without success. {_LOGIN_REMEDIATION}"
                )

            if self.cli_path is None:
                raise XaiGrokCliAuthError(
                    "the SuperGrok session token is expired or expiring and no Grok CLI is available to "
                    f"refresh it (set {ENV_CLI_BIN} to the CLI binary, or keep the credential fresh on the "
                    f"host that owns it). {_LOGIN_REMEDIATION}"
                )

            outcome = await self._warm(token)
            token = await asyncio.to_thread(read_session_token, self.auth_file)
            left = token.seconds_left()
            if left <= 0:
                raise XaiGrokCliAuthError(
                    f"the SuperGrok session token is still expired after a refresh attempt. {_LOGIN_REMEDIATION}"
                )
            if not outcome.ran or (threshold is not None and left <= threshold):
                # Loud at the moment of the problem: the token is alive but the
                # refresh did not achieve what it should have. Discovering this
                # later looks like random upstream auth flakiness.
                logger.warning(
                    "xai-grok-cli refresh was ineffective (seconds_left=%s threshold=%s ran=%s outcome=%s) — "
                    "serving a short-lived token; %s",
                    "unknown" if left == float("-inf") else f"{left:.0f}",
                    "none" if threshold is None else f"{threshold:.0f}",
                    outcome.ran,
                    outcome.detail,
                    _LOGIN_REMEDIATION,
                )
            return token

    async def _warm(self, stale: SessionToken) -> WarmOutcome:
        """Trigger the vendor's own silent refresh with one throwaway CLI call.

        Never writes ``auth.json`` — the vendor's code does, and only ever reads
        it back here. The cooldown clock starts BEFORE the spawn so that a
        refresh which fails still holds off the next attempt.
        """
        assert self.cli_path is not None  # guarded by the caller
        self._warms += 1
        self._last_warm_monotonic = time.monotonic()
        left = stale.seconds_left()
        logger.info(
            "xai-grok-cli refreshing the session token (seconds_left=%s, refreshes=%d)",
            "unknown" if left == float("-inf") else f"{left:.0f}",
            self._warms,
        )

        spec = CliSpawnSpec(
            argv=[str(self.cli_path), *_WARM_ARGV_TAIL],
            timeout_s=self.warm_timeout_s,
            creationflags=no_window_creationflags(),
            env=self._warm_child_env(),
        )
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(self._spawn, spec)
        except subprocess.TimeoutExpired:
            logger.warning("xai-grok-cli refresh timed out after %.0fs", self.warm_timeout_s)
            return WarmOutcome(ran=False, detail="timeout")
        except OSError as exc:
            logger.warning("xai-grok-cli refresh could not spawn the CLI (%s)", type(exc).__name__)
            return WarmOutcome(ran=False, detail=type(exc).__name__)

        logger.info(
            "xai-grok-cli refresh finished (rc=%s, duration_s=%.1f)", result.returncode, time.monotonic() - started
        )
        return WarmOutcome(ran=True, detail=f"rc={result.returncode}")

    @staticmethod
    def _warm_child_env() -> dict[str, str]:
        """Environment for the throwaway CLI call.

        The child inherits this process's environment — that is how the vendor's
        CLI finds its own credentials — with the CLI's discovery/compat features
        switched off so the call stays as cheap as possible. It is not a
        secret-free sandbox.
        """
        env = os.environ.copy()
        env.update(_WARM_CHILD_ENV_OVERRIDES)
        return env
