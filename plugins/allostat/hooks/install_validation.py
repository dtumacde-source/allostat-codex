"""Install-validation checks shared between session-start.py and install.ps1.

PATCH-103 — 4-state taxonomy + 4-layer checks. Extends PATCH-102's 3-state
to distinguish "plugin not loaded" (the screenshot's silent-failure mode
where hooks never fire) from "MCP not registered" from "MCP registered
but failing to connect" from "server unreachable."

The silent-failure modes this module exists to prevent:

  1. Plugin entirely inert — marketplace.json malformed → Claude Code drops
     the plugin → no banner, no hooks, no skills, no commands. PATCH-102
     couldn't detect this (because its own banner can't fire to surface
     the state). Only the installer's Step 12b probe catches it.
  2. MCP not registered — plugin loaded but `claude mcp add` never ran.
     `mcp__allostat-mcp__*` tools missing from registry.
  3. MCP failed to connect — registration present but bearer rotated or
     transport broken. `mcp__allostat-mcp__*` calls return errors.
  4. Server unreachable — network/DNS/server-down.

Five state values, ordered by precedence (advisor 2026-05-16 review §2.1):

  Layer 1: plugin loaded?         → degraded_plugin_not_loaded if no
  Layer 2: MCP server registered? → degraded_mcp_not_registered if no
  Layer 3: server reachable?      → unreachable_server if no
  Layer 4: MCP connected (✓)?     → degraded_mcp_failed_connect if no
  MCP status determinate "connected"? → healthy if yes
  (otherwise — MCP check couldn't run) → unknown (audit #11: never
                                          report healthy on unverified state)

The precedence reads: "is the plugin loaded, is registration valid, is
the network up, is auth valid." Each step assumes the prior is true.
Symptom interpretations make sense at every level — never blame "MCP
failed to connect" when the server is actually unreachable upstream.

Layer 5 (authenticated tools/list probe) lives in install.ps1 / the
shared PowerShell helper, not here — it requires the bearer which the
PowerShell installer has in hand but the Python module does not. The
session-start banner doesn't need Layer 5 because the banner runs inside
a session where MCP tools are either callable or not (and the user finds
out the moment they're invoked).

CLI exit codes for the PowerShell installer to branch on:
  0 = healthy
  1 = degraded_plugin_not_loaded
  2 = degraded_mcp_not_registered
  3 = degraded_mcp_failed_connect
  4 = unreachable_server
  5 = unknown (CLI missing, subprocess error, etc.)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal


# R4 (2026-07-20) -- the second hardcoded production URL, and the last 4 of the
# 96. Kernel-counter attribution named the test
# (test_install_validation.py::TestValidateInstall::test_fix_hints_surface_doctor_command)
# and the parent-side resolver named the host (`mcp.allostat.ai`).
#
# Like the installer origin in update_check.py, this was hardcoded with no
# override, so no test could redirect it. It now resolves from the same
# variables the production MCP client already reads, which the suite's
# containment hard-sets to loopback -- so the redirect that was already in place
# for the client finally applies here too.
_DEFAULT_HEALTHZ_URL = "https://mcp.allostat.ai/healthz"


def _healthz_url() -> str:
    """Health endpoint, derived from the configured MCP endpoint.

    Resolved at call time: the suite sets the endpoint in a session fixture,
    which runs long after this module is imported by collection.
    """
    base = (
        os.environ.get("ALLOSTAT_MCP_URL")
        or os.environ.get("ALLOSTAT_MCP_ENDPOINT")
        or ""
    ).strip()
    if not base:
        return _DEFAULT_HEALTHZ_URL
    trimmed = base.rstrip("/")
    for suffix in ("/mcp", "/mcp/"):
        if trimmed.endswith(suffix.rstrip("/")):
            trimmed = trimmed[: -len(suffix.rstrip("/"))]
            break
    return f"{trimmed.rstrip('/')}/healthz"


# Kept for the call sites and tests that read the module constant.
_HEALTHZ_URL = _DEFAULT_HEALTHZ_URL
_DEFAULT_TIMEOUT_SECONDS = 2.0

# Test-only escape hatch: when this env var is set, validate_install()
# returns a healthy stub without hitting subprocess or the network.
# NEVER set in production.
_SKIP_ENV_VAR = "ALLOSTAT_SKIP_INSTALL_VALIDATION"


# ---------------------------------------------------------------------------
# `claude plugin list` parsing — multi-line block format:
#
#   Installed plugins:
#
#     ❯ allostat-mcp@local
#       Version: 0.1.3
#       Scope: user
#       Status: ✔ enabled
#
#     ❯ allostat@local
#       ...
#
# Parse strategy: split into per-plugin blocks at the `❯` marker, then
# scan the target plugin's block for its Status line. Tolerant of
# Version/Scope ordering and "Version: unknown" (current install state on
# operator's PC has this).
# ---------------------------------------------------------------------------

# Block start marker. Subprocess output normally comes through as UTF-8 `❯`
# (PATCH-103 v0.1.9 forces encoding="utf-8" on subprocess.run), but legacy
# environments / locale glitches can transcode to `>` (OEM cp437 fallback).
# Match either form so the regex doesn't silently fail on encoding drift.
_PLUGIN_BLOCK_START = "❯ "
_PLUGIN_BLOCK_START_ALT = "> "  # transcoded fallback
# The plugin's name WITHOUT its marketplace suffix.
#
# This was `allostat-mcp@local` until 2026-08-07, and `@local` is the dev's own
# marketplace. `claude plugin list` prints `allostat-mcp@<marketplace>`, so on
# any install that did not come from `@local` the block was never found, the
# check returned "not loaded", and the customer was told their install was
# broken — "Claude Code rejected its manifest", "re-run the installer" — while
# the hooks were demonstrably firing and the MCP server was connected.
#
# Found on the bench VM, where `claude plugin list` shows
# `allostat-mcp@allostat` (disabled) and `allostat-mcp@allostat-devfix`
# (enabled). It would fire for every marketplace customer: the one install
# shape that passed was the developer's.
_ALLOSTAT_PLUGIN_NAME = "allostat-mcp"

# Status icons: ✔/√ for enabled, ✘/× for disabled. UTF-8 normally; locale-
# transcoded forms also handled for defense in depth.
_PLUGIN_STATUS_ENABLED_RE = re.compile(r"Status:\s*(?:✔|√)\s*enabled", re.UNICODE)
_PLUGIN_STATUS_DISABLED_RE = re.compile(r"Status:\s*(?:✘|×|x)\s*disabled", re.UNICODE)


# ---------------------------------------------------------------------------
# `claude mcp list` parsing — single-line per server:
#
#   Checking MCP server health…
#
#   allostat-mcp: https://mcp.allostat.ai/mcp (HTTP) - ✓ Connected
#   plugin:context7:context7: npx -y @upstash/context7-mcp - ✓ Connected
#
# The allostat MCP server name appears in several known shapes:
#   - `allostat: <url>`                          (bare-server registration)
#   - `allostat-mcp: <url>`                      (plugin-bundled, no namespace)
#   - `plugin:allostat-mcp:allostat-mcp: <url>`  (Claude Code plugin namespace)
#
# Plus a fallback: `mcp.allostat.ai` URL anywhere in the output indicates
# our server is registered regardless of naming convention.
#
# Connection statuses observed in the wild:
#   - `✓ Connected`           — healthy
#   - `✗ Failed to connect`   — auth/transport problem
#   - `! Needs authentication`— bearer missing/rejected at handshake
# ---------------------------------------------------------------------------

_MCP_ENTRY_NAME_PART = r"(?:allostat|allostat-mcp|plugin:allostat-mcp:allostat-mcp)"
_MCP_ENTRY_OR_URL_RE = re.compile(
    r"(?:^|\s|:)" + _MCP_ENTRY_NAME_PART + r"\b|mcp\.allostat\.ai",
    re.MULTILINE,
)

# Connection-status indicators. The Claude CLI actually emits the HEAVY glyphs
# ✔ (U+2714) / ✘ (U+2718) — same as the Layer-1 plugin-status regex above — NOT
# the light ✓ (U+2713) / ✗ (U+2717). Match BOTH weights, plus the cp437
# locale-transcoded fallbacks (✓→√, ✗→×/x). (2026-07-06: matching only the light
# glyphs mis-scored a genuinely Connected server as 'failed' → false DEGRADED on
# every run — operator-reported. Regression-guarded by test_..._u2714/_u2718.)
_MCP_CONNECTED_RE = re.compile(
    r"(?:^|\s)" + _MCP_ENTRY_NAME_PART + r":[^\n]*?[-–]\s*(?:✓|✔|√)\s*Connected",
    re.MULTILINE,
)
_MCP_CONNECTED_BY_URL_RE = re.compile(
    r"mcp\.allostat\.ai[^\n]*?[-–]\s*(?:✓|✔|√)\s*Connected",
    re.MULTILINE,
)

# Failed-to-connect status (or needs-auth, treated as failed for our purposes
# -- bearer issue either way; same operator action: re-run installer)
_MCP_FAILED_RE = re.compile(
    r"(?:^|\s)" + _MCP_ENTRY_NAME_PART
    + r":[^\n]*?[-–]\s*(?:(?:✗|✘|×|x)\s*Failed|!\s*Needs\s*authentication)",
    re.MULTILINE,
)
_MCP_FAILED_BY_URL_RE = re.compile(
    r"mcp\.allostat\.ai[^\n]*?[-–]\s*(?:(?:✗|✘|×|x)\s*Failed|!\s*Needs\s*authentication)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Fix-hint strings — surfaced in the SessionStart banner AND the install.ps1
# INCOMPLETE message. The allostat-doctor command is surfaced everywhere
# because it's the cheapest recovery path for invisible-failure modes.
# ---------------------------------------------------------------------------

_DOCTOR_CMD_WIN = (
    "& \"$env:USERPROFILE\\.claude\\plugins\\marketplaces\\local\\allostat-mcp\\scripts\\allostat-doctor.ps1\""
)

# Pre-release item 14: hints were PowerShell-only — Mac/Linux customers
# were told to run a command their shell can't execute while
# scripts/allostat-doctor.sh shipped unreferenced. The posix command
# points at the doctor script inside THIS plugin's own install tree
# (computed from the module's location, so it survives layout moves).
_DOCTOR_CMD_POSIX = (
    f"bash \"{Path(__file__).resolve().parent.parent / 'scripts' / 'allostat-doctor.sh'}\""
)

_DOCTOR_CMD = _DOCTOR_CMD_WIN if sys.platform == "win32" else _DOCTOR_CMD_POSIX

_FIX_HINT_PLUGIN_NOT_LOADED = (
    "Plugin failed to load (Claude Code rejected its manifest). "
    "Re-run the installer from your install email link, or run the "
    f"health-check: {_DOCTOR_CMD}"
)
_FIX_HINT_MCP_NOT_REGISTERED = (
    "MCP server not registered with Claude Code. Re-run the installer "
    f"from your install email link, or try the health-check: {_DOCTOR_CMD}"
)
_FIX_HINT_MCP_FAILED_CONNECT = (
    "MCP server registered but failing to connect — your access bearer "
    "may have been rotated. Re-run the installer from your install email "
    f"link to refresh credentials. Health-check: {_DOCTOR_CMD}"
)
_FIX_HINT_SERVER_DOWN = (
    "Check your internet connection. If you're online, the Allostat "
    "server may be temporarily down — try again in a few minutes."
)
_FIX_HINT_PENDING_RESTART = (
    "An Allostat update just landed but this session is still running the "
    "previous version — the MCP connection will keep showing 'not connected' "
    "until you FULLY quit and reopen Claude Code. Do that now. If it still "
    "shows disconnected after a full restart, that's a real problem — re-run "
    f"the installer from your install email link. Health-check: {_DOCTOR_CMD}"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


State = Literal[
    "healthy",
    "pending_restart",
    "degraded_plugin_not_loaded",
    "degraded_mcp_not_registered",
    "degraded_mcp_failed_connect",
    "unreachable_server",
    "unknown",
]

McpStatus = Literal["connected", "failed", "missing"]


@dataclass
class InstallStatus:
    """Result of a composite install validation check.

    state values: see module docstring + State type alias.

    Layer fields:
      plugin_loaded     — True / False / None (None = check unavailable)
      mcp_status        — "connected" / "failed" / "missing" / None
      server_reachable  — True / False (always determinate)
      mcp_registered    — derived from mcp_status; True if "connected" or "failed", False if "missing"

    fix_hint is human-readable text the banner / installer renders verbatim.
    detail carries raw layer outputs for dev debugging via -Verbose / doctor.
    """
    state: State
    plugin_loaded: bool | None
    mcp_registered: bool | None  # backward-compat: derived from mcp_status
    mcp_status: McpStatus | None
    server_reachable: bool
    fix_hint: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Layer 1 — plugin loaded
# ---------------------------------------------------------------------------


def check_plugin_loaded() -> bool | None:
    """Return whether `allostat-mcp@local` shows enabled+loaded in
    `claude plugin list` output.

    Returns:
        True  — plugin is enabled (loaded by Claude Code's plugin loader)
        False — plugin present but explicitly disabled, OR plugin missing
                entirely from listing
        None  — `claude` CLI unavailable / timed out / non-zero exit;
                state is unknown and the caller should not fail loudly
    """
    try:
        rc, stdout, _stderr = _run_claude_plugin_list()
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    if rc != 0:
        return None

    blocks = _extract_plugin_blocks(stdout, _ALLOSTAT_PLUGIN_NAME)
    if not blocks:
        return False  # plugin not in listing → not loaded
    # One ENABLED instance is enough. The same plugin legitimately appears
    # under several marketplaces at once — the bench has it disabled under
    # `@allostat` and enabled under `@allostat-devfix`, which is exactly how
    # you test a candidate build — and in that state the plugin IS loaded.
    if any(_PLUGIN_STATUS_ENABLED_RE.search(b) for b in blocks):
        return True
    if any(_PLUGIN_STATUS_DISABLED_RE.search(b) for b in blocks):
        return False
    # Blocks found but no status line — conservative: treat as not loaded
    return False


def _extract_plugin_blocks(output: str, plugin_name: str) -> list[str]:
    """Every block for `plugin_name`, across all marketplaces.

    `claude plugin list` prints one block per installed instance, headed
    `❯ <name>@<marketplace>`. The same plugin can legitimately be installed
    from more than one marketplace at once, so this matches on the name and
    returns them all rather than the first (or, before 2026-08-07, only the
    one whose marketplace happened to be hardcoded).

    A block runs from its marker to the next marker or end of output.
    Tolerates both UTF-8 (`❯ `) and locale-transcoded (`> `) markers —
    claude.exe emits UTF-8 but legacy environments may downconvert.
    """
    starts: list[int] = []
    for marker_char in (_PLUGIN_BLOCK_START, _PLUGIN_BLOCK_START_ALT):
        pattern = re.compile(
            re.escape(marker_char) + re.escape(plugin_name) + r"(?:@\S*)?\s*$",
            re.MULTILINE,
        )
        starts.extend(m.start() for m in pattern.finditer(output))
    if not starts:
        return []

    # All block boundaries, so a block ends at the NEXT plugin of any name.
    boundaries = sorted({
        m.start()
        for marker_char in (_PLUGIN_BLOCK_START, _PLUGIN_BLOCK_START_ALT)
        for m in re.finditer(re.escape(marker_char), output)
    })
    blocks: list[str] = []
    for start in sorted(set(starts)):
        end = next((b for b in boundaries if b > start), len(output))
        blocks.append(output[start:end])
    return blocks


def _extract_plugin_block(output: str, plugin_name: str) -> str | None:
    """First block for `plugin_name`, or None. Retained for callers that want
    a single block; `check_plugin_loaded` uses the plural form because one
    enabled instance among several is still loaded."""
    blocks = _extract_plugin_blocks(output, plugin_name)
    return blocks[0] if blocks else None


# ---------------------------------------------------------------------------
# Layer 2 + 3 — MCP registration and connection status
# ---------------------------------------------------------------------------


def check_mcp_status() -> McpStatus | None:
    """Parse `claude mcp list` for the allostat entry's connection status.

    Returns:
        "connected" — entry present, ✓ Connected
        "failed"    — entry present, ✗ Failed to connect or ! Needs authentication
        "missing"   — entry not in listing
        None        — CLI unavailable / errored
    """
    try:
        rc, stdout, _stderr = _run_claude_mcp_list()
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    if rc != 0:
        return None

    if _MCP_CONNECTED_RE.search(stdout) or _MCP_CONNECTED_BY_URL_RE.search(stdout):
        return "connected"
    if _MCP_FAILED_RE.search(stdout) or _MCP_FAILED_BY_URL_RE.search(stdout):
        return "failed"
    if _MCP_ENTRY_OR_URL_RE.search(stdout):
        # Name appears but no clear status — conservative: treat as failed
        return "failed"
    return "missing"


# ---------------------------------------------------------------------------
# Layer 4 — server reachable
# ---------------------------------------------------------------------------


def check_server_reachable(timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Return whether the Allostat MCP server's host responds to a HEAD
    request within the timeout.

    A 4xx / 5xx response still counts as reachable — the server is up,
    it just rejected the request shape. Only a connection-level failure
    (refused / timeout / DNS) counts as unreachable.

    urllib.request semantics: urlopen RAISES HTTPError on 4xx/5xx
    rather than returning a response. HTTPError IS a subclass of
    URLError, so we MUST catch it BEFORE the URLError handler — otherwise
    a 401/403/404 (server up, request rejected) gets misclassified as
    unreachable. PATCH-102 Surface Pro smoke test hit this exact bug:
    a 401 from /healthz fell through URLError → returned False → banner
    declared "unreachable" when the server was actually serving.
    """
    try:
        response = _http_head(_healthz_url(), timeout=timeout)
    except urllib.request.HTTPError:
        return True
    except (ConnectionRefusedError, TimeoutError, socket.timeout, socket.gaierror):
        return False
    except urllib.request.URLError:
        return False
    except Exception:
        return False

    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    return status is not None


# ---------------------------------------------------------------------------
# Composite check — validate_install()
# ---------------------------------------------------------------------------


def validate_install() -> InstallStatus:
    """Run all four layers and return a composite InstallStatus.

    Precedence (advisor 2026-05-16 review §2.1):
      1. Plugin loaded?  If no → degraded_plugin_not_loaded
      2. MCP registered? If no → degraded_mcp_not_registered
      3. Server reachable? If no → unreachable_server
      4. MCP connected (✓)? If no → degraded_mcp_failed_connect
      5. Otherwise → healthy

    Plugin-loaded fail-soft: if check_plugin_loaded() returns None
    (CLI unavailable), proceed to MCP checks as before — don't surface
    a degraded_plugin_not_loaded state on insufficient evidence.

    The benign in-session-update case (a Layer-4 failure that is really just
    "you updated but haven't fully restarted") is handled OUT of band by
    apply_update_pending(), applied by the SessionStart caller once it has the
    version_skew signal — kept separate so this function stays a pure, ordering-
    independent snapshot of the four layers.
    """
    from runtime_mode import is_local
    if is_local():
        try:
            from local_runtime import require_ready
            require_ready()
        except Exception as exc:
            return InstallStatus(
                state="degraded_mcp_failed_connect", plugin_loaded=True,
                mcp_registered=None, mcp_status=None, server_reachable=False,
                fix_hint="Repair the local Allostat runtime and its dependencies.",
                detail={"runtime": "local", "error_class": type(exc).__name__},
            )
        # This proves in-process execution, not the host's separate MCP
        # registration. Leave those fields unknown rather than fabricate it.
        return InstallStatus(
            state="healthy", plugin_loaded=True, mcp_registered=None,
            mcp_status=None, server_reachable=True, fix_hint=None,
            detail={"runtime": "local"},
        )
    # Test-only escape hatch.
    if os.environ.get(_SKIP_ENV_VAR):
        return InstallStatus(
            state="healthy",
            plugin_loaded=True,
            mcp_registered=True,
            mcp_status="connected",
            server_reachable=True,
            fix_hint=None,
            detail={"escape_hatch": True},
        )

    plugin_loaded = check_plugin_loaded()
    mcp_status = check_mcp_status()
    server_reachable = check_server_reachable()
    mcp_registered = (mcp_status != "missing") if mcp_status is not None else None

    detail = {
        "plugin_loaded": plugin_loaded,
        "mcp_status": mcp_status,
        "server_reachable": server_reachable,
    }

    # Layer 1 — plugin loaded?
    if plugin_loaded is False:
        return InstallStatus(
            state="degraded_plugin_not_loaded",
            plugin_loaded=False,
            mcp_registered=mcp_registered,
            mcp_status=mcp_status,
            server_reachable=server_reachable,
            fix_hint=_FIX_HINT_PLUGIN_NOT_LOADED,
            detail=detail,
        )

    # Layer 2 — MCP registered?
    if mcp_status == "missing":
        return InstallStatus(
            state="degraded_mcp_not_registered",
            plugin_loaded=plugin_loaded,
            mcp_registered=False,
            mcp_status="missing",
            server_reachable=server_reachable,
            fix_hint=_FIX_HINT_MCP_NOT_REGISTERED,
            detail=detail,
        )

    # Layer 3 — server reachable?
    if not server_reachable:
        return InstallStatus(
            state="unreachable_server",
            plugin_loaded=plugin_loaded,
            mcp_registered=mcp_registered,
            mcp_status=mcp_status,
            server_reachable=False,
            fix_hint=_FIX_HINT_SERVER_DOWN,
            detail=detail,
        )

    # Layer 4 — MCP connected (✓)?
    if mcp_status == "failed":
        return InstallStatus(
            state="degraded_mcp_failed_connect",
            plugin_loaded=plugin_loaded,
            mcp_registered=True,
            mcp_status="failed",
            server_reachable=True,
            fix_hint=_FIX_HINT_MCP_FAILED_CONNECT,
            detail=detail,
        )

    # Health requires POSITIVE proof. Having excluded "missing" (layer 2) and
    # "failed" (layer 4), mcp_status is now either "connected" or None. A
    # determinate "connected" transitively proves the whole chain (plugin
    # loaded + registered + reachable + auth-valid) → genuinely healthy.
    #
    # If mcp_status is None the MCP check could not run (e.g. `claude` CLI
    # unavailable / errored) — we have NOT proven health. Audit #11
    # (2026-07-06): never report `healthy` on unverified validation. Return an
    # explicit neutral `unknown` so the banner admits the gap (the banner
    # renders any unrecognized state as its neutral "status unknown" line) and
    # validate_install_cached never stores a false-healthy (it caches "healthy"
    # only). This replaces the prior fall-through that reported healthy whenever
    # layers 1/2 were fail-soft None.
    if mcp_status is None:
        return InstallStatus(
            state="unknown",
            plugin_loaded=plugin_loaded,
            mcp_registered=mcp_registered,
            mcp_status=None,
            server_reachable=server_reachable,
            fix_hint=None,
            detail={**detail, "reason": "validation_unavailable"},
        )

    # mcp_status == "connected" — end-to-end health proven.
    return InstallStatus(
        state="healthy",
        plugin_loaded=plugin_loaded,  # True or None (plugin check fail-soft)
        mcp_registered=True,
        mcp_status="connected",
        server_reachable=True,
        fix_hint=None,
        detail=detail,
    )


def apply_update_pending(status: InstallStatus, update_pending: bool | None) -> InstallStatus:
    """Downgrade a benign in-session-update Layer-4 failure to `pending_restart`.

    Advisor 2026-07-06 validation-honesty fix. When an update has landed on disk
    but this session is still running the previous version (the SessionStart
    caller detects this via version_skew and passes update_pending=True), a
    `degraded_mcp_failed_connect` is BENIGN: the stale process still owns the old
    MCP connection and a FULL restart clears it. Downgrade it to `pending_restart`
    (a WARN, exit 0) carrying a loud "fully restart" hint.

    NON-STICKY by construction — no marker needed. version_skew only reports skew
    on a *continuation* of a stale process; a full restart yields a fresh process
    with update_pending=False. So on the very next session after the prescribed
    restart, a STILL-failing Layer 4 is passed update_pending=False and stays
    `degraded_mcp_failed_connect` (real). "Pending restart, first observation" and
    "still red after the restart you were told to do" are thereby distinguished by
    the skew signal itself, not by this function.

    Only ever downgrades degraded_mcp_failed_connect; every other state (healthy,
    plugin-not-loaded, not-registered, unreachable) passes through untouched — an
    update-pending session with a genuinely unreachable server is still unreachable.
    Pure + ordering-independent: the caller applies it after it has the skew signal.
    """
    if update_pending and status.state == "degraded_mcp_failed_connect":
        return replace(
            status,
            state="pending_restart",
            fix_hint=_FIX_HINT_PENDING_RESTART,
            detail={**status.detail, "update_pending": True},
        )
    return status


# ---------------------------------------------------------------------------
# P2 (pillar-wiring 2026-06-05) — SessionStart latency cache
# ---------------------------------------------------------------------------


def validate_install_cached(
    *,
    cache_dir=None,
    ttl_seconds: float = 3600.0,
    now: float | None = None,
    validate_fn=None,
    plugin_json_path=None,
) -> InstallStatus:
    """Cache the HEALTHY validate_install() result to avoid the ~7-9s of `claude`
    CLI subprocess spawns (`plugin list` + `mcp list`) on every SessionStart
    (measured p99 9.2s; ~7-9s of it is two Node cold-starts, uncached).

    Install state is GLOBAL (per-machine, not per-project), so the cache is
    user-global (default `~/.claude`), keyed on plugin.json mtime + a TTL:
      - Only "healthy" results are cached. Degraded states re-check every session
        so a fix — or a new breakage — surfaces promptly.
      - Cache invalidates on TTL expiry OR plugin.json mtime change (a plugin
        install/update). Any cache read/parse error falls through to a live
        validate_install (fail-open: correctness over speed).

    Safe to cache: the banner's install check is a startup HINT — real MCP
    failures surface the moment a tool is invoked mid-session (see the module
    docstring's Layer-5 note), so a bounded-stale "healthy" never hides a problem
    the operator actually hits.
    """
    from runtime_mode import is_local
    if is_local():
        return validate_install()
    import time
    from pathlib import Path

    if validate_fn is None:
        validate_fn = validate_install
    if now is None:
        now = time.time()
    if cache_dir is None:
        cache_dir = Path.home() / ".claude"
    if plugin_json_path is None:
        plugin_json_path = Path(__file__).resolve().parent.parent / "plugin.json"

    try:
        plugin_mtime = Path(plugin_json_path).stat().st_mtime
    except OSError:
        plugin_mtime = 0.0  # Best-effort: no plugin.json → TTL-only invalidation

    cache_path = Path(cache_dir) / ".allostat_install_validation_cache.json"

    # Fast path — a fresh, healthy, mtime-matched cache skips both subprocesses.
    try:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("state") == "healthy"
                and isinstance(cached.get("cached_at"), (int, float))
                and (now - cached["cached_at"]) < ttl_seconds
                and cached.get("plugin_mtime") == plugin_mtime
                and isinstance(cached.get("status"), dict)
            ):
                return InstallStatus(**cached["status"])
    except Exception:
        # Best-effort: any cache read/parse error → fall through to live validate.
        pass

    status = validate_fn()

    # Cache only healthy results (degraded states must re-check next session).
    if getattr(status, "state", None) == "healthy":
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({
                    "state": "healthy",
                    "cached_at": now,
                    "plugin_mtime": plugin_mtime,
                    "status": asdict(status),
                }),
                encoding="utf-8",
            )
        except OSError:
            # Best-effort: cache write is non-fatal; validation already ran.
            pass
    return status


