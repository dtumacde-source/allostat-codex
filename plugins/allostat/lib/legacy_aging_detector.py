"""Legacy-aging detector — PATCH-191 (Phase 3.3a).

Producer for the `legacy_aging` observation event that volume-control's
4th detector (`should_nudge_legacy_aging`) consumes. Pre-PATCH-191 this
producer didn't exist; volume-control's 4th branch was structurally
unable to fire because nothing wrote the events it reads.

Strategy: at session-start, rglob the project tree for `_LEGACY_*` files
older than 30 days. If the count is ≥1, append a `legacy_aging`
observation with `file_count` in details. The existing server-side
`compose_volume_control_nudge` reads `legacy_file_count` from
`_volume_control_details_from_obs` which sums these observations.

Scope choices for v1:
- Scan project root (operator's cwd) with rglob
- Skip well-known build/cache directories to avoid scanning into
  third-party trees (.git, node_modules, __pycache__, .venv, etc.)
- Cap depth at PROJECT_SCAN_MAX_DEPTH to bound walk cost on huge trees
- Cap file count emitted in observation at OBSERVATION_FILE_LIST_CAP to
  bound observations.jsonl size
- Age threshold matches server-side constant
  `LEGACY_AGE_THRESHOLD_DAYS = 30`
- Operator can override via `ALLOSTAT_LEGACY_AGING_SCAN` env var:
  "" / "1" → scan enabled (default); "0" → disabled
  Disabling is useful for very large monorepos where session-start
  shouldn't pay the rglob cost.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


# Mirror server-side constant from pillars/volume_control.py
LEGACY_AGE_THRESHOLD_DAYS = 30
_AGE_THRESHOLD_SECONDS = LEGACY_AGE_THRESHOLD_DAYS * 86_400

# Bound walk cost. 8 levels deep covers reasonable project structures
# without recursing into typical deep node_modules / venv trees.
PROJECT_SCAN_MAX_DEPTH = 8

# Cap how many file paths get embedded in the observation details to
# avoid bloating observations.jsonl on projects with many LEGACY files.
OBSERVATION_FILE_LIST_CAP = 20

# Directory names skipped during rglob — never scan into these regardless
# of depth. Avoids walking node_modules / .venv / .git trees that may
# contain incidental _LEGACY_* filenames belonging to third-party code.
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".tox",
    ".eggs",
    "site-packages",
})


def _scan_enabled() -> bool:
    """Honor ALLOSTAT_LEGACY_AGING_SCAN env var. Defaults to enabled."""
    raw = os.environ.get("ALLOSTAT_LEGACY_AGING_SCAN", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _walk_for_legacy_files(
    root: Path,
    *,
    max_depth: int = PROJECT_SCAN_MAX_DEPTH,
    age_threshold_seconds: int = _AGE_THRESHOLD_SECONDS,
    now_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Walk root looking for `_LEGACY_*` files older than the threshold.

    Returns list of {path, age_days} dicts (sorted oldest-first). Skips
    `_SKIP_DIR_NAMES` directories at any depth. Bounded by max_depth.

    Best-effort — file stat errors are skipped silently rather than
    halting the walk.
    """
    if now_seconds is None:
        now_seconds = time.time()
    if not root.is_dir():
        return []

    found: list[dict[str, Any]] = []

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if name in _SKIP_DIR_NAMES:
                    continue
                _walk(entry, depth + 1)
            else:
                if "_LEGACY_" not in name:
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                age_seconds = now_seconds - st.st_mtime
                if age_seconds < age_threshold_seconds:
                    continue
                found.append({
                    "path": str(entry),
                    "age_days": int(age_seconds / 86_400),
                })

    _walk(root, depth=0)
    found.sort(key=lambda x: -x["age_days"])  # oldest first
    return found


def detect_and_emit(state_dir: Path, project_root: Path) -> dict[str, Any] | None:
    """Run the scan + emit the `legacy_aging` observation if any files
    above threshold exist.

    Returns the observation details dict that was emitted, or None when:
      - Scanning is disabled via env var
      - project_root not a directory
      - Zero files above threshold

    Caller (session-start hook) ignores the return value; it's primarily
    for tests + diagnostic logging.
    """
    if not _scan_enabled():
        return None
    if project_root is None or not project_root.is_dir():
        return None

    found = _walk_for_legacy_files(project_root)
    if not found:
        return None

    capped_paths = [f["path"] for f in found[:OBSERVATION_FILE_LIST_CAP]]
    oldest_age = found[0]["age_days"]

    details = {
        "file_count": len(found),
        "oldest_age_days": oldest_age,
        "threshold_days": LEGACY_AGE_THRESHOLD_DAYS,
        "scan_root": str(project_root),
        "sample_paths": capped_paths,
    }

    try:
        from local_state import append_observation  # noqa: E402
        append_observation(state_dir, "legacy_aging", details=details)
    except Exception:
        return None

    return details
