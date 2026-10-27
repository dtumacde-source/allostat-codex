"""Voice-keeper pillar — heuristic voice-fidelity evaluation.

Server-side decision logic for plugin/lib/voice_keeper.py (616 lines).
The wrapper plugin sends candidate text + recent violation history; the
server evaluates and returns violations + nudge decisions. The voice
reference file lives on the operator's filesystem (wrapper-side); the
server doesn't read it.

Tools exposed:

    voice_keeper_evaluate(excerpt, question) → PillarToolResponse

question.question_type ∈ {
    "evaluate_voice",        — REFUSED (AUDIT-D24): the server must not
                               receive raw assistant prose. Voice scanning is
                               wrapper-canonical in lib/voice_keeper_local.py.
    "compose_voice_nudge",   — priority-ordered nudge composer
    "format_eval_surface",   — operator-facing eval surface text
}

The server pillar carries only the two question_types that take no raw
prose: compose_voice_nudge (observation-level events) and format_eval_surface
(an already-computed eval dict). The voice-scanning logic (marker lists +
evaluate_voice) lives wrapper-canonical in lib/voice_keeper_local.py; the
dead, dispatch-refused server copy was removed (2026-06-16), so there is no
server↔wrapper scanning drift to track here.
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

PILLAR_ID = "voice-keeper"


# ============================================================================
# Constants — nudge composer only.
#
# Voice SCANNING constants (AI_SLOP_MARKERS, HEDGE_MARKERS, CORPORATE_MARKERS,
# DEFAULT_BANNED_TERMS, EM_DASH/PASSIVE thresholds) were removed when the dead,
# dispatch-refused evaluate_voice was deleted — scanning is wrapper-canonical
# in lib/voice_keeper_local.py (AUDIT-D24). Only the nudge-composer constants
# remain, since compose_voice_nudge still runs server-side.
# ============================================================================

# Skill-hint map for nudge composer.
VOICE_SKILL_MAP: dict[str, str] = {
    # Aggregate signals.
    "ai_slop_recent": "→ avoid slop markers next response",
    "sycophancy_recent": "→ no preamble, answer directly",
    "wall_of_text": "→ match brevity to question size",
    "advice_when_action_asked": "→ perform the action, don't advise",
    # Last-resort fallback only. Before AR-01 this was what a reader got for
    # EVERY non-slop, non-sycophancy violation — a category of concern plus a
    # file to go re-read, with the actual measurement dropped. It fired 41
    # times in one session and was correctly ignored all 41.
    "voice_drift_cumulative": "→ re-anchor on the project's voice reference file",
    # Per-category guidance, keyed by the `category` values `evaluate_voice`
    # actually emits (AR-01, 2026-07-30). Pinned by
    # test_every_detectable_category_has_actionable_guidance so a new detector
    # category cannot ship without something useful to say about it.
    "ai_slop": "→ cut the slop marker, say the thing plainly",
    "sycophancy": "→ drop the preamble, answer directly",
    "hedge": "→ drop the hedge, state it plainly",
    "corporate": "→ say it in plain English",
    "banned_term": "→ drop the intensifier, let the fact carry it",
    "em_dash_overuse": "→ fewer em-dashes; use commas, colons, or full stops",
    "passive_voice": "→ name the actor, rewrite active",
}

# Worst-first. A turn commonly trips several rules and the nudge has room for
# one, so the lead is chosen by severity rather than by scan order (which is
# just the order `evaluate_voice` happens to append its scanners).
_SEVERITY_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


def _describe_violation(violation: dict[str, Any]) -> str:
    """One concrete phrase naming what was measured.

    Prefers the `detail` stat string (em_dash_overuse / passive_voice compute
    one). Marker-based categories carry no `detail`, so their marker and count
    stand in — "'it's worth noting' ×2" is still a thing the reader can act on,
    which "voice drift" is not.
    """
    detail = violation.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    marker = violation.get("marker")
    if isinstance(marker, str) and marker.strip():
        occurrences = violation.get("occurrences")
        if isinstance(occurrences, int) and occurrences > 1:
            return f"'{marker}' ×{occurrences}"
        return f"'{marker}'"
    return ""


def _lead_violation(violations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The violation worth spending the line on: most severe, then most
    frequent. Ties keep scan order, so the result is deterministic."""
    ranked = [v for v in violations if isinstance(v, dict)]
    if not ranked:
        return None
    return max(
        ranked,
        key=lambda v: (
            _SEVERITY_RANK.get(str(v.get("severity") or "").lower(), 0),
            v.get("occurrences") if isinstance(v.get("occurrences"), int) else 0,
        ),
    )

