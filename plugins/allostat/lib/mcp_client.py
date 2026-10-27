"""MCP client for the Allostat hosted server.

Thin Streamable-HTTP client that hook scripts use to call server-side
pillar tools. Handles:

  - Harness-scoped bearer authentication (Claude and Codex never share authority)
  - Endpoint resolution (ALLOSTAT_MCP_ENDPOINT, default mcp.allostat.ai)
  - Retry-with-backoff on 5xx + connection errors (max 3 retries)
  - Rate-limit awareness (honors 429 Retry-After header)
  - Request ID propagation (server-side request_log correlation)
  - Pillar-tool call helpers per pillar (one method per server tool)

Errors are surfaced as MCPClientError with .status_code, .body, .retry_after
fields. Hook scripts catch + degrade silently — the regulator must never
break the operator's session because of a server-side failure. The wrapper
plugin's behavior on MCP failure is: log the error to local
observations.jsonl and proceed as if the server returned no_op.

Lives in a Claude Code plugin runtime — Python stdlib only (no httpx,
no requests, no MCP SDK on the wrapper side). urllib + json is enough
for Streamable HTTP at this thin layer.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from secret_redaction import redact_payload


# v0.2.3 PATCH-122 v4 helper. Module-level so MCPClientConfig.from_env can
# call it without an extra import dance.
_ENV_FILE_BEARER_PATTERN = re.compile(
    r'^\s*ALLOSTAT_MCP_TOKEN\s*=\s*"?([^"\s]+)"?\s*$', re.MULTILINE
)
_CREDENTIAL_FUTURE_SKEW_SECONDS = 300.0


def _read_bearer_candidates(harness: str = "claude") -> list[str]:
    """Read distinct installed bearers in bounded authentication order.

    v0.2.5 PATCH-122 v5: tries multiple candidate locations because
    Claude Code may load hook scripts from EITHER the marketplace tree
    OR the cache/<version> tree, and only the marketplace tree had .env
    in v0.2.4 (install.ps1 only wrote one location). v0.2.5 install.ps1
    writes .env to both trees, but this multi-location lookup is the
    safety net so any deployment layout works.

    Candidate inventory before the harness filter:
      1. <plugin_dir>/.env   (sibling to this module's lib/, the v0.2.3
                              canonical location — works when hook
                              loaded from same tree as .env)
      2. ~/.claude/plugins/marketplaces/local/allostat-mcp/.env
                             (cross-tree fallback — works when hook
                              loaded from cache tree but .env lives
                              in marketplace tree)
      3. ~/.allostat/codex/credential.env (durable across Codex upgrades)

    The function retains 1+2 for Claude or 1+3 for Codex. It never ranks all
    three together.

    NEWEST WINS, not first-hit-wins (2026-07-27). First-hit-wins made
    candidate 1 authoritative, and candidate 1 is VERSION-PINNED: it lives in
    cache/local/allostat-mcp/<version>/, a directory that is written once at
    install and then frozen forever. Candidate 2 is the one location every
    installer run rewrites. So once the bearer rotated, any session pinned to
    an older version directory kept reading a credential that no longer
    existed, while the live one sat in candidate 2 and was never consulted.

    The cost was total and silent. A rejected bearer 401s every MCP call, so
    SessionStart's `refresh_subscription_state` cannot reach the server and
    writes the fail-closed `"unknown"` sentinel; `should_inject` reads that and
    every downstream hook early-returns. Allostat stops writing handoffs and
    stops regulating while still appearing installed — the local innate rules
    keep firing, so the plugin looks alive. Measured on the operator's machine
    2026-07-26: bearer rotated 16:20:12Z, first 401 16:21:36Z, 598 following.

    Sane mtimes order candidates newest-first. A timestamp more than five
    minutes in the future is not freshness evidence, so it ranks after every
    sane candidate while remaining available as a bounded fallback.
    This does NOT relax entitlement — it only stops a PAYING subscriber from
    being misread as unauthenticated. A genuine lapse still returns
    lapsed/canceled from the server and still disables the wrapper.

    Returns a de-duplicated list. Best-effort read/stat failures skip only the
    affected path. Configuration may add only the requested harness's scoped
    process variable; the ambiguous legacy process variable is accepted solely
    for a fileless single-harness install.
    """
    if harness not in {"claude", "codex"}:
        raise ValueError(f"unknown Allostat harness: {harness!r}")
    local_candidates: list[Path] = []
    claude_candidates: list[Path] = []
    codex_candidates: list[Path] = []
    try:
        local_candidates.append(Path(__file__).resolve().parent.parent / ".env")
    except (OSError, ValueError):
        pass
    try:
        claude_candidates.append(
            Path.home() / ".claude" / "plugins" / "marketplaces"
            / "local" / "allostat-mcp" / ".env"
        )
    except (OSError, ValueError):
        pass
    try:
        # Codex-lane durable store (2026-08-08, Codex marketplace plan Phase 1):
        # matches install.py's INSTALL_DIR_PARTS + RECOVERY_FILE_NAME, and is
        # what the activation skill writes. On Codex the plugin directory is a
        # marketplace SNAPSHOT that `codex plugin marketplace upgrade` replaces
        # wholesale, so candidate 1 dies on every update — the PATCH-122
        # rotation class. This candidate is the one that survives updates.
        # It participates only when the explicit caller is Codex; Claude never
        # sees or falls back to this authority.
        codex_candidates.append(
            Path.home() / ".allostat" / "codex" / "credential.env"
        )
    except (OSError, ValueError):
        pass

    # The recovery file beside the installed snapshot that is executing this
    # module participates in either harness. The remaining authority is scoped:
    # Claude's marketplace store OR Codex's durable store, never both. Mtime
    # can rank rotations only inside this boundary.
    candidates = local_candidates + (
        claude_candidates if harness == "claude" else codex_candidates
    )

    # (clock-sane, effective-mtime, -index, bearer). Ties keep earlier paths.
    now = time.time()
    found: list[tuple[int, float, int, str]] = []
    for index, env_path in enumerate(candidates):
        try:
            if not env_path.is_file():
                continue
            content = env_path.read_text(encoding="utf-8", errors="replace")
            m = _ENV_FILE_BEARER_PATTERN.search(content)
            if not m:
                continue
            bearer = m.group(1).strip()
            if bearer:
                mtime = env_path.stat().st_mtime
                sane = int(mtime <= now + _CREDENTIAL_FUTURE_SKEW_SECONDS)
                effective_mtime = mtime if sane else float("-inf")
                found.append((sane, effective_mtime, -index, bearer))
        except (OSError, ValueError):
            continue
    found.sort(reverse=True)
    ordered: list[str] = []
    for *_rank, bearer in found:
        if bearer not in ordered:
            ordered.append(bearer)
    return ordered


def _read_bearer_from_env_file(harness: str = "claude") -> str | None:
    """Return the preferred installed bearer, preserving the legacy API."""
    candidates = _read_bearer_candidates(harness)
    return candidates[0] if candidates else None


DEFAULT_ENDPOINT = "https://mcp.allostat.ai/mcp"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
# H-25: aggregate wall-clock ceiling across the whole retry loop. Without it
# the loop had no total budget — a server that keeps returning 429 + a large
# Retry-After (or 5xx) sleeps once PER attempt, stacking to ~40s of blocking
# inside a single hook fire (contradicts "hooks must never block"). This cap
# sits just above ONE honored Retry-After wait (capped at 10s per attempt, an
# existing tested contract) so a single recoverable rate-limit still succeeds,
# but the STACK of retries is aborted early — failing fast with the same error
# the loop raises on exhaustion. Override via ALLOSTAT_MCP_RETRY_BUDGET_SECONDS.
DEFAULT_RETRY_BUDGET_SECONDS = 12.0
MCP_PROTOCOL_VERSION = "2025-03-26"

# Env-var names for endpoint override. ALLOSTAT_MCP_URL is the canonical
# name per wrapper-mcp.md contract §1; ALLOSTAT_MCP_ENDPOINT is the older
# name kept for backwards compatibility. If both are set, URL wins.
URL_ENV = "ALLOSTAT_MCP_URL"
ENDPOINT_ENV_LEGACY = "ALLOSTAT_MCP_ENDPOINT"


def _resolve_endpoint() -> str:
    """Resolve the MCP endpoint URL from env, falling back to prod default.

    Order of precedence:
      1. ALLOSTAT_MCP_URL (canonical per contract — used for local dev / staging)
      2. ALLOSTAT_MCP_ENDPOINT (legacy name, pre-contract — still honored)
      3. DEFAULT_ENDPOINT (prod URL — the fallback that ships to customers)

    Validates that the resolved URL starts with http:// or https://. If
    malformed, falls back to DEFAULT_ENDPOINT with a stderr warning so the
    hook subprocess does not crash. Per wrapper-mcp.md §1 invariant: the
    default that ships MUST be prod — never bake localhost or staging.
    """
    import sys

    raw = os.environ.get(URL_ENV) or os.environ.get(ENDPOINT_ENV_LEGACY)
    if not raw:
        return DEFAULT_ENDPOINT
    raw = raw.strip()
    if raw.startswith("https://"):
        return raw
    if raw.startswith("http://"):
        # Security (ultraswarm): every request to this endpoint carries
        # 'Authorization: Bearer <per-user subscription token>'. Over plain http
        # to a remote host that bearer travels in cleartext — a MITM can capture
        # it and impersonate the paying user against prod. Only allow http for a
        # loopback host (local dev); reject any other http and fall back to the
        # prod https default rather than leak the token.
        if _is_loopback_http(raw):
            return raw
        print(
            f"allostat-mcp: warning - refusing plaintext http:// endpoint "
            f"({URL_ENV} or {ENDPOINT_ENV_LEGACY}) to a non-loopback host "
            f"(would transmit the subscription bearer in cleartext); using "
            f"default {DEFAULT_ENDPOINT}",
            file=sys.stderr,
        )
        return DEFAULT_ENDPOINT
    # Malformed URL — log and fall back.
    print(
        f"allostat-mcp: warning - env-var endpoint override "
        f"({URL_ENV} or {ENDPOINT_ENV_LEGACY}) does not start with "
        f"http:// or https://; using default {DEFAULT_ENDPOINT}",
        file=sys.stderr,
    )
    return DEFAULT_ENDPOINT


def _is_loopback_http(raw: str) -> bool:
    """True if `raw` is an http:// URL pointing at a loopback host — the only
    place carrying the bearer over cleartext http is acceptable (local dev).

    H-10: the loopback decision is made by PARSING, never by string
    prefix/suffix matching. The old predicate accepted any attacker-registrable
    DNS name that merely started with "127." (`127.evil.com`,
    `127.0.0.1.evil.com`) or ended with ".localhost" (`evil.localhost`), which
    let the per-user subscription Bearer be aimed at an attacker's cleartext
    host. Now:
      - a host that parses as an IP address (incl. bracketed IPv6) is loopback
        ONLY if `ipaddress.ip_address(...).is_loopback` — i.e. 127.0.0.0/8 or
        ::1, decided numerically, so `127.evil.com` (not an IP) never qualifies;
      - a non-IP host is loopback ONLY if it is EXACTLY "localhost" — no suffix
        match, so `evil.localhost` is rejected.
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal — only the exact name "localhost" is loopback.
        return host == "localhost"

def _plugin_json_path() -> Path:
    """Path to the plugin manifest (sibling of lib/)."""
    return Path(__file__).resolve().parent.parent / "plugin.json"


def _read_plugin_version() -> str:
    """Read the shipped plugin version from plugin.json at import time.

    Pre-release item 28: keeps the wire-visible version strings
    (User-Agent, initialize clientInfo) in sync with the version that
    actually ships in plugin.json instead of a second hand-frozen
    constant. Best-effort: any failure falls back to "0.0.0" so a
    hook subprocess never crashes on import.
    """
    try:
        data = json.loads(_plugin_json_path().read_text(encoding="utf-8"))
        version = str(data.get("version", "")).strip()
        if version:
            return version
    except (OSError, ValueError):
        pass
    return "0.0.0"


PLUGIN_VERSION = _read_plugin_version()

# Custom User-Agent so Cloudflare's WAF doesn't 403 us — the default
# Python-urllib UA gets blanket-blocked by many Cloudflare-fronted sites.
# Identifying as `allostat-mcp-wrapper/<version>` makes the traffic
# attributable + lets advisor whitelist the UA if needed.
USER_AGENT = f"allostat-mcp-wrapper/{PLUGIN_VERSION} (+https://allostat.ai)"


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """H-10 defense in depth: refuse to follow 3xx redirects on bearer
    requests. `urllib`'s default `HTTPRedirectHandler` retains the
    `Authorization` header across redirects — including cross-host and
    https->http downgrade — so a validated origin that answers 3xx could
    exfiltrate the subscription Bearer to an attacker-controlled target.
    Returning None from `redirect_request` makes urllib surface the 3xx as an
    HTTPError instead of transparently re-requesting, so the Bearer only ever
    reaches the pre-validated origin."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _urlopen_no_redirect(req, *, timeout):
    """Open a bearer-carrying request with redirects refused, WITHOUT mutating
    urllib's process-global opener.

    H-10 (concurrency): the previous implementation swapped urllib's GLOBAL
    opener via `install_opener(opener)` for the duration of the call and
    restored the prior opener in a finally. Under concurrency that swap RACES —
    a forced two-thread interleave issued one bearer request through the
    baseline (redirect-following) opener and left the process with another
    call's opener installed. We instead build a LOCAL `OpenerDirector` carrying
    the no-redirect handler and call its `.open()` directly, so redirect refusal
    is per-call and thread-safe and the global opener is never touched (other
    urllib users — update_check, activate — keep their default redirect
    behavior). This module-level function is the seam the test-suite mocks."""
    opener = urllib_request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=timeout)


def _is_sse_content_type(value: str) -> bool:
    """Match the HTTP media type case-insensitively, excluding parameters."""
    media_type = value.partition(";")[0].strip()
    return media_type.casefold() == "text/event-stream"


class MCPClientError(Exception):
    """Raised on any failure that prevents a successful MCP tool call."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


