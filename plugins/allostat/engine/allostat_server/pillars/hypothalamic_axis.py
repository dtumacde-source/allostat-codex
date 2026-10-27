"""Hypothalamic-axis pillar — signal-routing REFERENCE map (advisory).

Given an event signal (the wrapper observed something on the operator's
side), `SIGNAL_ROUTING` names which downstream pillars are conceptually
relevant.

IMPORTANT (audit #10, 2026-07-06): this table is an ADVISORY reference, NOT
the live turn-time dispatcher. The actual UserPromptSubmit routing is
hard-coded in `dispatch_tools.dispatch_user_prompt_submit_logic` (token
threshold → innate-enforcer; operator correction → pattern-observer +
recall-silos; observations present → stress-response / metabolism /
volume-control / pattern-observer). That dispatcher does NOT consult this
table, and the two can diverge — e.g. `operator_correction` here lists
[stress-response, pattern-observer] while the live dispatcher fires
pattern-observer + recall-silos. So editing SIGNAL_ROUTING does NOT change
runtime behavior; treat it as documentation of intended routing, or wire the
dispatcher against it directly if it should become authoritative.

The original aspiration was to centralize routing server-side so the table
could evolve without plugin updates; that wiring was never completed on the
UserPromptSubmit path. `hypothalamic_axis_route` remains callable for the
session_start-state query and explicit route_signal lookups.

Tool exposed by γ:

    hypothalamic_axis_route(excerpt, question) → PillarToolResponse

  question.question_type ∈ {"route_signal", "session_start_state"}

  For "route_signal", question.details contains:
    signal_type: str — e.g., "usage_threshold_crossed", "operator_correction"
    payload:     dict (optional) — additional context (threshold, tool name, etc.)

  Returns decision.action="route", decision.details.pillars: list[str].
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .._time import parse_iso as _parse_iso
from ..pillar_protocol import (
    DecisionPayload,
    coerce_int,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "hypothalamic-axis"


# Re-calibration thresholds (mirror plugin SKILL.md §"Re-calibration triggers"):
#   - 90 days since last calibration → natural refresh cadence
#   - Drift detected on ≥3 topics in rolling 100-event log → recal due
#   - Operator-initiated /allostat calibrate → always due
RECALIBRATION_DAYS_THRESHOLD = 90
RECALIBRATION_DRIFT_TOPIC_THRESHOLD = 3


# Signal → list of pillars to engage. ADVISORY REFERENCE ONLY (audit #10):
# this documents the INTENDED routing; it is NOT read by the live
# UserPromptSubmit dispatcher (dispatch_tools.dispatch_user_prompt_submit_logic
# hard-codes its own routing and does not import this table). Keep it in sync
# with the dispatcher by hand, or wire the dispatcher against it — but do not
# assume edits here change runtime behavior.
SIGNAL_ROUTING: dict[str, list[str]] = {
    # Innate guardrails fire first on volume/secret/destructive signals.
    "usage_threshold_crossed": ["innate-enforcer"],
    "secret_pattern_detected": ["innate-enforcer", "stress-response"],
    "destructive_command_attempt": ["innate-enforcer", "stress-response"],
    "innate_violation_attempt": ["innate-enforcer", "stress-response"],
    # Tool failures + corrections → stress + learning loop.
    "tool_failure": ["stress-response"],
    "tool_failure_repeated": ["stress-response"],
    "operator_correction": ["stress-response", "pattern-observer"],
    "contradiction_surge": ["stress-response"],
    # Allostatic load → metabolism + stress.
    "allostatic_load_threshold": ["stress-response", "metabolism"],
    # Session lifecycle.
    "session_start": ["recall-silos"],
    "session_end": ["volume-control", "pattern-observer"],
    # Memory / hygiene.
    "memory_sweep_due": ["volume-control"],
    "rollout_proposed": ["volume-control"],
}


# ---------- Decision logic ----------


def _route_signal(signal_type: str) -> list[str]:
    """Return the ordered pillar list for `signal_type`. Empty list when
    unknown — wrapper treats empty list as no_op."""
    return list(SIGNAL_ROUTING.get(signal_type, []))


# ---------- MCP tool ----------


def determine_session_start_state(
    *,
    has_allostat_state: bool,
    has_existing_context: bool,
    has_other_projects: bool,
    local_only: bool = False,
) -> int:
    """Mirror plugin/hooks/session-start.py's state-selection logic.

    Returns 1/2/3/4 per the SKILL.md §"Three-state session-start logic":
      State 1 — Known project (.allostat/state.json exists)        → silent
      State 2 — Existing CLAUDE.md / memory but no Allostat state   → activate
      State 3 — Truly new + operator has other projects             → prompt
      State 4 — First install ever (or LOCAL_ONLY hiding others)    → welcome

    LOCAL_ONLY (ALLOSTAT_LOCAL_ONLY env var on the wrapper side) forces
    the "no other projects" branch — even if other projects exist, they
    are hidden, so the decision tree falls through to State 4. Used for
    blind tests and sensitive client work.

    The wrapper probes its local filesystem to populate the flags; the
    server returns the state decision.
    """
    if has_allostat_state:
        return 1
    if has_existing_context:
        return 2
    if has_other_projects and not local_only:
        return 3
    return 4


def determine_recalibration_due(
    *,
    last_calibration_iso: str | None,
    drift_topic_count: int = 0,
    operator_initiated: bool = False,
    now: datetime | None = None,
    days_threshold: int = RECALIBRATION_DAYS_THRESHOLD,
    drift_threshold: int = RECALIBRATION_DRIFT_TOPIC_THRESHOLD,
) -> tuple[bool, str]:
    """Mirror SKILL.md §"Re-calibration triggers".

    Re-calibration is due when ANY of (checked in this order):
      1. Operator initiated (/allostat calibrate)
      2. drift_topic_count >= drift_threshold (default 3)
      3. > days_threshold days since last_calibration_iso (default 90)

    Returns (due, reason). Reason is the FIRST trigger that fires in the order
    above — drift (an active signal) takes precedence over the days-staleness
    backstop when both hold.
    """
    if operator_initiated:
        return True, "operator_initiated"

    if drift_topic_count >= drift_threshold:
        return True, f"drift_on_{drift_topic_count}_topics"

    if last_calibration_iso is None:
        return True, "never_calibrated"

    last = _parse_iso(last_calibration_iso)
    if last is None:
        return True, "unparseable_last_calibration"

    if now is None:
        now = datetime.now(timezone.utc)
    if (now - last) > timedelta(days=days_threshold):
        days_ago = (now - last).days
        return True, f"days_since_last={days_ago}"

    return False, "within_window"


def hypothalamic_axis_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function for the hypothalamic-axis pillar.

    Dispatches three question_types:
      route_signal           — signal_type → ordered pillar dispatch list
      session_start_state    — filesystem flags → state 1/2/3/4 decision
      recalibration_due      — last_calibration + drift + operator_initiated → yes/no
    """
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        if qt == "route_signal":
            return _route_signal_response(question)
        if qt == "session_start_state":
            return _session_start_state_response(question)
        if qt == "recalibration_due":
            return _recalibration_due_response(question)
        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