# Sycophancy markers — response-opener phrases.
SYCOPHANCY_MARKERS: tuple[str, ...] = (
    "you're absolutely right",
    "you're absolutely correct",
    "great question",
    "great point",
    "i apologize for",
    "my apologies",
    "i appreciate",
    "thank you for clarifying",
    "thanks for the correction",
    "that's a great",
)


# ============================================================================
# Decision logic
# ============================================================================


def compose_voice_nudge(
    recent_violation_events: list[dict[str, Any]],
) -> str | None:
    """Voice-keeper's nudge composer (mirrors plugin compose_voice_nudge).

    The wrapper supplies recent voice_violation_detected events from its
    local observations.jsonl (filtered to last 30 events). Server priority:
      1. Sycophancy in most recent → highest priority
      2. AI-slop in most recent → with examples
      3. Cumulative drift (≥3 recent events)
    Returns the nudge line or None.
    """
    if not recent_violation_events:
        return None

    latest = recent_violation_events[-1]
    payload = (latest.get("details") or {}).get("payload") or {}
    violations = payload.get("violations") or []

    has_sycophancy = any(
        any(syco in str(v.get("marker", "")).lower() for syco in SYCOPHANCY_MARKERS)
        for v in violations
    )
    if has_sycophancy:
        return f"voice: sycophancy_recent. {VOICE_SKILL_MAP['sycophancy_recent']}"

    has_ai_slop = any(v.get("category") == "ai_slop" for v in violations)
    if has_ai_slop:
        slop_markers = [
            v.get("marker") for v in violations[:3]
            if v.get("category") == "ai_slop"
        ]
        examples = ", ".join(f"'{m}'" for m in slop_markers if m)
        return f"voice: ai_slop_recent ({examples}). {VOICE_SKILL_MAP['ai_slop_recent']}"

    if len(recent_violation_events) >= 3:
        # AR-01 (2026-07-30). This branch used to emit the category name
        # `voice_drift_cumulative`, a count, and a pointer to a reference file —
        # and nothing about what was actually detected. Every one of the 41
        # violations on 2026-07-27 came through here carrying a precise
        # measurement that never reached the reader.
        #
        # The threshold is deliberately UNCHANGED (ticket: "MUST NOT change
        # detection thresholds"), and so is the one-line budget. Only the
        # content changed: lead with the finding, keep the aggregate count,
        # and give guidance for the category actually detected instead of the
        # generic re-anchor line.
        count = len(recent_violation_events)
        lead = _lead_violation(violations)
        if lead is not None:
            category = str(lead.get("category") or "").strip()
            described = _describe_violation(lead)
            guidance = VOICE_SKILL_MAP.get(
                category, VOICE_SKILL_MAP["voice_drift_cumulative"]
            )
            headline = f"voice_drift_cumulative — {category}" if category else "voice_drift_cumulative"
            if described:
                headline = f"{headline}: {described}"
            return f"voice: {headline} ({count} recent). {guidance}"
        return (
            f"voice: voice_drift_cumulative ({count} recent). "
            f"{VOICE_SKILL_MAP['voice_drift_cumulative']}"
        )

    return None


