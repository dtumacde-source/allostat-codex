"""Metabolism pillar — token-budget regulation (insulin/glucagon + thyroid).

Hybrid pillar per v2.4 conversion plan §4 row ζ: token-state inference
stays CLIENT-SIDE (the wrapper reads Claude's session token count and
emits signals). The SERVER aggregates cross-call token-pressure signals
for cross-session insight + provides the threshold-meaningful decision,
work-mode inference, eviction priority, baseline computation, and nudge
composer.

The wrapper sends:
  - excerpt.observations: rolling-window observations including any
    pillar_signal_emitted events with signal_type in
    {usage_threshold_crossed, approaching_cutoff}
  - question.details: per-question parameters (e.g., loaded_rules for
    eviction; threshold_volume + work_mode for threshold-meaningful)

The server returns:
  - baseline classification (hyperactive/normal/hypoactive)
  - eviction priority for one rule
  - selected rule IDs to evict (n lowest-priority)
  - work mode inference (deploy/deep/exploratory/normal)
  - threshold-meaningful boolean (does this token-volume warrant surfacing?)
  - metabolism nudge body (priority-ordered by recent threshold crossings)

Decision logic mirrors plugin/lib/metabolism_runtime.py verbatim.
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

PILLAR_ID = "metabolism"


# ============================================================================
# Constants — locked to plugin/lib/metabolism_runtime.py.
# ============================================================================

# Eviction-priority tier table.
TIER_PRIORITY: dict[str, int] = {
    "innate": 1000,
    "operator-innate": 900,
    "umbrella": 700,
    "user": 500,
    "project": 300,
    "outside-matrix": 100,
    "session": 50,
}

# Work-mode classification signatures.
WORK_MODE_SIGNATURES: dict[str, set[str]] = {
    "deploy": {
        "rollout_detected", "deploy_event",
        "wrangler_publish", "git_push_main",
        "package_published", "package_uploaded",
    },
    "deep": {
        "file_edit", "file_write", "code_change",
        "destructive_command_attempt", "innate_violation_attempt",
        "rule_promoted", "fork_created",
    },
    "exploratory": {
        "memory_read", "doc_read", "audit_run",
        "session_start", "calibration_query",
        "file_read",
    },
}

# Baseline-classifier event sets.
HIGH_STAKES_EVENTS: set[str] = {
    "rollout_detected", "deploy_event", "destructive_command_attempt",
    "innate_violation_attempt", "rule_promoted", "fork_created",
}
EXPLORATORY_EVENTS: set[str] = {
    "memory_read", "doc_read", "audit_run",
    "session_start", "calibration_query",
}

# Token-volume thresholds — locked to scripts/token_check.py THRESHOLDS.
TOKEN_THRESHOLDS: tuple[int, ...] = (
    50_000, 150_000, 300_000, 500_000, 700_000, 750_000,
)

# METABOLISM_SKILL_MAP + compose_metabolism_nudge removed (D2, 2026-06-05): the
# token-threshold skill-hint ladder was a dead duplicate of innate rule 05's
# handoff-checkpoint path. Rule 05 is now the single token-threshold surface —
# gentle 50k-500k (with these skill hints folded into its templates) + the hard
# 700k/750k red-box.


# ============================================================================
# Baseline classifier
# ============================================================================


def compute_baseline(
    observations: list[dict[str, Any]],
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> str:
    """Compute the rolling baseline metabolic rate.

    Mirrors plugin/lib/metabolism_runtime.py::compute_baseline — operates
    on wrapper-supplied observations instead of reading observations.jsonl.

    Returns "hyperactive" | "normal" | "hypoactive".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=window_days)

    high_stakes_count = 0
    exploratory_count = 0
    total = 0

    for event in observations:
        ts = _parse_iso(event.get("timestamp"))
        if ts is None or ts < threshold:
            continue
        total += 1
        event_type = event.get("event", "")
        if event_type in HIGH_STAKES_EVENTS:
            high_stakes_count += 1
        elif event_type in EXPLORATORY_EVENTS:
            exploratory_count += 1

    if total < 5:
        return "normal"

    if high_stakes_count / total > 0.3:
        return "hyperactive"
    if exploratory_count / total > 0.6:
        return "hypoactive"
    return "normal"


# ============================================================================
# Eviction priority + selection
# ============================================================================


def eviction_priority(
    tier: str,
    *,
    recently_used: bool = False,
    recently_violated: bool = False,
) -> int:
    """Compute eviction priority. Higher = keep loaded.

    Mirrors plugin/lib/metabolism_runtime.py::eviction_priority:
      base = TIER_PRIORITY[tier]
      +200 when recently_used
      +500 when recently_violated
    """
    base = TIER_PRIORITY.get(tier, 0)
    boost = 0
    if recently_used:
        boost += 200
    if recently_violated:
        boost += 500
    return base + boost


