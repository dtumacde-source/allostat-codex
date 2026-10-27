"""Allostat LEGACY → _PURGE (cold storage) filesystem lifecycle ladder.

Archive is TERMINAL — nothing is ever auto-deleted. Content moves toward cold
storage and stops loading; it is never destroyed. (Operator directive: archive,
never destroy.) The former auto-delete stage — an unauthorized `shutil.rmtree`
of _PURGE folders >60d old, wired into the Stop hook — was removed 2026-07-08;
see MD_GOVERNANCE.md.

Stage transitions:
  1. legacy_supersede(rule_path, date)    - Active → _LEGACY/ (operator-driven)
  2. sweep_legacy_to_purge(memory_dir)    - _LEGACY/ files >30d → _PURGE/<today>/

_PURGE is permanent cold storage (kept under that name for back-compat). The
Stop hook fires the move-only sweep at session-end (session_id debounced).
Operator-explicit supersede via /allostat-tend --legacy <rule_id>.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


LEGACY_DAYS_BEFORE_PURGE = 30
LEGACY_SUBDIR = "_LEGACY"
PURGE_SUBDIR = "_PURGE"


@dataclass
class LifecycleAction:
    action: str  # "supersede" | "sweep_to_purge"
    source_path: str
    target_path: str | None = None
    success: bool = True
    error: str | None = None


def legacy_supersede(rule_path: Path, supersession_date: str | None = None) -> LifecycleAction:
    """Move a .md file from active memory tree to _LEGACY/ with the v2.3
    naming convention: <name>_LEGACY_pre_<YYYYMMDD>_rollout.md

    Args:
        rule_path: active .md file to supersede
        supersession_date: YYYYMMDD; defaults to today UTC
    """
    if not rule_path.exists():
        return LifecycleAction(
            action="supersede",
            source_path=str(rule_path),
            success=False,
            error="source_not_found",
        )
    if supersession_date is None:
        supersession_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    memory_dir = rule_path.parent
    legacy_dir = memory_dir / LEGACY_SUBDIR
    legacy_dir.mkdir(parents=True, exist_ok=True)

    stem = rule_path.stem  # without .md
    legacy_name = f"{stem}_LEGACY_pre_{supersession_date}_rollout.md"
    target = legacy_dir / legacy_name

    if target.exists():
        # Detect-before-write (invariant #4, archive-not-destroy): Path.rename
        # clobbers an existing destination silently on POSIX, so a same-UTC-day
        # re-supersede would destroy the earlier _LEGACY archive (v1 operator
        # content) with success=True and no backup. Refuse instead — matching
        # the target_exists_skipped guard every sibling stage transition uses
        # (sweep_legacy_to_purge, pruning.archive_candidate/restore_archive_pass).
        return LifecycleAction(
            action="supersede",
            source_path=str(rule_path),
            target_path=str(target),
            success=False,
            error="target_exists_skipped",
        )

    try:
        rule_path.rename(target)
        return LifecycleAction(
            action="supersede",
            source_path=str(rule_path),
            target_path=str(target),
            success=True,
        )
    except OSError as e:
        return LifecycleAction(
            action="supersede",
            source_path=str(rule_path),
            success=False,
            error=str(e),
        )


def sweep_legacy_to_purge(
    memory_dir: Path,
    threshold_days: int = LEGACY_DAYS_BEFORE_PURGE,
    today: datetime | None = None,
) -> list[LifecycleAction]:
    """Scan _LEGACY/ for files older than threshold_days; move each into
    _PURGE/<today YYYYMMDD>/. Idempotent on file moves (skip if target exists).
    """
    legacy_dir = memory_dir / LEGACY_SUBDIR
    if not legacy_dir.exists():
        return []

    if today is None:
        today = datetime.now(timezone.utc)
    today_stamp = today.strftime("%Y%m%d")
    purge_today = memory_dir / PURGE_SUBDIR / today_stamp

    actions: list[LifecycleAction] = []
    threshold_seconds = threshold_days * 86400

    for src in legacy_dir.rglob("*.md"):
        try:
            mtime = src.stat().st_mtime
            age_seconds = today.timestamp() - mtime
            if age_seconds < threshold_seconds:
                continue
        except OSError as e:
            actions.append(LifecycleAction(
                action="sweep_to_purge",
                source_path=str(src),
                success=False,
                error=f"stat_failed: {e}",
            ))
            continue

        try:
            purge_today.mkdir(parents=True, exist_ok=True)
            target = purge_today / src.name
            if target.exists():
                actions.append(LifecycleAction(
                    action="sweep_to_purge",
                    source_path=str(src),
                    target_path=str(target),
                    success=False,
                    error="target_exists_skipped",
                ))
                continue
            src.rename(target)
            actions.append(LifecycleAction(
                action="sweep_to_purge",
                source_path=str(src),
                target_path=str(target),
                success=True,
            ))
        except OSError as e:
            actions.append(LifecycleAction(
                action="sweep_to_purge",
                source_path=str(src),
                success=False,
                error=str(e),
            ))

    return actions


def run_full_sweep(memory_dir: Path) -> dict:
    """Move-only archival sweep: _LEGACY/ files >30d → _PURGE/<today>/.
    Called by the Stop hook (session-end-debounced).

    Archive is TERMINAL: this NEVER deletes, and _PURGE is permanent cold
    storage. The former delete_expired_purge stage (a timed `shutil.rmtree`
    of _PURGE folders) was removed 2026-07-08 — operator directive: archive,
    never destroy.
    """
    sweep_actions = sweep_legacy_to_purge(memory_dir)
    return {
        "swept_to_purge": [a for a in sweep_actions if a.success],
        "sweep_errors": [a for a in sweep_actions if not a.success],
    }