@dataclass
class MCPClientConfig:
    """Resolved client configuration. Default factory reads env vars."""

    endpoint: str = DEFAULT_ENDPOINT
    harness: str = "claude"
    bearer_token: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS
    # H-25: aggregate wall-clock budget across the whole retry loop (not per
    # attempt). Bounds total blocking so hooks never hang for tens of seconds.
    retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS
    request_id_prefix: str = "wrap"
    bearer_fallbacks: tuple[str, ...] = ()
    runtime: str = "hosted"

    @classmethod
    def from_env(
        cls,
        *,
        harness: str = "claude",
        token_env: str = "ALLOSTAT_MCP_TOKEN",
        timeout_env: str = "ALLOSTAT_MCP_TIMEOUT_SECONDS",
        retry_budget_env: str = "ALLOSTAT_MCP_RETRY_BUDGET_SECONDS",
    ) -> "MCPClientConfig":
        """Build a config from the wrapper plugin's standard env vars,
        preferring the plugin's on-disk .env over the OS env var.

        Endpoint resolution moved to _resolve_endpoint() so the precedence
        ALLOSTAT_MCP_URL > ALLOSTAT_MCP_ENDPOINT > DEFAULT_ENDPOINT applies
        consistently. (The ignored legacy `endpoint_env` kwarg was retired
        pre-release — zero callers across all three repos.)

        v0.2.3 PATCH-122 v4: hook subprocesses inherit OS env from the
        Claude Code process tree, which on Windows means they hold
        whatever credential variable was live when the terminal that
        spawned claude.exe first opened. Every installer run rotates the
        bearer via /install/resolve, but the in-memory env of any
        already-running shell stays stale — so the hook subprocess
        sends an invalidated bearer and gets 401 even though the
        installer wrote the new one to the User-scope registry. The
        `.mcp.json` is read by Claude Code itself (which we already
        rewrote to literal bearer in v0.2.2 PATCH-122 v1), but Python
        hook subprocesses go through this MCPClient code path with no
        knowledge of `.mcp.json`.

        Fix: read the plugin's own `.env` file first (written by the
        installer at <plugin_dir>/.env with the literal post-rotation
        bearer). Fall back to env var only when `.env` is absent or
        malformed (covers dev/test paths where the installer hasn't run).
        """
        if harness not in {"claude", "codex"}:
            raise ValueError(f"unknown Allostat harness: {harness!r}")
        from runtime_mode import runtime_mode
        if runtime_mode() == "local":
            return cls(endpoint="local://allostat", harness=harness, runtime="local")
        endpoint = _resolve_endpoint()
        installed = _read_bearer_candidates(harness)
        other_harness = "codex" if harness == "claude" else "claude"
        other_installed = _read_bearer_candidates(other_harness)
        scoped_process = (
            os.environ.get(f"ALLOSTAT_{harness.upper()}_MCP_TOKEN", "") or ""
        ).strip()
        generic_process = (os.environ.get(token_env, "") or "").strip()
        # The legacy process variable has no harness/account provenance. It is
        # safe only for a single-harness, fileless dev/legacy install. Once any
        # installed authority exists, never use it as a cross-account fallback.
        process_bearer = scoped_process or (
            generic_process if not installed and not other_installed else ""
        )
        ordered: list[str] = []
        for candidate in [*installed, process_bearer]:
            if candidate and candidate not in ordered:
                ordered.append(candidate)
        bearer = ordered[0] if ordered else ""

        cfg = cls(
            endpoint=endpoint,
            harness=harness,
            bearer_token=bearer,
            bearer_fallbacks=tuple(ordered[1:]),
        )
        if raw_timeout := os.environ.get(timeout_env):
            try:
                cfg.timeout_seconds = float(raw_timeout)
            except (TypeError, ValueError):
                pass
        if raw_budget := os.environ.get(retry_budget_env):
            try:
                cfg.retry_budget_seconds = float(raw_budget)
            except (TypeError, ValueError):
                pass
        return cfg


