"""Folder-drift detector — rule innate-12 (companion to canonical_workspace).

Folder drift = a strategic deliverable (project source-of-truth content)
being written to a generic drift folder (Downloads / Desktop / Documents)
when the active project has a canonical workspace declared. The rule YAML
lists this module FIRST under rule 12's `implementation.lib_module`:

    lib/folder_drift_check.py + new lib/canonical_workspace.py (TBD)

`canonical_workspace.py` owns the primitives (Downloads/drift detection,
canonical-workspace discovery, strategic-deliverable classification,
allowlist). This module composes them into the drift decision the Volume
Control producer emits as an observation.

Two surfaces:

  - check_folder_drift(target, project_root=None, content=None) -> DriftResult
      pure predicate. is_drift True iff the target is a strategic deliverable
      landing in a drift folder while a canonical workspace is declared
      (and the target is not allowlisted).

  - detect_and_emit(state_dir, tool_name, file_path, project_root, content)
      classifies + appends a `folder_drift_detected` observation. Mirrors
      rollout_detector.detect_and_emit: env-gated, tool-name-guarded,
      best-effort (any append failure is swallowed so the PreToolUse hook
      never breaks the operator's session).

Generic paths only — no operator-specific paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from canonical_workspace import (
    _is_allowlisted,
    _segments,
    classify_write_target,
    find_canonical_workspace,
    is_drift_location,
)


# File-mutating tools this detector applies to. Bash/Read/Glob etc. are not
# file-content writes and are skipped (a `cp ... Downloads/` via Bash is out
# of scope for v1 — the rule's enforcer_hook keys off Write/Edit).
_WRITE_TOOLS = frozenset({"write", "edit", "notebookedit", "multiedit"})

# Drift-folder segment → canonical drift_location tag. Mirrors the
# _DRIFT_SEGMENTS vocabulary in canonical_workspace.
_DRIFT_TAGS = ("downloads", "desktop", "documents")


@dataclass(frozen=True)
class DriftResult:
    """Result of a folder-drift check.

    - is_drift:       True iff strategic deliverable + drift folder +
                      canonical workspace declared + not allowlisted.
    - drift_location: which drift folder ("downloads"/"desktop"/"documents"),
                      or None when not in a drift folder.
    - canonical_path: declared canonical workspace root (for the redirect
                      suggestion), or None when none declared.
    - is_strategic:   target classifies as a strategic deliverable.
    - reason:         short machine tag for forensics / logging.
    """

    is_drift: bool
    drift_location: str | None
    canonical_path: str | None
    is_strategic: bool
    reason: str


def _drift_location_tag(path: str | None) -> str | None:
    """Return which drift folder `path` traverses, or None."""
    if not path:
        return None
    segs = set(_segments(path))
    for tag in _DRIFT_TAGS:
        if tag in segs:
            return tag
    return None


def check_folder_drift(
    target_path: str | None,
    project_root: str | None = None,
    content: str | None = None,
) -> DriftResult:
    """Pure drift predicate. Never raises; safe no-drift default on bad input.

    is_drift True iff ALL hold:
      1. target is a strategic deliverable (classify_write_target),
      2. target lands in a drift folder (Downloads / Desktop / Documents),
      3. a canonical workspace is declared (preferring `project_root`, else
         autodiscovered from the target),
    and the target is not allowlisted.
    """
    if not target_path:
        return DriftResult(False, None, None, False, "no_target")

    drift_loc = _drift_location_tag(target_path)
    is_strat = classify_write_target(target_path, content=content)

    canonical_path = None
    if project_root:
        canonical_path = find_canonical_workspace(project_root)
        if canonical_path is None:
            # project_root may itself be the declaring dir; find_*
            # starts at its parent when project_root is an existing dir, so
            # double-check the root directly.
            from canonical_workspace import _direct_declaration

            canonical_path = _direct_declaration(project_root)
    else:
        canonical_path = find_canonical_workspace(target_path)

    allowlisted = _is_allowlisted(target_path)
    is_drift = bool(
        is_strat and drift_loc and canonical_path and not allowlisted
    )

    if is_drift:
        reason = "drift"
    elif allowlisted:
        reason = "allowlisted"
    elif not drift_loc:
        reason = "not_drift_folder"
    elif not canonical_path:
        reason = "no_canonical_workspace"
    elif not is_strat:
        reason = "not_strategic"
    else:
        reason = "no_drift"

    return DriftResult(
        is_drift=is_drift,
        drift_location=drift_loc if is_drift else None,
        canonical_path=canonical_path,
        is_strategic=is_strat,
        reason=reason,
    )


def _scan_enabled() -> bool:
    """Honor ALLOSTAT_FOLDER_DRIFT_CHECK. Defaults to enabled.

    Operator can set `0|false|no|off` to disable. Mirrors the
    rollout_detector env-gate idiom.
    """
    raw = os.environ.get("ALLOSTAT_FOLDER_DRIFT_CHECK", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _append_observation(state_dir, event: str, details: dict | None = None) -> None:
    """Indirection over local_state.append_observation so tests can
    monkeypatch this symbol (matching the pattern other detectors rely on).

    Imported lazily so the module imports cleanly even if local_state isn't
    importable in a given context (e.g. a narrow unit-test harness).
    """
    from local_state import append_observation  # noqa: E402

    append_observation(state_dir, event, details=details)


def detect_and_emit(
    state_dir,  # Path | None
    tool_name: str,
    file_path: str | None,
    project_root: str | None = None,
    content: str | None = None,
) -> dict[str, Any] | None:
    """Check for folder drift on a Write/Edit and emit a
    `folder_drift_detected` observation when it fires.

    Returns the emitted details dict, or None when nothing fires (not a
    write tool, not drift, scanning disabled, no state_dir, or append
    failed). Best-effort: any append failure is swallowed so the PreToolUse
    hook never breaks the operator's session.
    """
    if not _scan_enabled():
        return None
    if (tool_name or "").lower() not in _WRITE_TOOLS:
        return None
    if state_dir is None:
        return None

    result = check_folder_drift(file_path, project_root=project_root, content=content)
    if not result.is_drift:
        return None

    details: dict[str, Any] = {
        "drift_location": result.drift_location,
        "canonical_path": result.canonical_path,
        "target_excerpt": (file_path or "")[:300],
        "basename": PurePath(file_path).name if file_path else "",
    }
    try:
        _append_observation(state_dir, "folder_drift_detected", details=details)
    except Exception:
        return None
    return details