def select_rules_to_evict(
    loaded_rules: list[dict[str, Any]],
    *,
    target_eviction_count: int,
) -> list[str]:
    """Pick lowest-priority rules to evict, never evicting innate-* rules.

    Mirrors plugin's select_rules_to_evict.
    """
    # Entries without an `id` can't be evicted by id — skip them instead of
    # KeyError-ing the whole eviction pass (deep-audit 2026-07-02).
    scored = [
        (
            r["id"],
            eviction_priority(
                r.get("tier", "user"),
                recently_used=bool(r.get("recently_used", False)),
                recently_violated=bool(r.get("recently_violated", False)),
            ),
        )
        for r in loaded_rules
        if isinstance(r, dict) and r.get("id")
    ]
    # Never evict innate.
    scored = [s for s in scored if not s[0].startswith("innate-")]
    scored.sort(key=lambda x: x[1])
    return [rule_id for rule_id, _ in scored[:max(0, target_eviction_count)]]


# ============================================================================
# Context pressure
# ============================================================================


def context_pressure(loaded_rule_count: int, *, ceiling: int = 50) -> float:
    """Context pressure as fraction of soft ceiling.

    Mirrors plugin's context_pressure.
    """
    if ceiling <= 0:
        return 0.0
    return loaded_rule_count / ceiling


# ============================================================================
# Work-mode inference
# ============================================================================


def infer_work_mode(
    observations: list[dict[str, Any]],
    *,
    session_window_events: int = 30,
) -> str:
    """Classify the active session's work mode.

    Mirrors plugin/lib/metabolism_runtime.py::infer_work_mode — but takes
    observations as parameter (wrapper supplies the last N).
    Returns one of "deploy" | "deep" | "exploratory" | "normal".
    """
    counts = {"deploy": 0, "deep": 0, "exploratory": 0, "other": 0}
    sampled = 0

    # Walk newest-first up to session_window_events. Check the cap BEFORE
    # counting (deep-audit 2026-07-02): the old increment-then-break counted
    # the (N+1)th never-classified event into `sampled`, inflating the
    # 30%-of-sampled threshold denominator by one.
    for event in reversed(observations):
        if sampled >= session_window_events:
            break
        sampled += 1
        event_type = event.get("event", "")
        classified = False
        for mode, signatures in WORK_MODE_SIGNATURES.items():
            if event_type in signatures:
                counts[mode] += 1
                classified = True
                break
        if not classified:
            counts["other"] += 1

    if sampled < 3:
        return "normal"

    deploy = counts["deploy"]
    deep = counts["deep"]
    exploratory = counts["exploratory"]

    threshold = max(3, int(0.30 * sampled))

    if deploy >= threshold:
        return "deploy"
    if deep >= threshold:
        return "deep"
    if exploratory >= threshold and exploratory > deep and exploratory > deploy:
        return "exploratory"
    return "normal"


# ============================================================================
# Threshold-meaningful decision
# ============================================================================


def threshold_meaningful(
    threshold_volume: int,
    *,
    baseline: str = "normal",
    work_mode: str = "normal",
) -> bool:
    """Decide whether a token-volume threshold crossing warrants surfacing.

    Mirrors plugin/lib/metabolism_runtime.py::threshold_meaningful:
      500k+: always meaningful
      300k:  meaningful unless explicitly exploratory
      50k/150k: meaningful when work_mode in (deep, deploy) OR baseline hyperactive
      else: default True
    """
    if threshold_volume >= 500_000:
        return True
    if threshold_volume == 300_000:
        return work_mode != "exploratory"
    if threshold_volume in (50_000, 150_000):
        return work_mode in ("deep", "deploy") or baseline == "hyperactive"
    return True


# ============================================================================
# Status-line formatter
# ============================================================================


def format_metabolic_status_line(
    *,
    baseline: str = "normal",
    work_mode: str = "normal",
) -> str:
    """Format the metabolism status line.

    Mirrors plugin/lib/metabolism_runtime.py::format_metabolic_status_line.
    """
    armed: list[str] = []
    suppressed: list[str] = []
    for tk in TOKEN_THRESHOLDS:
        suppress = tk == 50_000 and work_mode in ("exploratory", "normal")
        label = f"{tk // 1000}k"
        if suppress:
            suppressed.append(label)
        else:
            armed.append(label)

    if armed and suppressed:
        threshold_summary = (
            f"armed at {'/'.join(armed)} ({'/'.join(suppressed)} suppressed)"
        )
    elif armed:
        threshold_summary = f"armed at {'/'.join(armed)}"
    else:
        threshold_summary = (
            "all thresholds suppressed (rare — operator should override)"
        )
    return f"Metabolism: {work_mode} mode · baseline {baseline} · {threshold_summary}"