# v0.2.6 PATCH-128 body composers — additive only, no constant changes.
# `_aggregate` (dispatch_tools.py:120) requires both non-no_op action AND
# non-empty body to surface a nudge. These composers produce the body
# strings that were missing in v0.2.5 (silent-pillar bug).

_STATE_BODY_TEMPLATES: dict[int, str] = {
    1: "Allostat resumed (known project).",
    2: (
        "Allostat detected existing memory/context but no Allostat state "
        "for this project. Run `/allostat-init` to activate."
    ),
    3: (
        "New project. You have Allostat configured on other projects — "
        "run `/allostat-init` to set up here."
    ),
    4: "Welcome to Allostat. Run `/allostat-init` to begin onboarding.",
}


def _compose_session_start_body(state: int) -> str:
    return _STATE_BODY_TEMPLATES.get(state, "")


def _compose_route_body(signal_type: str, pillars: list[str]) -> str:
    pillar_list = ", ".join(pillars) if pillars else "(no pillars)"
    return f"Routing `{signal_type}` → {pillar_list}."


def _compose_recalibration_body(reason: str) -> str:
    return f"Re-calibration due ({reason}). Run `/allostat-calibrate`."


def _route_signal_response(question: QuestionPayload) -> PillarToolResponse:
    signal_type = question.details.get("signal_type")
    if not isinstance(signal_type, str) or not signal_type:
        return _no_op(
            decision_class="missing_signal_type",
            reason="question.details.signal_type required",
        )
    pillars = _route_signal(signal_type)
    if not pillars:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={
                    "signal_type": signal_type,
                    "pillars": [],
                    "reason": "no routing entry for this signal",
                },
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="no_route",
                fingerprint=signal_type,
            ),
        )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="route",
            body=_compose_route_body(signal_type, pillars),
            details={
                "signal_type": signal_type,
                "pillars": pillars,
                "ordering": "sequential",
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="routed",
            fingerprint=signal_type,
        ),
    )


def _session_start_state_response(question: QuestionPayload) -> PillarToolResponse:
    details = question.details or {}
    state = determine_session_start_state(
        has_allostat_state=bool(details.get("has_allostat_state", False)),
        has_existing_context=bool(details.get("has_existing_context", False)),
        has_other_projects=bool(details.get("has_other_projects", False)),
        local_only=bool(details.get("local_only", False)),
    )
    state_label = {
        1: "known_project",
        2: "existing_context_no_state",
        3: "new_with_other_projects",
        4: "first_install_or_local_only",
    }[state]
    return PillarToolResponse(
        decision=DecisionPayload(
            action="session_start_state",
            body=_compose_session_start_body(state),
            details={"state": state, "state_label": state_label},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class=state_label,
            fingerprint=f"state_{state}",
        ),
    )


def _recalibration_due_response(question: QuestionPayload) -> PillarToolResponse:
    details = question.details or {}
    due, reason = determine_recalibration_due(
        last_calibration_iso=details.get("last_calibration_iso"),
        drift_topic_count=coerce_int(details.get("drift_topic_count"), 0),
        operator_initiated=bool(details.get("operator_initiated", False)),
        days_threshold=int(
            details.get("days_threshold", RECALIBRATION_DAYS_THRESHOLD)
        ),
        drift_threshold=int(
            details.get("drift_threshold", RECALIBRATION_DRIFT_TOPIC_THRESHOLD)
        ),
    )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="recalibration_due" if due else "no_op",
            body=_compose_recalibration_body(reason) if due else None,
            details={"due": due, "reason": reason},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="recal_due" if due else "recal_not_due",
            fingerprint=reason,
        ),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="hypothalamic_axis_route",
        description=(
            "Central integrator + orchestrator. "
            "question_type=route_signal: signal_type → ordered pillar dispatch list. "
            "question_type=session_start_state: filesystem flags → state 1/2/3/4 "
            "decision (Known project / Existing context / New with other projects / "
            "First install). "
            "question_type=recalibration_due: last_calibration ISO + drift topic "
            "count + operator_initiated flag → recalibration-due decision per "
            "the 90-day / N≥3-topic / operator-initiated triggers."
        ),
    )
    def hypothalamic_axis_route(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return hypothalamic_axis_decision(excerpt, question)


def _no_op(*, decision_class: str, reason: str | None = None) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )
