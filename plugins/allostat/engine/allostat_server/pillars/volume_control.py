"""Volume-control pillar — filesystem hygiene gland (kidney axis).

Hybrid pillar per v2.4 conversion plan §4 row ζ: server makes the
decisions (which files to archive, when to nudge for canonical check,
etc.); the wrapper executes the filesystem operations (mv, archive).

The wrapper sends:
  - excerpt.observations: recent rollout / topology_change / multi-folder
    edit / canonical-verified events the wrapper has filtered to the
    current session
  - question.details.legacy_file_summary: {file_count, oldest_age_days}
    aggregated by the wrapper (it scans .allostat/ rglob '*_LEGACY_*'
    client-side, since the server is stateless per request)

The server returns:
  - action: "emit_volume_nudge" + body (the operator-facing nudge line)
  - OR "archive_files" + details.target_files (when the wrapper asked
    for a concrete archive decision)
  - OR "no_op"

Detection ports plugin/lib/volume_control_runtime.py verbatim:
  _detect_rollout_unrecorded
  _detect_topology_change_unrecorded
  _detect_canonical_check_pending
  compose_volume_control_nudge (priority order)

Legacy-file aging is wrapper-side filesystem work; the wrapper aggregates
and asks the server for the threshold decision.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..pillar_protocol import (
    DecisionPayload,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "volume-control"

# ============================================================================
# Constants — locked to plugin/lib/volume_control_runtime.py.
# ============================================================================

LEGACY_AGE_THRESHOLD_DAYS = 30
LEGACY_FILE_COUNT_NUDGE_THRESHOLD = 5

VOLUME_SKILL_MAP: dict[str, str] = {
    "rollout_unrecorded": "→ /allostat record-migration",
    "rollout_unarchived": "→ archive prior version with _LEGACY_pre_<date>_rollout suffix",
    "canonical_check_pending": "→ verify CANONICAL.txt before editing",
    "legacy_aging": "→ run /purge to move >30d legacy files to _PURGE folder",
    "memory_drift": "→ #audit-memory or /allostat audit",
    "topology_change_unrecorded": "→ /allostat record-migration",
}


# ============================================================================
# Detectors (pure functions over wrapper-supplied events list)
# ============================================================================


def detect_rollout_unrecorded(events: list[dict[str, Any]]) -> bool:
    """Rollout/deploy event logged but no record-migration entry follows.

    Mirrors plugin's _detect_rollout_unrecorded.
    """
    rollout_idx: int | None = None
    for i, ev in enumerate(events):
        if ev.get("event") in (
            "rollout_detected",
            "deploy_event",
            "package_published",
        ):
            rollout_idx = i
            break
    if rollout_idx is None:
        return False
    for ev in events[rollout_idx + 1:]:
        if ev.get("event") in ("migration_recorded", "allostat_record_migration"):
            return False
    return True


def detect_topology_change_unrecorded(events: list[dict[str, Any]]) -> bool:
    """A topology_change_detected fired but no record-migration follows.

    Mirrors plugin's _detect_topology_change_unrecorded.
    """
    topology_idx: int | None = None
    for i, ev in enumerate(events):
        if ev.get("event") == "topology_change_detected":
            topology_idx = i
            break
    if topology_idx is None:
        return False
    for ev in events[topology_idx + 1:]:
        if ev.get("event") in ("migration_recorded", "allostat_record_migration"):
            return False
    return True


def detect_canonical_check_pending(events: list[dict[str, Any]]) -> bool:
    """Multi-folder edit happened recently without a canonical_verified event.

    Mirrors plugin's _detect_canonical_check_pending.
    """
    edit_in_multi: int | None = None
    for i, ev in enumerate(events):
        if ev.get("event", "") in ("multi_folder_edit", "cross_project_access"):
            edit_in_multi = i
            break
    if edit_in_multi is None:
        return False
    for ev in events[edit_in_multi + 1:]:
        if ev.get("event") in ("canonical_verified", "canonical_check_passed"):
            return False
    return True


def should_nudge_legacy_aging(
    legacy_file_count: int,
    threshold: int = LEGACY_FILE_COUNT_NUDGE_THRESHOLD,
) -> bool:
    """Decide whether to fire the legacy-aging nudge.

    Plugin nudges when `legacy_count >= 5`. Wrapper aggregates the count
    via its own rglob (filesystem-side); server makes the threshold call.
    """
    return legacy_file_count >= threshold


# ============================================================================
# Integrator — compose_volume_control_nudge
# ============================================================================


def compose_volume_control_nudge(
    events: list[dict[str, Any]],
    *,
    legacy_file_count: int | None = None,
) -> tuple[str | None, str | None]:
    """Volume Control pillar's nudge composer.

    Mirrors plugin/lib/volume_control_runtime.py::compose_volume_control_nudge.
    Returns (nudge_body, signal_type) — both None when no nudge fires.

    Priority order (most-actionable first):
      1. Topology change unrecorded
      2. Rollout unrecorded
      3. Canonical check pending
      4. Legacy files aging (operator-supplied count ≥ 5)
    """
    if events and detect_topology_change_unrecorded(events):
        return (
            f"volume: topology_change_unrecorded. "
            f"{VOLUME_SKILL_MAP['topology_change_unrecorded']}",
            "topology_change_unrecorded",
        )
    if events and detect_rollout_unrecorded(events):
        return (
            f"volume: rollout_unrecorded. {VOLUME_SKILL_MAP['rollout_unrecorded']}",
            "rollout_detected",
        )
    if events and detect_canonical_check_pending(events):
        return (
            f"volume: canonical_check_pending. "
            f"{VOLUME_SKILL_MAP['canonical_check_pending']}",
            "canonical_check_pending",
        )
    if legacy_file_count is not None and should_nudge_legacy_aging(legacy_file_count):
        return (
            f"volume: legacy_aging ({legacy_file_count} files). "
            f"{VOLUME_SKILL_MAP['legacy_aging']}",
            "legacy_aging",
        )
    return (None, None)


# ============================================================================
# Hybrid: apply_archive_decision (server names files; wrapper executes mv)
# ============================================================================


def decide_archive_targets(
    candidate_files: list[dict[str, Any]],
    *,
    age_threshold_days: int = LEGACY_AGE_THRESHOLD_DAYS,
) -> dict[str, Any]:
    """Decide which candidate files the wrapper should archive.

    Inputs:
      candidate_files: list of {path: str, age_days: int, is_legacy: bool}
      age_threshold_days: minimum age for archival eligibility (default 30)

    Returns:
      {
        archive: [paths to move to _PURGE folder],
        leave: [paths to skip],
        rationale: per-file decision summary,
      }

    Per plan §4 row ζ: "wrapper uploads filesystem state excerpt, server
    returns 'archive these files / leave these alone', wrapper executes
    the mv". Server is stateless — wrapper sends candidates, server
    returns decisions, wrapper applies.
    """
    archive: list[str] = []
    leave: list[str] = []
    rationale: list[dict[str, Any]] = []

    for c in candidate_files:
        path = c.get("path", "")
        age = int(c.get("age_days", 0) or 0)
        is_legacy = bool(c.get("is_legacy", False))

        if not is_legacy:
            leave.append(path)
            rationale.append(
                {"path": path, "decision": "leave", "reason": "not a _LEGACY_ file"}
            )
            continue

        if age >= age_threshold_days:
            archive.append(path)
            rationale.append(
                {
                    "path": path,
                    "decision": "archive",
                    "reason": f"age {age}d >= threshold {age_threshold_days}d",
                }
            )
        else:
            leave.append(path)
            rationale.append(
                {
                    "path": path,
                    "decision": "leave",
                    "reason": f"age {age}d < threshold {age_threshold_days}d",
                }
            )

    return {"archive": archive, "leave": leave, "rationale": rationale}


# ============================================================================
# MCP tool dispatch
# ============================================================================


def volume_control_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below."""
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        details = question.details or {}

        if qt == "detect_rollout_unrecorded":
            return _bool_response(
                detect_rollout_unrecorded(excerpt.observations),
                fired_action="emit_volume_nudge",
                fired_class="rollout_unrecorded",
                silent_class="no_rollout_unrecorded",
            )
        if qt == "detect_topology_change_unrecorded":
            return _bool_response(
                detect_topology_change_unrecorded(excerpt.observations),
                fired_action="emit_volume_nudge",
                fired_class="topology_change_unrecorded",
                silent_class="no_topology_change_unrecorded",
            )
        if qt == "detect_canonical_check_pending":
            return _bool_response(
                detect_canonical_check_pending(excerpt.observations),
                fired_action="emit_volume_nudge",
                fired_class="canonical_check_pending",
                silent_class="no_canonical_check_pending",
            )
        if qt == "should_nudge_legacy_aging":
            count = int(details.get("legacy_file_count", 0))
            threshold = int(
                details.get("threshold", LEGACY_FILE_COUNT_NUDGE_THRESHOLD)
            )
            return _bool_response(
                should_nudge_legacy_aging(count, threshold=threshold),
                fired_action="emit_volume_nudge",
                fired_class="legacy_aging",
                silent_class="no_legacy_aging",
            )
        if qt == "compose_volume_control_nudge":
            legacy_count = details.get("legacy_file_count")
            try:
                legacy_count = int(legacy_count) if legacy_count is not None else None
            except (TypeError, ValueError):
                legacy_count = None
            nudge, signal = compose_volume_control_nudge(
                excerpt.observations,
                legacy_file_count=legacy_count,
            )
            if nudge is None:
                return PillarToolResponse(
                    decision=DecisionPayload(action="no_op"),
                    stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="silent"),
                )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_volume_nudge",
                    body=nudge,
                    details={"signal_type": signal},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="composed",
                    fingerprint=signal,
                ),
            )
        if qt == "decide_archive_targets":
            candidates = details.get("candidate_files", [])
            if not isinstance(candidates, list):
                return _no_op(
                    decision_class="missing_candidates",
                    reason="question.details.candidate_files (list) required",
                )
            threshold = int(
                details.get("age_threshold_days", LEGACY_AGE_THRESHOLD_DAYS)
            )
            decision = decide_archive_targets(
                candidates, age_threshold_days=threshold
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="archive_decisions" if decision["archive"] else "no_op",
                    body=(
                        f"Archive proposed: {len(decision['archive'])} "
                        f"file(s) above {threshold}-day age threshold."
                        if decision["archive"] else None
                    ),
                    details=decision,
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=(
                        "archive_proposed" if decision["archive"] else "no_archive"
                    ),
                    fingerprint=f"archive:{len(decision['archive'])}",
                ),
            )

        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


def _bool_response(
    value: bool,
    *,
    fired_action: str,
    fired_class: str,
    silent_class: str,
) -> PillarToolResponse:
    if value:
        return PillarToolResponse(
            decision=DecisionPayload(
                action=fired_action,
                body=f"volume: {fired_class}. {VOLUME_SKILL_MAP.get(fired_class, '')}".strip(),
                details={"signal_type": fired_class},
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="fired",
                fingerprint=fired_class,
            ),
        )
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op"),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=silent_class),
    )


def _no_op(
    *, decision_class: str, reason: str | None = None
) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="volume_control_evaluate",
        description=(
            "Volume-control pillar — filesystem hygiene gland (kidney axis). "
            "Hybrid: server decides, wrapper applies. "
            "question_type ∈ {detect_rollout_unrecorded, "
            "detect_topology_change_unrecorded, detect_canonical_check_pending, "
            "should_nudge_legacy_aging, compose_volume_control_nudge, "
            "decide_archive_targets}."
        ),
    )
    def volume_control_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return volume_control_decision(excerpt, question)