def format_eval_surface(result: dict[str, Any]) -> str:
    """Render the eval result for surfacing to Claude or the operator.

    Mirrors plugin/lib/voice_keeper.py::format_eval_surface verbatim.
    """
    status_emoji = "🟢" if result.get("loop_status") == "satisfied" else "🔴"
    voice_ref = result.get("voice_reference_path") or "(none — using built-in heuristics)"
    lines = [
        f"\n{status_emoji}  Voice keeper evaluation",
        f"  Status: {result.get('loop_status', '')}",
        f"  Words: {result.get('word_count', 0)}",
        f"  Voice reference: {voice_ref}",
        f"  Summary: {result.get('summary', '')}",
    ]
    violations = result.get("violations", [])
    if violations:
        lines.append("")
        lines.append("  Violations:")
        for v in violations[:8]:
            lines.append(
                f"    [{v.get('severity', '')}] {v.get('category', '')}: "
                f"'{v.get('marker', '')}' ×{v.get('occurrences', 0)}"
            )
        if len(violations) > 8:
            lines.append(f"    (+{len(violations) - 8} more)")
    return "\n".join(lines)


# ============================================================================
# MCP tool dispatch
# ============================================================================


def voice_keeper_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below."""
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        details = question.details or {}

        if qt == "evaluate_voice":
            # PATCH-198 / AUDIT-D24 (2026-05-24) — privacy moat regression
            # closure. Pre-PATCH-198 this branch accepted `target_text` (raw
            # assistant prose) over the wire. The server has no business
            # seeing assistant content per the ExcerptPayload docstring
            # invariant + PATCH-190 wrapper-local voice-keeper port. Stub
            # the branch to refuse with a clear, actionable error so any
            # remaining caller can migrate to wrapper-local evaluation.
            #
            # Per advisor §6 (2026-05-24): REFUSE rather than DELETE — keeps
            # the API surface (tool exists, question_type recognized) and
            # gives a clear deprecation message instead of silent 404 /
            # ImportError. Backward-compat-friendly retirement.
            return _no_op(
                decision_class="raw_content_refused",
                reason=(
                    "evaluate_voice deprecated server-side per AUDIT-D24 "
                    "(2026-05-24): accepts raw assistant prose, regressing "
                    "the ExcerptPayload 'no LLM response content' privacy "
                    "invariant. Use wrapper-local lib/voice_keeper_local."
                    "evaluate_voice (PATCH-190) instead. Server-side tool "
                    "preserved for API surface compatibility but inert."
                ),
            )

        if qt == "compose_voice_nudge":
            events = details.get("recent_violation_events", []) or []
            if not isinstance(events, list):
                return _no_op(
                    decision_class="missing_events",
                    reason="question.details.recent_violation_events (list) required",
                )
            nudge = compose_voice_nudge(events)
            if nudge is None:
                return PillarToolResponse(
                    decision=DecisionPayload(action="no_op"),
                    stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="silent"),
                )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_voice_nudge",
                    body=nudge,
                    details={"events_seen": len(events)},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="composed",
                ),
            )

        if qt == "format_eval_surface":
            result_in = details.get("result")
            if not isinstance(result_in, dict):
                return _no_op(
                    decision_class="missing_result",
                    reason="question.details.result (dict) required",
                )
            surface = format_eval_surface(result_in)
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_eval_surface",
                    body=surface,
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="surface_rendered",
                ),
            )

        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


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
        name="voice_keeper_evaluate",
        description=(
            "Voice-keeper pillar. "
            "question_type=evaluate_voice: REFUSED (AUDIT-D24) — raw assistant "
            "prose must not reach the server; voice scanning is wrapper-local "
            "(lib/voice_keeper_local.py). "
            "question_type=compose_voice_nudge: priority-ordered nudge "
            "composer over recent voice_violation_detected events. "
            "question_type=format_eval_surface: render an eval-result dict "
            "as operator-facing surface text."
        ),
    )
    def voice_keeper_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return voice_keeper_decision(excerpt, question)
