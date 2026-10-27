"""Allostat migration module — PATCH-MIGRATE.

Scope (per operator acceptance bar 2026-05-24):

  "install the MCP on my PC and the memory tree gets fixed from the
  plugin structure into the new structure... with all files seeded and
  moved to proper locations... automatically."

Audit found operator's existing files are already at new-structure paths
(`~/.claude/`, `~/.claude/allostat/projects/`,
`~/.claude/projects/<sanitized-cwd>/memory/`). PATCH-A scaffolder writes
Bucket B templates only when target absent. PATCH-D scaffolder writes
per-project memory tree only when memory dir absent. No file MOVES are
needed today — only scaffolding-when-absent.

This module provides:

1. **Backup discipline.** `backup_before_change(path)` snapshots an
   existing file before any modifier touches it. Used by future
   migration logic; currently exposed for the operator-PC test path.

2. **Verification.** `verify_install_state(home)` confirms scaffolders
   completed: first-run marker present, Bucket B templates present at
   `~/.claude/`, archipelago.json present at
   `~/.claude/.allostat/archipelago.json`. Returns structured result.

3. **Migration marker.** `write_migration_marker(home)` writes
   `~/.allostat/.migration_complete` after verification confirms
   structure is sound. Distinct from `.first_run_complete` (which
   PATCH-A scaffolder writes) so the two phases can be reasoned about
   separately.

Per `feedback_migration_never_destroys_existing_files.md`:
- NEVER deletes a file without backup
- Idempotent via marker (re-running skips when marker present)
- Log every action to `~/.allostat/.migration_log_<ts>.jsonl` for forensics
- Operator's existing tree content is sacrosanct
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


_MIGRATION_MARKER_PATH_PARTS = (".allostat", ".migration_complete")
_MIGRATION_BACKUP_DIR_PARTS = (".allostat",)
_MIGRATION_BACKUP_DIR_PREFIX = ".migration_backup_"
_MIGRATION_LOG_PREFIX = ".migration_log_"


@dataclass
class MigrationAction:
    """One migration action: backup / verify / marker. Logged for forensics."""
    kind: str  # "backup_created" | "verify_passed" | "verify_failed" | "marker_written"
    target: str  # path or check name
    detail: str = ""


@dataclass
class MigrationResult:
    """Result of a migration verification + marker write."""
    already_complete: bool = False
    verification_passed: bool = False
    actions: list[MigrationAction] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _ts_utc() -> str:
    """Current UTC timestamp for backup/log filename."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_migration_marker_path(home: Path | None = None) -> Path:
    """Path to the migration-complete marker file."""
    h = home or Path.home()
    p = h
    for part in _MIGRATION_MARKER_PATH_PARTS:
        p = p / part
    return p


def is_migration_complete(home: Path | None = None) -> bool:
    """True if migration marker present (migration already ran)."""
    return get_migration_marker_path(home=home).is_file()


def backup_before_change(target: Path, home: Path | None = None) -> MigrationAction:
    """Snapshot a file before any modifier touches it. NEVER deletes the
    target — only copies. Returns MigrationAction with backup location.

    Per feedback_migration_never_destroys_existing_files.md, this is the
    primitive any future migration MUST use before any move/overwrite.
    """
    h = home or Path.home()
    backup_root = h
    for part in _MIGRATION_BACKUP_DIR_PARTS:
        backup_root = backup_root / part
    backup_dir = backup_root / f"{_MIGRATION_BACKUP_DIR_PREFIX}{_ts_utc()}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        return MigrationAction(
            kind="backup_skipped_absent",
            target=str(target),
            detail="target does not exist; nothing to back up",
        )

    backup_target = backup_dir / target.name
    try:
        shutil.copy2(target, backup_target)
        return MigrationAction(
            kind="backup_created",
            target=str(target),
            detail=f"backed up to {backup_target}",
        )
    except OSError as e:
        return MigrationAction(
            kind="backup_failed",
            target=str(target),
            detail=str(e),
        )