class MCPClient:
    """Streamable-HTTP MCP client over urllib.

    Each call_tool() issues a JSON-RPC `tools/call` POST. The server's
    Bearer-auth middleware accepts the token; rate-limit middleware
    enforces per-key limits with 429 + Retry-After on overrun.
    """

    def __init__(
        self,
        config: MCPClientConfig | None = None,
        *,
        harness: str | None = None,
    ):
        if config is not None and harness is not None and config.harness != harness:
            raise ValueError("MCP client harness disagrees with its configuration")
        self.config = (
            config
            if config is not None
            else MCPClientConfig.from_env(harness=harness or "claude")
        )
        if self.config.runtime not in {"local", "hosted"}:
            raise ValueError("Unknown Allostat execution adapter")
        self._local_client = None
        self._credential_candidates: list[str] = []
        for candidate in [self.config.bearer_token, *self.config.bearer_fallbacks]:
            if candidate and candidate not in self._credential_candidates:
                self._credential_candidates.append(candidate)
        self._credential_index = 0
        # MCP streamable-http: initialize MAY establish an mcp-session-id
        # (stateful server); subsequent tools/call requests pass it back in
        # the Mcp-Session-Id header. A STATELESS server returns no session-id
        # — that is valid, not an error: we just never send the header.
        # Each MCPClient instance is created fresh per hook subprocess (Claude
        # Code hooks are short-lived) so the wrapper pays one initialize +
        # tools/call per hook fire.
        self._session_id: str | None = None
        # True once initialize has succeeded (whether or not a session-id was
        # returned), so a stateless session (session_id stays None) isn't
        # re-initialized on every call.
        self._initialized: bool = False

    def _advance_bearer_after_401(self) -> bool:
        """Select the next distinct installed credential after auth rejection."""
        next_index = self._credential_index + 1
        if next_index >= len(self._credential_candidates):
            return False
        self._credential_index = next_index
        self.config.bearer_token = self._credential_candidates[next_index]
        self._session_id = None
        self._initialized = False
        return True

    # ------------------------------------------------------------------
    # Tool-call surface
    # ------------------------------------------------------------------

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool by name. Returns the result.content[0] parsed
        as JSON (the server's PillarToolResponse dict).

        Raises MCPClientError on any failure (auth, network, 5xx, etc.).
        """
        if self.config.runtime == "local":
            return self._local_call("call_tool", tool_name, arguments or {}, request_id=request_id)
        rid = request_id or f"{self.config.request_id_prefix}-{uuid.uuid4().hex[:12]}"
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        }
        body = self._post_with_retry(payload, request_id=rid)
        return self._parse_tool_result(body)

    def read_resource(
        self,
        uri: str,
        *,
        request_id: str | None = None,
    ) -> str:
        """Read an MCP resource by URI. Returns the resource's text content.

        Server resources include the question bank at
        `allostat://question-bank/calibration` (text/YAML).
        """
        if self.config.runtime == "local":
            return self._local_call("read_resource", uri, request_id=request_id)
        rid = request_id or f"{self.config.request_id_prefix}-{uuid.uuid4().hex[:12]}"
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "resources/read",
            "params": {"uri": uri},
        }
        body = self._post_with_retry(payload, request_id=rid)
        return self._parse_resource_result(body)

    def _local_call(self, method, *args, **kwargs):
        try:
            if self._local_client is None:
                from local_runtime import LocalClient
                self._local_client = LocalClient()
            return getattr(self._local_client, method)(*args, **kwargs)
        except Exception as exc:
            # Preserve the hook error contract. Never retry through HTTP or
            # attach operator-derived arguments to diagnostics on failure.
            raise MCPClientError(f"Local Allostat call failed ({type(exc).__name__})") from exc

    # ------------------------------------------------------------------
    # Low-level: POST with retry + auth
    # ------------------------------------------------------------------

    def _timeout_for(self, deadline: float | None) -> float:
        """Per-network-call timeout, clamped to the remaining aggregate budget.

        H-04: each urlopen previously used the full per-call timeout, so a
        single attempt (initialize + tool call = two network ops) could run
        ~2x the per-call timeout and overshoot the aggregate retry budget —
        which the loop only ever checked before retry *sleeps*, not around the
        network calls. Clamping every call to the remaining budget makes the
        budget a real wall-clock ceiling on network time. Floors at a small
        positive value so a near-exhausted budget fails fast rather than passing
        0 (non-blocking) or a negative to urlopen.
        """
        base = self.config.timeout_seconds
        if deadline is None:
            return base
        return max(0.05, min(base, deadline - time.monotonic()))

    def _post_with_retry(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """POST the JSON-RPC payload with retry/backoff. Returns the parsed
        JSON response body or raises MCPClientError."""
        last_err: MCPClientError | None = None
        # H-25: aggregate wall-clock ceiling for the whole loop. Elapsed is
        # measured from the first attempt; before any retry sleep we check that
        # the sleep would not push us past the deadline. If it would, we stop
        # retrying and fall through to raise the last error — the SAME failure
        # the loop already produces on exhaustion. This bounds total blocking
        # so a slow/rate-limiting server can never hang a hook for tens of
        # seconds. Uses monotonic time (immune to wall-clock adjustments).
        deadline = time.monotonic() + self.config.retry_budget_seconds
        for attempt in range(self.config.max_retries + 1):
            # H-04: don't start an attempt we have no budget left for; the
            # per-call clamp (_timeout_for) then keeps the attempt's network ops
            # inside the remaining budget too.
            if time.monotonic() >= deadline:
                break
            try:
                return self._post_once(
                    payload, request_id=request_id, deadline=deadline
                )
            except MCPClientError as e:
                last_err = e
                # Authentication fallback is bounded by the distinct installed
                # credentials, independently of transport max_retries. Only a
                # 401 advances; no other response sprays a second bearer.
                while e.status_code == 401 and self._advance_bearer_after_401():
                    if time.monotonic() >= deadline:
                        break
                    try:
                        return self._post_once(
                            payload, request_id=request_id, deadline=deadline
                        )
                    except MCPClientError as fallback_error:
                        e = fallback_error
                        last_err = e
                # Don't retry on 4xx (except 429 with Retry-After).
                if e.status_code is not None and 400 <= e.status_code < 500:
                    if e.status_code == 429 and e.retry_after is not None:
                        sleep_for = min(e.retry_after, 10.0)
                        # Would this sleep exceed the aggregate budget? Fail fast.
                        if time.monotonic() + sleep_for > deadline:
                            break
                        time.sleep(sleep_for)
                        continue
                    raise
                # 5xx + connection errors → exponential backoff
                if attempt < self.config.max_retries:
                    backoff = self.config.backoff_base * (2**attempt)
                    # Aggregate budget gate (same rationale as the 429 path).
                    if time.monotonic() + backoff > deadline:
                        break
                    time.sleep(backoff)
                else:
                    raise
        # Unreachable with sane config (loop always raises or returns), but a
        # bare assert vanishes under python -O and then raised `None`. Fail
        # loudly with a real error instead.
        if last_err is None:
            raise MCPClientError(
                "internal error: retry loop exited without a response or error "
                f"(max_retries={self.config.max_retries})"
            )
        raise last_err

    def _ensure_session(self, *, request_id: str, deadline: float | None = None) -> None:
        """Initialize an MCP session if we don't yet have a session-id.

        Server requires initialize → tools/call sequence. We capture the
        mcp-session-id header on initialize's 200 response (stateful server)
        and attach it as Mcp-Session-Id on subsequent calls. A stateless
        server returns no session-id — also valid; we just don't send the
        header. Guarded on `_initialized` (not on `_session_id`) so a
        stateless session isn't re-initialized on every call. Also skips when
        a session-id is already set (callers/tests may pre-seed one).
        """
        if self._initialized or self._session_id is not None:
            return
        init_payload = {
            "jsonrpc": "2.0",
            "id": f"{request_id}-init",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "allostat-mcp-wrapper",
                    "version": PLUGIN_VERSION,
                },
            },
        }
        body_bytes = json.dumps(init_payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-Request-ID": f"{request_id}-init",
            "User-Agent": USER_AGENT,
        }
        req = urllib_request.Request(
            self.config.endpoint,
            data=body_bytes,
            headers=headers,
            method="POST",
        )
        timeout = self._timeout_for(deadline)
        try:
            with _urlopen_no_redirect(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if _is_sse_content_type(content_type):
                    # M-10: parse the initialize SSE response INCREMENTALLY —
                    # return on the first complete frame instead of reading to
                    # EOF. A stateful server can answer initialize with a
                    # long-lived event-stream (the init result arrives as the
                    # first frame, then the connection stays open); the old
                    # resp.read() blocked until the server closed the stream or
                    # the socket timed out, stalling the hook on every session
                    # start. Mirrors the tool-response path in _post_once. The
                    # frame itself isn't needed downstream (session state comes
                    # from the response headers) — we consume just enough to
                    # confirm the correlated init RESPONSE arrived. M-07: match
                    # on the init request id so a notification/request the
                    # server interleaves first isn't mistaken for the result.
                    _parse_sse_stream(resp, init_payload["id"])
                else:
                    # Non-SSE (application/json) init: drain the body so the
                    # connection stays clean.
                    resp.read()
                sid = resp.headers.get("mcp-session-id") or resp.headers.get(
                    "Mcp-Session-Id"
                )
                # sid present → stateful server (store + send on later calls).
                # sid absent → stateless server: valid, leave _session_id None
                # and never send Mcp-Session-Id. Either way initialize
                # succeeded, so mark initialized to avoid re-initializing.
                self._session_id = sid or None
                self._initialized = True
        except urllib_error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise MCPClientError(
                f"HTTP {e.code} from MCP server during initialize",
                status_code=e.code,
                body=err_body[:500],
            )
        # Pre-release item 13: mirror the _post_once conversion. Without
        # these, a URLError/TimeoutError during initialize escaped raw —
        # skipping _post_with_retry's backoff and the hooks' mcp_error
        # observation (both key on MCPClientError).
        except urllib_error.URLError as e:
            raise MCPClientError(
                f"network error contacting MCP server during initialize: "
                f"{e.reason}",
                status_code=None,
            )
        except TimeoutError:
            raise MCPClientError(
                f"timeout contacting MCP server during initialize after "
                f"{timeout:.1f}s",
                status_code=None,
            )

    def _post_once(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One POST attempt. Raises MCPClientError on any failure."""
        if not self.config.bearer_token:
            raise MCPClientError(
                "Allostat MCP credential not configured — wrapper cannot reach "
                "the hosted MCP server.",
                status_code=401,
            )

        # Ensure we have an MCP session for non-initialize calls.
        if payload.get("method") != "initialize":
            self._ensure_session(request_id=request_id, deadline=deadline)

        body_bytes = json.dumps(redact_payload(payload)).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "X-Request-ID": request_id,
            "User-Agent": USER_AGENT,
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib_request.Request(
            self.config.endpoint,
            data=body_bytes,
            headers=headers,
            method="POST",
        )

        timeout = self._timeout_for(deadline)
        try:
            with _urlopen_no_redirect(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if _is_sse_content_type(content_type):
                    # M-10: parse SSE frames incrementally straight off the
                    # response stream. The old path read the WHOLE body to EOF
                    # (resp.read()) before parsing, so a long-lived / slow
                    # stream that had already delivered the first frame still
                    # blocked the hook until the server closed the connection.
                    # Reading line-by-line lets us return the correlated frame
                    # the instant it arrives. M-07: match on the request id we
                    # sent so an interleaved notification delivered ahead of the
                    # response can't be mistaken for the tool result.
                    return _parse_sse_stream(resp, payload.get("id"))
                resp_body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(resp_body)
                except json.JSONDecodeError as e:
                    raise MCPClientError(
                        f"non-JSON response body: {e}",
                        status_code=resp.status,
                        body=resp_body[:500],
                    )
        except urllib_error.HTTPError as e:
            retry_after = _parse_retry_after(e.headers.get("Retry-After"))
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise MCPClientError(
                f"HTTP {e.code} from MCP server",
                status_code=e.code,
                body=err_body[:500],
                retry_after=retry_after,
            )
        except urllib_error.URLError as e:
            raise MCPClientError(
                f"network error contacting MCP server: {e.reason}",
                status_code=None,
            )
        except TimeoutError:
            raise MCPClientError(
                f"timeout contacting MCP server after "
                f"{timeout:.1f}s",
                status_code=None,
            )

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_tool_result(self, body: dict[str, Any]) -> dict[str, Any]:
        """Extract the PillarToolResponse dict from the JSON-RPC envelope.

        Tool results land in `result.content[0].text` per MCP spec, encoded
        as JSON. Errors land in `error`.
        """
        if "error" in body:
            err = body["error"]
            raise MCPClientError(
                f"MCP error: {err.get('message', '(no message)')}",
                status_code=err.get("code"),
                body=json.dumps(err)[:500],
            )
        result = body.get("result", {})
        # FastMCP returns results as content blocks.
        content = result.get("content", [])
        if not content:
            return result
        first = content[0]
        # Text content blocks carry the JSON-encoded tool response.
        if first.get("type") == "text":
            text = first.get("text", "")
            try:
                return json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return {"text": text}
        return first

    def _parse_resource_result(self, body: dict[str, Any]) -> str:
        """Extract resource text content from the JSON-RPC envelope."""
        if "error" in body:
            err = body["error"]
            raise MCPClientError(
                f"MCP error: {err.get('message', '(no message)')}",
                status_code=err.get("code"),
            )
        contents = body.get("result", {}).get("contents", [])
        if not contents:
            return ""
        first = contents[0]
        return first.get("text", "")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _parse_retry_after(header_value: str | None) -> float | None:
    """Honor Retry-After in seconds-form (HTTP-date form ignored — rate
    limit headers from our middleware are always seconds)."""
    if not header_value:
        return None
    try:
        return float(header_value)
    except (TypeError, ValueError):
        return None


def _parse_sse_data_line(line: str) -> dict[str, Any] | None:
    """Return the parsed JSON of a single SSE `data:` line, or None if the
    line is not a data line, is an empty data line, or does not parse as JSON.

    Shared by the string-based (`_parse_sse`) and stream-based
    (`_parse_sse_stream`) parsers so both apply identical framing rules.
    """
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _is_correlated_response(frame: Any, expected_id: Any) -> bool:
    """True if `frame` is the JSON-RPC RESPONSE we are waiting for.

    M-07: the SSE parsers previously returned the FIRST parseable `data:`
    frame, whatever it was. MCP Streamable HTTP lets a server interleave
    server-to-client requests and notifications on the same event-stream, and
    such a message can be delivered BEFORE the response to our call. Returning
    it would let a notification be mistaken for the initialize/tool result.

    A JSON-RPC response is correlated to its request SOLELY by a matching `id`
    and carries a `result` or `error` (never a `method`). So a frame is our
    response only when:
      - it is a JSON object, and
      - it does NOT carry a `method` (that marks a server request/notification,
        which we skip even if it happens to reuse our id), and
      - its `id` equals the id we sent.

    `expected_id is None` disables correlation (first parseable frame wins) —
    the legacy contract kept only for direct `_parse_sse` unit callers that do
    not drive a full request/response cycle.
    """
    if expected_id is None:
        return True
    if not isinstance(frame, dict):
        return False
    if "method" in frame:
        return False
    return frame.get("id") == expected_id


def _parse_sse(body: str, expected_id: Any = None) -> dict[str, Any]:
    """Parse an SSE event-stream body into the correlated JSON-RPC response.

    MCP Streamable HTTP transport returns event-stream for stateful flows;
    each event is a `data: <json>` line. We return the first frame that is the
    response correlated to `expected_id` (M-07), skipping any interleaved
    notification/request frames delivered ahead of it. With `expected_id` None
    the first parseable frame wins (legacy).
    """
    for line in body.splitlines():
        frame = _parse_sse_data_line(line)
        if frame is not None and _is_correlated_response(frame, expected_id):
            return frame
    raise MCPClientError(
        "no correlated SSE response in response body",
        status_code=None,
        body=body[:500],
    )


def _parse_sse_stream(resp: Any, expected_id: Any = None) -> dict[str, Any]:
    """Parse an SSE event-stream INCREMENTALLY off the response object,
    returning the first complete JSON-RPC frame correlated to `expected_id`
    WITHOUT waiting for EOF.

    M-10: the previous SSE path read the entire body to EOF before parsing a
    single frame, so a slow or long-lived stream delayed availability of an
    already-delivered first message (and could stall a hook until the server
    closed the connection). Here we consume one line at a time from the
    file-like `resp` (a urllib HTTP response supports `readline()` yielding
    bytes) and return the moment the correlated `data:` frame arrives — never
    reading past it, so an un-closed stream no longer blocks.

    M-07: frames are matched by JSON-RPC id (`_is_correlated_response`). An
    interleaved server notification/request delivered ahead of the response is
    skipped and we keep reading, rather than mistaking it for the result.

    Framing rules match `_parse_sse` exactly. Falls back to a full `read()`
    if the response object exposes no usable `readline()`.
    """
    readline = getattr(resp, "readline", None)
    if not callable(readline):
        # No incremental reader available — degrade to the buffered parse.
        raw = resp.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return _parse_sse(raw, expected_id)

    collected: list[str] = []
    while True:
        raw_line = readline()
        if not raw_line:
            break  # EOF (empty bytes/str) — stream closed with no full frame.
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line
        collected.append(line)
        frame = _parse_sse_data_line(line.rstrip("\r\n"))
        if frame is not None and _is_correlated_response(frame, expected_id):
            return frame
    raise MCPClientError(
        "no correlated SSE response in response body",
        status_code=None,
        body="".join(collected)[:500],
    )
