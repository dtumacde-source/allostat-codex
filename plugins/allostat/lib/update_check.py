"""S3 Leg 3a — detect a newer Allostat version is available; surface a notice.

The 'notify' half of auto-update (the 'fetch' half is Leg 3b, gated).

'Latest available' comes from one atomically-published release manifest:
  https://installer.allostat.ai/install/bundle/release.version
The JSON binds the release version to its immutable versioned bundle URL and
SHA-256. It is committed only after server, bundle, and both marketplaces agree.

SessionStart cost is bounded: the wrapper fetches at most once per TTL window
(default 24h), caching the result in <state_dir>/update_check.json. On a cache
hit it does no network at all. Best-effort throughout: any failure falls back to
the cached last-known value, or to no notice — never raises, never blocks hard
(short timeout).

Pairs with E8 (`version_skew`): E8 = "you upgraded on disk but didn't restart";
this = "a newer version exists, run the installer". E8's restart nudge takes
priority over this in the banner (you restart what you have before fetching more).
Honors the install-time consent (`consent.update_notifications_enabled`) and the
`ALLOSTAT_UPDATE_NOTICE=0` opt-out.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import consent  # wrapper lib
import version_skew  # wrapper lib (reuse the authoritative installed-version read)

# R4 (2026-07-20) -- THIS LINE WAS THE 96.
#
# Under containment the wrapper suite made 96 attempts to reach a non-loopback
# destination while reporting green. Child-process instrumentation named the
# host on the first run: `installer.allostat.ai:443`, from here, once per
# SessionStart hook subprocess a test launched.
#
# The URL was hardcoded with no override, so no test could redirect it and
# nothing in the suite could avoid it. Every developer run before the
# containment boundary existed therefore DID reach the production installer
# host -- containment is what stopped it, and containment is newer than this
# line.
#
# `ALLOSTAT_INSTALLER_BASE_URL` is not a new invention and not a security
# switch: `scripts/repair-install.sh` already honours exactly this variable for
# staging installs. Making the update check honour it too is consistency, and it
# gives the suite a loopback target instead of a production one.
_DEFAULT_INSTALLER_BASE = "https://installer.allostat.ai"


def _installer_base_url() -> str:
    """Installer origin, overridable for staging and for the test suite."""
    base = (os.environ.get("ALLOSTAT_INSTALLER_BASE_URL") or "").strip()
    return (base or _DEFAULT_INSTALLER_BASE).rstrip("/")


def _latest_url() -> str:
    return f"{_installer_base_url()}/install/bundle/release.version"


# Kept as a module constant for the several tests and call sites that read it.
# Resolved at import so an override set before import is honoured; callers that
# need late resolution use `_latest_url()`.
_LATEST_URL = _latest_url()
_CACHE_FILENAME = "update_check.json"
_DEFAULT_TTL_SECONDS = 86400  # at most one network check per day
_FETCH_TIMEOUT = 4.0
_USER_AGENT = "allostat-mcp-wrapper-updatecheck"


@dataclass
class UpdateStatus:
    installed: str | None
    latest: str | None
    update_available: bool


def _parse_version(v) -> tuple:
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(-1)  # junk sorts low
    return tuple(out)


def is_newer(latest, installed) -> bool:
    if not latest or not installed:
        return False
    try:
        return _parse_version(latest) > _parse_version(installed)
    except Exception:
        return False


def _notices_disabled() -> bool:
    raw = os.environ.get("ALLOSTAT_UPDATE_NOTICE")
    return raw is not None and raw.strip().lower() in ("0", "false", "no", "off")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_path(state_dir: Path) -> Path:
    return state_dir / _CACHE_FILENAME


def _read_cache(state_dir: Path | None) -> dict:
    if state_dir is None:
        return {}
    try:
        data = json.loads(_cache_path(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(state_dir: Path | None, latest: str) -> None:
    if state_dir is None:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(state_dir).write_text(
            json.dumps({"latest": latest, "checked_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: a failed cache write just means we re-check next time.
        # Silenced intentionally; no correctness impact, banner must not break.
        pass


def _fetch_latest() -> str | None:
    try:
        # Resolved at call time, not import time: the suite's containment sets
        # the override in a session fixture, which runs after this module has
        # already been imported by collection.
        req = urllib.request.Request(
            _latest_url(), headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
            document = json.loads(r.read(4096).decode("utf-8"))
        if not isinstance(document, dict) or document.get("schema") != 1:
            return None
        version = document.get("version")
        bundle_url = document.get("bundle_url")
        bundle_sha = document.get("bundle_sha256")
        if not isinstance(version, str) or not version:
            return None
        parts = version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        expected_url = (
            f"{_installer_base_url()}/install/bundle/allostat-mcp-{version}.zip"
        )
        if bundle_url != expected_url:
            return None
        if (
            not isinstance(bundle_sha, str)
            or len(bundle_sha) != 64
            or any(char not in "0123456789abcdef" for char in bundle_sha)
        ):
            return None
        return version
    except Exception:
        return None
    return None


def _cache_fresh(cache: dict, ttl_seconds: int) -> bool:
    ts = cache.get("checked_at")
    if not ts:
        return False
    try:
        when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (_now() - when).total_seconds() < ttl_seconds


def _latest_throttled(state_dir: Path | None, ttl_seconds: int) -> str | None:
    cache = _read_cache(state_dir)
    if _cache_fresh(cache, ttl_seconds):
        return cache.get("latest")
    fetched = _fetch_latest()
    if fetched:
        _write_cache(state_dir, fetched)
        return fetched
    # Fetch failed -> fall back to last-known cached value (may be stale).
    return cache.get("latest")


def evaluate(
    state_dir: Path | None,
    plugin_root: Path,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> UpdateStatus | None:
    """Best-effort. Returns an UpdateStatus, or None when disabled / no data."""
    if _notices_disabled():
        return None
    from runtime_mode import is_local
    if is_local() and not os.environ.get("ALLOSTAT_INSTALLER_BASE_URL", "").strip():
        # Local preview builds have no release feed yet. Never recommend the
        # old hosted bundle as their update. An explicit feed retains the
        # existing consent, caching, and version comparison implementation.
        return None
    try:
        if not consent.update_notifications_enabled():
            return None
        installed = version_skew.read_registered_version(plugin_root)
        latest = _latest_throttled(state_dir, ttl_seconds)
        if not latest:
            return None
        return UpdateStatus(installed=installed, latest=latest,
                            update_available=is_newer(latest, installed))
    except Exception:
        return None


def _codex_marketplace_install(plugin_root: str | None = None) -> bool:
    """True when this wrapper runs as a Codex MARKETPLACE plugin.

    Discriminator: Codex hook processes receive CLAUDE_PLUGIN_ROOT/PLUGIN_ROOT
    pointing into `~/.codex/plugins/…` (local-marketplace cache) or
    `~/.codex/.tmp/marketplaces/…` (git-marketplace snapshot). The legacy
    installer layout lives under `~/.allostat/codex/` and sets neither.
    `plugin_root` overrides the env read so callers/tests stay deterministic.
    """
    root = (
        plugin_root
        if plugin_root is not None
        else (
            os.environ.get("CLAUDE_PLUGIN_ROOT")
            or os.environ.get("PLUGIN_ROOT")
            or ""
        )
    )
    normalized = root.replace("\\", "/")
    return "/.codex/plugins/" in normalized or "/.codex/.tmp/marketplaces/" in normalized


def format_banner_line(
    status: UpdateStatus | None,
    harness: str | None = None,
    *,
    windows: bool | None = None,
    mac: bool | None = None,
    codex_plugin_root: str | None = None,
) -> str | None:
    """One line naming the update command that actually works on THIS harness.

    Until 2026-08-04 this said "Re-run the installer to upgrade" to everyone. A
    Codex customer typically no longer has install.py on disk, so the notice
    named an action they could not take; a Claude Code customer was never told
    the one-word command they do have.

    The Codex branch also used to emit PowerShell (`irm ... -OutFile`) for every
    OS, which is not a command macOS/Linux can run at all. `windows` selects
    which OS-specific fetch command to show; None (the default) resolves to
    `os.name == "nt"` at call time -- same pattern as
    codex_wiring._codex_command_token's `windows_shell`. The production call
    site (session-start.py) never passes it, so resolution always happens on
    the customer's own machine.
    """
    if not status or not status.update_available:
        return None
    installed = status.installed or "your version"
    head = f"ℹ Allostat update available: v{installed} → v{status.latest}."
    if harness == "claude":
        # M02 (2026-08-06 swarm): `/allostat update` is Windows-only — its own
        # doc says so (wrapper/commands/allostat.md) — yet this branch told
        # every Claude customer to run it. macOS/Linux get the code-free
        # installer refresh (the same recovery repair-install.sh routes to; it
        # reuses the saved bearer and never consumes an install code). `mac`
        # mirrors `windows`: None resolves on the customer's machine at call
        # time, and the production call site passes neither.
        on_windows = windows if windows is not None else (os.name == "nt")
        if on_windows:
            return f"{head} Run `/allostat update`."
        on_mac = mac if mac is not None else (sys.platform == "darwin")
        script = "install/mac/install.sh" if on_mac else "install/linux/install.sh"
        return (
            f"{head} Update with:\n"
            f"   curl -fsSL https://installer.allostat.ai/{script} | bash -s -- --refresh\n"
            "   Then fully quit and reopen Claude Code to load the new version."
        )
    if harness == "codex":
        # Codex marketplace plan Phase 3 (2026-08-08): a marketplace install
        # updates with ONE command — the plugin runs from the marketplace
        # snapshot, so refreshing the snapshot IS the update (proven live,
        # 0.1.0→0.2.0, hub design/20260808_codex_marketplace_distribution_plan.md
        # Spike B). Legacy installer-wired installs are directed onto that
        # same public route; the retired standalone installer is never
        # advertised as an update mechanism.
        if _codex_marketplace_install(codex_plugin_root):
            return (
                f"{head} Update with:\n"
                "   codex plugin marketplace upgrade\n"
                "   Then restart Codex. If Codex asks, re-trust the hooks via /hooks."
            )
        return (
            f"{head} Migrate to the marketplace with:\n"
            "   codex plugin marketplace add dtumacde-source/allostat-codex\n"
            "   codex plugin add allostat@allostat\n"
            "   Then fully restart Codex, ask `Activate Allostat`, and trust "
            "the four hooks via /hooks."
        )
    return f"{head} Re-run the Allostat installer to upgrade."