def verify_install_state(home: Path | None = None) -> MigrationResult:
    """Verify scaffolding completed on this install.

    Checks:
    - `~/.allostat/.first_run_complete` marker present
    - Six Bucket B templates present at `~/.claude/`
    - `~/.claude/.allostat/archipelago.json` present

    Returns MigrationResult with passed/failed actions and failure reasons.
    """
    result = MigrationResult()
    h = home or Path.home()

    # First-run marker
    first_run_marker = h / ".allostat" / ".first_run_complete"
    if first_run_marker.is_file():
        result.actions.append(MigrationAction(
            kind="verify_passed",
            target=str(first_run_marker),
            detail="first-run marker present (PATCH-A scaffolder ran)",
        ))
    else:
        msg = f"first-run marker absent: {first_run_marker}"
        result.failures.append(msg)
        result.actions.append(MigrationAction(
            kind="verify_failed",
            target=str(first_run_marker),
            detail=msg,
        ))

    # Bucket B templates
    bucket_b_files = (
        "working_preferences.md",
        "file_locations.md",
        "typography.md",
        "design.md",
        "rollout_archival.md",
        "working_preferences_walkthrough_appendix.md",
    )
    customer_claude_dir = h / ".claude"
    missing = [
        f for f in bucket_b_files
        if not (customer_claude_dir / f).is_file()
    ]
    if missing:
        msg = f"Bucket B templates missing: {missing}"
        result.failures.append(msg)
        result.actions.append(MigrationAction(
            kind="verify_failed",
            target=str(customer_claude_dir),
            detail=msg,
        ))
    else:
        result.actions.append(MigrationAction(
            kind="verify_passed",
            target=str(customer_claude_dir),
            detail=f"all {len(bucket_b_files)} Bucket B templates present",
        ))

    # Archipelago
    archipelago = h / ".claude" / ".allostat" / "archipelago.json"
    if archipelago.is_file():
        result.actions.append(MigrationAction(
            kind="verify_passed",
            target=str(archipelago),
            detail="archipelago.json present",
        ))
    else:
        msg = f"archipelago.json absent: {archipelago}"
        result.failures.append(msg)
        result.actions.append(MigrationAction(
            kind="verify_failed",
            target=str(archipelago),
            detail=msg,
        ))

    result.verification_passed = not result.failures
    return result


def write_migration_marker(home: Path | None = None) -> MigrationAction:
    """Write the migration-complete marker AFTER verification passes."""
    marker = get_migration_marker_path(home=home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "marker": "migration_complete",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "migrate_version": "PATCH-MIGRATE v1.4.20",
    }
    try:
        marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return MigrationAction(
            kind="marker_written",
            target=str(marker),
            detail="migration verification complete",
        )
    except OSError as e:
        return MigrationAction(
            kind="marker_write_failed",
            target=str(marker),
            detail=str(e),
        )


def append_migration_log(
    actions: list[MigrationAction],
    home: Path | None = None,
) -> Path | None:
    """Append migration actions to a timestamped JSONL log for forensics."""
    h = home or Path.home()
    log_dir = h / ".allostat"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{_MIGRATION_LOG_PREFIX}{_ts_utc()}.jsonl"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            for action in actions:
                entry = {
                    "kind": action.kind,
                    "target": action.target,
                    "detail": action.detail,
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                }
                f.write(json.dumps(entry) + "\n")
        return log_path
    except OSError:
        return None


def run_migration(
    home: Path | None = None,
    force: bool = False,
) -> MigrationResult:
    """End-to-end migration: verify install state + write marker if passed.

    Idempotent via marker gate. Re-running on a complete install short-
    circuits to MigrationResult(already_complete=True).

    Args:
        home: override home dir (for testing)
        force: re-run verification even when marker present

    Returns MigrationResult with full audit trail.
    """
    if not force and is_migration_complete(home=home):
        result = MigrationResult(already_complete=True, verification_passed=True)
        result.actions.append(MigrationAction(
            kind="skipped_already_complete",
            target=str(get_migration_marker_path(home=home)),
            detail="migration marker present; skipping verification",
        ))
        return result

    result = verify_install_state(home=home)

    if result.verification_passed:
        marker_action = write_migration_marker(home=home)
        result.actions.append(marker_action)

    # Always log the run for forensics
    append_migration_log(result.actions, home=home)
    return result