# ============================================================================
# MCP tool dispatch
# ============================================================================


def metabolism_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below."""
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        details = question.details or {}

        if qt == "compute_baseline":
            window = coerce_int(details.get("window_days"), 30)
            baseline = compute_baseline(
                excerpt.observations, window_days=window
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="baseline_classified",
                    body=f"Metabolic baseline: `{baseline}` (over {window}d).",
                    details={"baseline": baseline, "window_days": window},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=baseline,
                ),
            )

        if qt == "eviction_priority":
            tier = details.get("tier", "user")
            priority = eviction_priority(
                tier,
                recently_used=bool(details.get("recently_used", False)),
                recently_violated=bool(details.get("recently_violated", False)),
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="priority_computed",
                    body=f"Eviction priority `{priority}` for tier `{tier}`.",
                    details={"priority": priority, "tier": tier},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="priority_computed",
                ),
            )

        if qt == "select_rules_to_evict":
            loaded = details.get("loaded_rules", [])
            target = coerce_int(details.get("target_eviction_count"), 0)
            if not isinstance(loaded, list):
                return _no_op(
                    decision_class="missing_loaded_rules",
                    reason="question.details.loaded_rules (list) required",
                )
            to_evict = select_rules_to_evict(
                loaded, target_eviction_count=target
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="evictions_selected" if to_evict else "no_op",
                    body=(
                        f"{len(to_evict)} rule(s) proposed for eviction (target={target})."
                        if to_evict else None
                    ),
                    details={
                        "evict": to_evict,
                        "evict_count": len(to_evict),
                        "target": target,
                    },
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=(
                        "evictions_proposed" if to_evict else "no_evictions"
                    ),
                ),
            )

        if qt == "context_pressure":
            count = coerce_int(details.get("loaded_rule_count"), 0)
            ceiling = coerce_int(details.get("ceiling"), 50)
            pressure = context_pressure(count, ceiling=ceiling)
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="pressure_computed",
                    body=f"Context pressure: `{pressure}` ({count}/{ceiling} rules).",
                    details={"pressure": pressure, "ceiling": ceiling},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="pressure_computed",
                ),
            )

        if qt == "infer_work_mode":
            window = coerce_int(details.get("session_window_events"), 30)
            mode = infer_work_mode(
                excerpt.observations,
                session_window_events=window,
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="work_mode_inferred",
                    body=f"Work mode inferred: `{mode}`.",
                    details={"work_mode": mode},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=mode,
                    fingerprint=mode,
                ),
            )

        if qt == "threshold_meaningful":
            threshold_volume = coerce_int(details.get("threshold_volume"), 0)
            baseline = details.get("baseline", "normal")
            work_mode = details.get("work_mode", "normal")
            meaningful = threshold_meaningful(
                threshold_volume, baseline=baseline, work_mode=work_mode
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="threshold_meaningful" if meaningful else "threshold_suppressed",
                    body=(
                        f"Threshold {threshold_volume} meaningful "
                        f"(baseline=`{baseline}`, work_mode=`{work_mode}`)."
                        if meaningful else
                        f"Threshold {threshold_volume} suppressed "
                        f"(baseline=`{baseline}`, work_mode=`{work_mode}`)."
                    ),
                    details={
                        "meaningful": meaningful,
                        "threshold_volume": threshold_volume,
                        "baseline": baseline,
                        "work_mode": work_mode,
                    },
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=(
                        "meaningful" if meaningful else "suppressed"
                    ),
                ),
            )

        if qt == "format_metabolic_status_line":
            baseline = details.get("baseline", "normal")
            work_mode = details.get("work_mode", "normal")
            line = format_metabolic_status_line(baseline=baseline, work_mode=work_mode)
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="status_line",
                    body=line,
                    details={"baseline": baseline, "work_mode": work_mode},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="status_rendered",
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
        name="metabolism_evaluate",
        description=(
            "Metabolism pillar — token-budget regulation. "
            "Hybrid: token-state inference is wrapper-side; server "
            "aggregates signals + makes threshold decisions. "
            "question_type ∈ {compute_baseline, eviction_priority, "
            "select_rules_to_evict, context_pressure, infer_work_mode, "
            "threshold_meaningful, format_metabolic_status_line}."
        ),
    )
    def metabolism_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return metabolism_decision(excerpt, question)
