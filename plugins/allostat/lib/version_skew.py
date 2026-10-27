"""E8 — version-skew banner (F4-class mitigation).

Problem (F4): the operator upgrades the plugin on disk (install.ps1 -Upgrade),
but the running Claude Code process keeps serving the pre-upgrade plugin
surface (slash-command defs and other manifest state are loaded once per
process). A *partial* restart doesn't reload them. The upgrade "silently
doesn't take" until the user FULLY quits and reopens Claude Code. Today that
stale state is invisible — the F4 diagnosis confirmed the on-disk delivery
was correct; the gap is purely a not-fully-restarted process.

This module detects that skew at SessionStart and surfaces a one-line nudge
("fully quit and reopen Claude Code"). It converts an invisible stale-state
into an explicit, self-correcting prompt. Wrapper-only — no server call.

Detection is false-positive-safe: it only reports skew on a *continuation*
of an existing process (a fresh process by definition just loaded the current
version, so there is nothing to nudge). Two independent signals are OR'd so
the check is robust whether or not Claude Code re-resolves the hook path on
upgrade:

  1. tree-skew — the hook is running from an older versioned plugin tree than
     the one currently registered in installed_plugins.json. This fires when
     Claude Code pins the plugin/hook path at process start (the common case;
     it is also why slash commands go stale). Zero false positives.
  2. stamp-skew — the authoritative registered version has moved away from the
     version this process stamped at its first SessionStart. This fires when
     Claude Code re-resolves the hook path to the new tree but still serves a
     stale in-process command surface. Keyed on parent PID + the SessionStart
     `source` field so a genuine fresh launch refreshes the stamp.

Best-effort throughout: any read/parse failure returns "no skew" rather than
raising. Opt out entirely with ALLOSTAT_UPDATE_NOTICE=0.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Registration key for this plugin in installed_plugins.json. The marketplace
# name is "local"; the plugin id is "allostat-mcp" (decoupled from the repo
# folder name). Match the exact key first, then any "allostat-mcp@*" fallback
# so a future marketplace rename doesn't blind the check.
_PLUGIN_KEY = "allostat-mcp@local"
_PLUGIN_KEY_PREFIX = "allostat-mcp@"

_STAMP_FILENAME = "process_version_stamp.json"


@dataclass
class SkewDecision:
    """Result of a version-skew evaluation.

    skew:            True if the running process is behind the on-disk install.
    loaded_version:  the version the running process is serving (stale).
    current_version: the version currently installed on disk.
    reason:          "tree" | "stamp" | None — which signal fired.
    stamp_to_write:  dict to persist as the new stamp, or None to leave the
                     stamp file untouched.
    """

    skew: bool
    loaded_version: str | None
    current_version: str | None
    reason: str | None
    stamp_to_write: dict | None


def notices_disabled() -> bool:
    """True if the operator opted out via ALLOSTAT_UPDATE_NOTICE=0."""
    raw = os.environ.get("ALLOSTAT_UPDATE_NOTICE")
    if raw is None:
        return False
    return raw.strip().lower() in ("0", "false", "no", "off")


def read_tree_version(plugin_root: Path) -> str | None:
    """Version of the plugin tree the running hook is executing from
    (plugin.json at the plugin root). On a stale, path-pinned process this is
    the OLD version even after an upgrade installed a new versioned tree."""
    try:
        data = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("version")
    return str(v) if v else None


def _find_installed_plugins_json(plugin_root: Path) -> Path | None:
    """Walk up from the plugin root to the harness `plugins/` dir that holds
    installed_plugins.json (the authoritative current-install pointer that an
    upgrade repoints). Layout: .../.claude/plugins/cache/local/allostat-mcp/<v>.
    """
    for parent in plugin_root.parents:
        ip = parent / "installed_plugins.json"
        if ip.is_file():
            return ip
    return None


def read_registered_version(plugin_root: Path) -> str | None:
    """Authoritative installed version from installed_plugins.json. This is
    what `install.ps1 -Upgrade` repoints; a fresh process loads exactly this."""
    ip = _find_installed_plugins_json(plugin_root)
    if ip is None:
        return None
    try:
        data = json.loads(ip.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return None
    entry = plugins.get(_PLUGIN_KEY)
    if entry is None:
        for key, val in plugins.items():
            if isinstance(key, str) and key.startswith(_PLUGIN_KEY_PREFIX):
                entry = val
                break
    if not entry:
        return None
    record = entry[0] if isinstance(entry, list) and entry else entry
    if not isinstance(record, dict):
        return None
    v = record.get("version")
    if v:
        return str(v)
    # Fallback: derive from the installPath basename (.../allostat-mcp/<v>).
    install_path = record.get("installPath")
    if isinstance(install_path, str) and install_path:
        base = install_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if base and base != "unknown":
            return base
    return None


def decide(
    *,
    tree_version: str | None,
    registered_version: str | None,
    prior_stamp: dict | None,
    ppid: int,
    source: str | None,
) -> SkewDecision:
    """Pure decision. See module docstring for the two signals.

    Skew is only ever reported on a *continuation* of an existing process. A
    fresh process (no prior stamp, source=="startup", or a new PID) just
    loaded the current version, so we (re)stamp and report no skew.
    """
    fresh = (
        prior_stamp is None
        or source == "startup"
        or prior_stamp.get("ppid") != ppid
    )
    if fresh:
        stamp = (
            {"ppid": ppid, "version": registered_version, "stamped_at": _now_iso()}
            if registered_version
            else None
        )
        return SkewDecision(
            skew=False,
            loaded_version=None,
            current_version=None,
            reason=None,
            stamp_to_write=stamp,
        )

    # Continuation of the same process — evaluate skew. Do NOT rewrite the
    # stamp, so the nudge keeps firing every session until a full restart.
    tree_skew = bool(
        tree_version and registered_version and tree_version != registered_version
    )
    stamped_version = prior_stamp.get("version")
    stamp_skew = bool(
        stamped_version
        and registered_version
        and stamped_version != registered_version
    )

    if tree_skew:
        return SkewDecision(
            skew=True,
            loaded_version=tree_version,
            current_version=registered_version,
            reason="tree",
            stamp_to_write=None,
        )
    if stamp_skew:
        return SkewDecision(
            skew=True,
            loaded_version=stamped_version,
            current_version=registered_version,
            reason="stamp",
            stamp_to_write=None,
        )
    return SkewDecision(
        skew=False,
        loaded_version=None,
        current_version=None,
        reason=None,
        stamp_to_write=None,
    )


def evaluate(state_dir: Path | None, plugin_root: Path, payload: dict) -> SkewDecision | None:
    """IO orchestrator. Reads versions + the per-process stamp, decides, and
    persists a refreshed stamp when needed. Best-effort: returns None on any
    failure or when notices are disabled."""
    if notices_disabled():
        return None
    try:
        registered_version = read_registered_version(plugin_root)
        tree_version = read_tree_version(plugin_root)
        ppid = os.getppid()
        source = payload.get("source") if isinstance(payload, dict) else None

        stamp_path = (state_dir / _STAMP_FILENAME) if state_dir is not None else None
        prior_stamp = _read_stamp(stamp_path)

        decision = decide(
            tree_version=tree_version,
            registered_version=registered_version,
            prior_stamp=prior_stamp,
            ppid=ppid,
            source=source,
        )

        if decision.stamp_to_write is not None and stamp_path is not None:
            _write_stamp(stamp_path, decision.stamp_to_write)

        return decision
    except Exception:
        return None


def format_banner_line(decision: SkewDecision) -> str | None:
    """One-line banner notice for a skew, or None when there is no skew."""
    if not decision.skew:
        return None
    loaded = decision.loaded_version or "an older version"
    current = decision.current_version or "a newer version"
    return (
        f"⚠ Allostat updated on disk (v{loaded} → v{current}) — "
        "fully quit and reopen Claude Code to load it."
    )


# --- internal helpers -------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_stamp(stamp_path: Path | None) -> dict | None:
    if stamp_path is None or not stamp_path.is_file():
        return None
    try:
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_stamp(stamp_path: Path, stamp: dict) -> None:
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    except OSError:
        # Best-effort: the stamp is a disposable per-process hint. A failed
        # write just means the next session re-stamps from the current
        # registered version — no correctness loss, and the banner must never
        # break on state IO. Silenced intentionally (no observation needed).
        pass