# ---------------------------------------------------------------------------
# Internal helpers — patched in tests
# ---------------------------------------------------------------------------


def _run_claude_plugin_list() -> tuple[int, str, str]:
    """Invoke `claude plugin list` and return (returncode, stdout, stderr).

    Uses a 15-second timeout — plugin listing is fast (no network calls,
    just reads installed_plugins.json + marketplace manifests); the margin
    covers slow cold-start of the claude CLI itself.

    PATCH-103 v0.1.9: explicit encoding="utf-8" + errors="replace".
    On Windows, Python's default subprocess text decoding uses cp1252 (the
    system locale). claude.exe emits UTF-8 — chars like ❯ (0xE2 0x9D 0xAF)
    or ✔ (0xE2 0x9C 0x94) contain bytes (0x9D, 0x9C) that cp1252 maps to
    undefined → UnicodeDecodeError in the reader thread → silent failure.
    Forcing UTF-8 decode + replace-on-error makes capture robust.

    H-31: resolve `claude` via shutil.which (honors PATHEXT → finds
    claude.cmd/.bat npm shims on Windows) before invoking. A bare "claude"
    hands CreateProcess a name it only extends with .exe, never .cmd/.bat,
    so an npm shim raises FileNotFoundError → silent "CLI unavailable"
    degradation. When which() returns None the CLI truly is absent, so we
    raise FileNotFoundError to preserve that (caught upstream) degradation.
    """
    claude = shutil.which("claude")
    if claude is None:
        raise FileNotFoundError("claude CLI not found on PATH")
    proc = subprocess.run(
        [claude, "plugin", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_claude_mcp_list() -> tuple[int, str, str]:
    """Invoke `claude mcp list` and return (returncode, stdout, stderr).

    15-second timeout — `claude mcp list` pings each registered server to
    report connection status, so realistic latency is several seconds
    (measured 4.3s on the PATCH-102 Surface Pro smoke test with 5 servers).

    PATCH-103 v0.1.9: explicit encoding="utf-8" — see _run_claude_plugin_list.

    H-31: resolve `claude` via shutil.which before invoking — see
    _run_claude_plugin_list for the Windows .cmd/.bat shim rationale.
    """
    claude = shutil.which("claude")
    if claude is None:
        raise FileNotFoundError("claude CLI not found on PATH")
    proc = subprocess.run(
        [claude, "mcp", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _http_head(url: str, *, timeout: float):
    """Send a HEAD request and return the response object.

    Wrapping urllib.request.urlopen so tests can patch a single seam.
    """
    request = urllib.request.Request(url, method="HEAD")
    return urllib.request.urlopen(request, timeout=timeout)


# ---------------------------------------------------------------------------
# CLI entry — `python -m install_validation` for the PowerShell installer
# ---------------------------------------------------------------------------


_EXIT_CODE_BY_STATE: dict[str, int] = {
    "healthy": 0,
    # pending_restart is a WARN, not a failure: automation may treat it as pass
    # (exit 0), but the human MUST be told to fully restart (loud fix_hint). It
    # is non-sticky — if the connection is still red after the prescribed
    # restart, the next check reports real degraded_mcp_failed_connect (exit 3).
    "pending_restart": 0,
    "degraded_plugin_not_loaded": 1,
    "degraded_mcp_not_registered": 2,
    "degraded_mcp_failed_connect": 3,
    "unreachable_server": 4,
    "unknown": 5,
}


def _cli_main() -> int:
    """Run validate_install() and return the appropriate exit code.

    Also prints a single line of JSON to stdout describing the result so
    the installer can show a sensible message.

    Exit codes:
      0 = healthy
      1 = degraded_plugin_not_loaded
      2 = degraded_mcp_not_registered
      3 = degraded_mcp_failed_connect
      4 = unreachable_server
      5 = unknown future state
    """
    status = validate_install()
    print(json.dumps(status.as_dict()))
    return _EXIT_CODE_BY_STATE.get(status.state, 5)


if __name__ == "__main__":
    sys.exit(_cli_main())
