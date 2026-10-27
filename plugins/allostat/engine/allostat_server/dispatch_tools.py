"""Server-side dispatch tools — v0.2.0 Architecture C.

Each `dispatch_*` tool replaces the wrapper's per-hook classify-then-loop
pattern with a single server-side aggregation. The wrapper hook makes one
MCP call per hook event; the dispatch tool internally classifies, invokes
relevant pillars, and returns one `DispatchResponse` containing:
  - aggregated `additional_context` text the wrapper renders to operator
  - union of `client_state_writes` from all fired pillars
  - `pillars_invoked` list (telemetry — also records via record_pillar_invocation)
  - per-pillar `decisions` (debug)

This module is the load-bearing v0.2.0 change: it eliminates the wrapper's
classifier.py + the 1:120 dispatch gate at post-tool-use.py:59. Architecture
C: thin shim hooks forward events; server classifies + invokes pillars.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ._time import utc_now_iso as _utc_now_iso
from .config import DEDUP_WINDOW_SECONDS as _DEDUP_WINDOW_SECONDS
from .observation_helpers import observe_caught_exception
from .runtime_context import local_dedup_state, local_execution
from .pillar_protocol import (
    ClientStateWrite,
    DecisionPayload,
    DispatchResponse,
    ExcerptPayload,
    QuestionPayload,
)
from .pillars import (
    hypothalamic_axis,
    innate_enforcer,
    metabolism,
    onboarding_interview,
    pattern_observer,
    recall_silos,
    stress_response,
    voice_keeper,
    volume_control,
)


# ============================================================================
# Pillar invocation table
# ============================================================================


# Each pillar exposes a pure `<name>_decision(excerpt, question)` function.
# Dispatch tools call these directly rather than going through MCP — same
# process, no transport overhead, ContextVar + record_pillar_invocation
# still fire (each pillar wraps its decision in those).
PILLAR_DECISIONS = {
    "hypothalamic-axis": hypothalamic_axis.hypothalamic_axis_decision,
    "innate-enforcer": innate_enforcer.innate_enforcer_decision,
    "stress-response": stress_response.stress_response_decision,
    "pattern-observer": pattern_observer.pattern_observer_decision,
    "recall-silos": recall_silos.recall_silos_decision,
    "voice-keeper": voice_keeper.voice_keeper_decision,
    "onboarding-interview": onboarding_interview.onboarding_interview_decision,
    "volume-control": volume_control.volume_control_decision,
    "metabolism": metabolism.metabolism_decision,
}


def _count_legacy_files(observations: list[dict[str, Any]]) -> int:
    """Sum file_count across legacy_aging observations in the session tail.

    Used by every dispatcher call site that invokes volume-control's
    compose_volume_control_nudge — the legacy_aging branch reads
    details.legacy_file_count and falls through to no_op when None/0.
    Centralizing extraction here keeps every dispatch endpoint consistent
    (Stop, UserPromptSubmit, etc.) instead of each site re-inlining the
    same loop and risking missed sites (queue item #22 root cause).

    Returns 0 when no legacy_aging events present — caller passes empty
    details in that case so the handler's legacy branch correctly stays
    quiet.
    """
    total = 0
    for ev in observations:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "legacy_aging":
            continue
        details = ev.get("details") or {}
        if not isinstance(details, dict):
            continue
        try:
            total += int(details.get("file_count", 0))
        except (TypeError, ValueError):
            pass
    return total


def _volume_control_details_from_obs(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose the details dict for compose_volume_control_nudge from
    the session's observation tail. Returns {} when there's nothing to
    extract — handler then falls through to quiet no_op."""
    legacy_count = _count_legacy_files(observations)
    if legacy_count > 0:
        return {"legacy_file_count": legacy_count}
    return {}


def _compose_stress_nudge_decision(
    excerpt: ExcerptPayload,
) -> tuple[str, DecisionPayload | None]:
    """Build the compose_stress_nudge invocation from the observation tail.

    Shared by dispatch_user_prompt_submit_logic + dispatch_post_tool_use_logic
    (pre-release item 25b — the ~35-line W6 block was copy-pasted at both
    sites). W6 (pillar-wiring 2026-06-05) — feed the dark sensitization
    branches. Pre-W6 the dispatchers passed only event_counts, leaving
    compose_stress_nudge's drift_recurrence (branch 1) and idle_resumption
    (branch 5) silently dead via the dispatcher (2 of 6 branches). The
    stress handler already forwards these params — only the dispatchers
    omitted them. Derive from the observations the server already has:
      - current_session_activity = the observation window → enables
        drift_recurrence (fires once excerpt.corrections is populated
        wrapper-side; that corrections feed is the remaining half).
      - last_event_iso = newest observation timestamp (ISO-8601 strings
        sort lexically) → closes idle_resumption (branch 5) server-side.
    drift_seen_entries stays None here (wrapper-local salience state; the
    detector runs without it — just no salience suppression).
    """
    event_counts: dict[str, int] = {}
    for ev in excerpt.observations:
        et = ev.get("event")
        if isinstance(et, str):
            event_counts[et] = event_counts.get(et, 0) + 1
    _obs_timestamps = [
        ev.get("timestamp")
        for ev in excerpt.observations
        if isinstance(ev, dict) and isinstance(ev.get("timestamp"), str)
    ]
    _last_event_iso = max(_obs_timestamps) if _obs_timestamps else None
    return (
        "stress-response",
        _invoke_pillar(
            "stress-response", excerpt, "compose_stress_nudge",
            {
                "event_counts": event_counts,
                "current_session_activity": list(excerpt.observations),
                "last_event_iso": _last_event_iso,
            },
        ),
    )


def _invoke_pillar(
    pillar: str,
    excerpt: ExcerptPayload,
    question_type: str,
    details: dict[str, Any] | None = None,
) -> DecisionPayload | None:
    """Invoke one pillar's pure decision function and return its
    DecisionPayload. Returns None if the pillar is unknown or the pillar
    raises (defense-in-depth — one pillar's bug must not crash the entire
    dispatch).
    """
    decision_fn = PILLAR_DECISIONS.get(pillar)
    if decision_fn is None:
        return None
    question = QuestionPayload(
        pillar=pillar,
        question_type=question_type,
        details=details or {},
    )
    try:
        response = decision_fn(excerpt, question)
    except Exception as exc:
        # ITEM D (Phase 7): defense-in-depth still holds (one pillar's bug
        # must not crash dispatch), but the crash is no longer indistinguishable
        # from a clean no-decision — log which pillar failed + the error.
        observe_caught_exception(
            "dispatch", "pillar_invocation", exc,
            pillar=pillar, question_type=question_type,
        )
        return None
    return response.decision


def _safe_pillar_compute(
    label: str,
    compute_fn: Any,
    observations: list[dict[str, Any]],
    default: Any,
) -> Any:
    """Call a pure pillar helper (compute_thriving_signal / compute_baseline /
    infer_work_mode) on raw observations, degrading to `default` if it raises.

    H-34 (exception-isolation): these three helpers are invoked DIRECTLY on
    excerpt.observations (they return plain str/bool, not DecisionPayloads, so
    they can't go through PILLAR_DECISIONS / _invoke_pillar). Without this
    guard a single malformed observation (e.g. `details` as a non-dict string,
    which makes compute_thriving_signal's `(event.get("details") or {}).get(...)`
    raise AttributeError) would escape the async handler and crash the whole
    dispatch — unlike every other pillar call, which _invoke_pillar isolates.
    Mirrors _invoke_pillar's try/except + observe_caught_exception contract.
    """
    try:
        return compute_fn(observations)
    except Exception as exc:
        observe_caught_exception(
            "dispatch", "pillar_compute", exc, compute=label,
        )
        return default


def _aggregate(
    decisions: list[tuple[str, DecisionPayload | None]],
) -> DispatchResponse:
    """Combine per-pillar decisions into a single DispatchResponse.

    Filters out None decisions. Joins bodies into `additional_context`,
    unions `client_state_writes` from every pillar, and records pillars
    that contributed (skipping no_op / no_match decisions for nudge-text
    purposes but still listing them in pillars_invoked since they ran).

    v0.2.0 architectural commitment: every nudge that surfaces ALSO produces
    a `nudge_history.jsonl` append write so the operator has a persistent
    audit of regulator activity. Individual pillars can additionally emit
    their own client_state_writes (e.g., pattern-observer's pending_proposals).

    PATCH-148 (2026-05-22): Skill-invocation directive REMOVED. The
    directive that previously appended to nudge text was redundant
    (the pillar firing IS the felt-presence anchor) AND broken (the
    named skills `allostat-mcp:<pillar>` were dropped from the wrapper
    skill manifest in PATCH-140 v1.1.0 prune; the directive instructed
    Claude to invoke skills that no longer exist). Per advisor pass-2
    paragraph 2 (2026-05-22): "Allostat is environmental, not coercive."
    """
    nudge_lines: list[str] = []
    state_writes: list[ClientStateWrite] = []
    pillars_invoked: list[str] = []
    flat_decisions: list[DecisionPayload] = []

    for pillar, decision in decisions:
        if decision is None:
            continue
        flat_decisions.append(decision)
        if pillar not in pillars_invoked:
            pillars_invoked.append(pillar)
        body = decision.body
        if body and decision.action not in ("no_op", "no_match"):
            nudge_lines.append(f"[allostat / {pillar}] {body}")
            # v0.2.0: persist every fired nudge to nudge_history.jsonl so
            # the operator can audit regulator activity across sessions.
            state_writes.append(ClientStateWrite(
                target="nudge_history.jsonl",
                mode="append",
                content={
                    "timestamp": _utc_now_iso(),
                    "pillar": pillar,
                    "action": decision.action,
                    "body": body,
                    "details": decision.details,
                },
                pillar_id=pillar,
            ))
        if decision.client_state_writes:
            state_writes.extend(decision.client_state_writes)

    text = "\n\n".join(nudge_lines)
    # PATCH-148 (2026-05-22): skill-invocation directive removed. Pillar
    # firing IS the felt-presence anchor; the directive was redundant +
    # broken (named missing skills post-PATCH-140 manifest prune).
    # Advisor pass-2 §2: "Allostat is environmental, not coercive."

    return DispatchResponse(
        additional_context=text,
        client_state_writes=state_writes,
        pillars_invoked=pillars_invoked,
        decisions=flat_decisions,
    )



# ============================================================================
# PATCH-149 (2026-05-22) — cascade dedup
# ============================================================================
#
# Per advisor 2026-05-22 bundled response section 2 + audit Phase 3 FUNC-1
# (CRITICAL). Same-body decision firing every prompt produced the
# "stress: too_many_edits" noise the audit identified. v2.3 plugin had
# session_dedup logic; v0.2.0 architecture didn't carry it forward.
# PATCH-132 STATE.md entry described this but no code shipped.
#
# Mechanism: per-(session_id, pillar, body_fingerprint) suppression with
# a TTL window. Module-level RAM state; server restart clears. Decisions
# suppressed by dedup become no_op_dedup_suppressed (telemetry preserved).

_DEDUP_STATE: dict[tuple[str, str, str], datetime] = {}
# Timestamp of the last expired-entry sweep (GC throttle — see
# _filter_decisions_by_dedup). None = never swept / just reset.
_DEDUP_GC_LAST: datetime | None = None
# Window (default 300s — v2.3 standard) lives in config.DEDUP_WINDOW_SECONDS
# (pre-release item 35), imported above as _DEDUP_WINDOW_SECONDS.


def _hash_decision_body(body: str) -> str:
    """16-char sha256 prefix of decision body for dedup fingerprinting."""
    return hashlib.sha256(
        (body or "").encode("utf-8", errors="ignore")
    ).hexdigest()[:16]


def _filter_decisions_by_dedup(
    session_id: str | None,
    decisions: list[tuple[str, DecisionPayload | None]],
    *,
    now: datetime | None = None,
    window_seconds: int = _DEDUP_WINDOW_SECONDS,
) -> list[tuple[str, DecisionPayload | None]]:
    """Suppress same-(session, pillar, body) decisions that fired
    within the dedup window. Returns a NEW list with suppressed entries
    converted to no_op_dedup_suppressed (so telemetry sees they ran).

    Without session_id: passes through unchanged (no_dedup mode).
    Already-silent decisions (None, no_op, no_match, empty body): passed
    through unchanged.
    """
    if not session_id:
        return decisions
    if local_execution.get() and local_dedup_state.get() is None:
        from .local_dedup import transaction
        with transaction(now=now, window_seconds=window_seconds):
            return _filter_decisions_by_dedup(
                session_id, decisions, now=now, window_seconds=window_seconds
            )
    # H-33 (exception-isolation): the dedup key is (session_id, pillar,
    # fingerprint); a non-hashable session_id (JSON array/object arriving on
    # the wire) makes `_DEDUP_STATE.get(key)` raise "unhashable type" and
    # crashes the async handler. Callers only guard `if not session_id`, so
    # coerce anything non-str to a stable str form — dedup keeps working
    # (same value → same key) instead of taking down the dispatch.
    if not isinstance(session_id, str):
        session_id = str(session_id)
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window_seconds)

    # The hosted service owns its process cache. Short-lived local hooks use
    # an adapter-owned transaction so the same five-minute suppression rule
    # survives process exits and concurrent hook invocations.
    local_state = local_dedup_state.get()
    dedup_state = _DEDUP_STATE if local_state is None else local_state

    filtered: list[tuple[str, DecisionPayload | None]] = []
    for pillar, decision in decisions:
        if decision is None or not decision.body:
            filtered.append((pillar, decision))
            continue
        if decision.action in ("no_op", "no_match"):
            filtered.append((pillar, decision))
            continue
        fingerprint = _hash_decision_body(decision.body)
        key = (session_id, pillar, fingerprint)
        last_fired = dedup_state.get(key)
        if last_fired is not None and last_fired > cutoff:
            # Suppressed — convert to no_op_dedup_suppressed to maintain
            # telemetry visibility for forensics.
            filtered.append((pillar, DecisionPayload(
                action="no_op_dedup_suppressed",
                details={
                    "dedup_pillar": pillar,
                    "dedup_fingerprint": fingerprint,
                    "dedup_window_seconds": window_seconds,
                    "suppressed_action": decision.action,
                },
            )))
            continue
        dedup_state[key] = now
        filtered.append((pillar, decision))

    # Garbage-collect expired entries to keep state bounded. Throttled to at
    # most once per dedup window (fixpass 2026-07-01): the full-dict scan is
    # O(n) and this function runs on hot dispatch paths. Correctness never
    # needed the sweep — stale entries are already ignored by the cutoff
    # comparison above — only memory boundedness does, and once-per-window
    # bounds the dict just as well.
    global _DEDUP_GC_LAST
    if local_state is not None or _DEDUP_GC_LAST is None or (now - _DEDUP_GC_LAST) >= timedelta(seconds=window_seconds):
        expired = [k for k, ts in dedup_state.items() if ts <= cutoff]
        for k in expired:
            del dedup_state[k]
        if local_state is None:
            _DEDUP_GC_LAST = now

    return filtered


def _reset_dedup_state() -> None:
    """Test helper — force the dedup state to clear between unit tests."""
    global _DEDUP_GC_LAST
    _DEDUP_STATE.clear()
    _DEDUP_GC_LAST = None


# ============================================================================
# Classifier helpers (ported from wrapper's classifier.py)
# ============================================================================


# PATCH-189 (2026-05-23): Layer A observation classifier refinement.
# Mirrors wrapper-side `user-prompt-submit.py::_CORRECTION_PATTERNS`. If
# either side updates, update BOTH. See wrapper-side comment for the
# Phase 2 Investigation 1 evidence (11 events tagged operator_correction,
# only 1 was an actual correction) that motivated the refinement.
_CORRECTION_PATTERNS: tuple[re.Pattern, ...] = (
    # Phase 3.1: negation imperatives anchored to clause start. Pre-Phase-3.1
    # bare `\b(don'?t|...)\b` matched "don't worry" embedded mid-prompt,
    # "stop button" in code discussion, "reset the DB" as task instruction.
    # Now requires the imperative to OPEN a clause (prompt start or after
    # sentence-end punctuation) — the natural position for operator corrections.
    re.compile(
        r"(?:^|[\.\!\?]\s+)\s*(don'?t|do not|stop|never|avoid)\s+\w+",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Phase 3.1: reset / undo / revert / scratch that — anchored AND
    # require correction-anaphor suffix. Pre-Phase-3.1 caught task
    # instructions like "revert PR #123", "reset the database". Bare
    # "reset" dropped — too ambiguous (reset DB, reset state, reset branch).
    re.compile(
        r"(?:^|[\.\!\?]\s+)\s*("
        r"scratch that"
        r"|undo(?=[\.\!\?,]|$)"
        r"|undo\s+(?:that|the\s+(?:last|change|commit|edit))"
        r"|revert(?=[\.\!\?,]|$)"
        r"|revert\s+(?:that|the\s+(?:last|change|commit|edit))"
        r")\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    # PATCH-189: polite correction wrappers (narrow). Replaces the
    # pre-PATCH-189 broad `please\s+\w+` which matched every task prompt.
    re.compile(
        r"\bplease\s+(stop|don'?t|do not|avoid|never|use less|use fewer|cut|remove|skip)\b",
        re.IGNORECASE,
    ),
    # PATCH-189: explicit-correction language directed at Claude.
    re.compile(
        r"\b(you should have|you shouldn'?t|you should not|you'?re wrong|that'?s incorrect|wrong about|not what i)\b",
        re.IGNORECASE,
    ),
    # PATCH-189: first-person preference assertion.
    re.compile(r"\bi(?:'?d)?\s+prefer\b", re.IGNORECASE),
    # PATCH-189: standalone "no," / "no." at prompt start (excludes "no problem"
    # / "no worries"). Catches "no, X is wrong" / "no. use Y". Deep-audit
    # 2026-07-02: require terminating punctuation (was `no[,.\s]+`, which fired
    # on "no rush"/"no need to"). Mirror of the wrapper-side pattern — keep both
    # in lockstep.
    re.compile(
        r"^\s*no[,.]\s*(?!problem|worry|worries|big deal)\w+",
        re.IGNORECASE,
    ),
    # ---- Disagreement shapes (2026-08-07) — mirror of the wrapper's table ----
    #
    # The detector was keyed almost entirely on imperatives, and measured
    # accordingly: it fired on two prompts that were instructions and missed
    # all three genuine corrections, which arrived as disagreement. That is how
    # corrections usually arrive — someone who thinks you are wrong says so,
    # they do not issue an order. All three shapes point BACKWARD at something
    # already said, which is what separates a correction from a task.
    re.compile(
        r"\b(?:that|this|it)(?:'?s|\s+is|\s+was)?\s*(?:n[o']?t\s+)?"
        r"(?:wrong|incorrect|inaccurate|backwards|mistaken)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:that|this|it)(?:'?s|\s+is)\s+not\s+(?:right|correct|true|accurate)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byou(?:'?re|\s+are|'?ve|\s+have)\s+"
        r"(?:confus(?:ing|ed)|mix(?:ing|ed)\s+up|conflat(?:ing|ed)|"
        r"misread(?:ing)?|misremember(?:ing|ed))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:that|this|it)\s+(?:was|used\s+to\s+be)\s+"
        r"(?:true|right|correct|the\s+case)\b"
        r"(?=[^.!?\n]{0,120}\b(?:but|however|though|yet|now|since|"
        r"no\s+longer|not\s+anymore|isn'?t|is\s+not|changed|moved|"
        r"renamed|replaced|removed)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:"
        r"(?:that|this|it)(?:'?s|\s+is|\s+was)\s+(?:now\s+)?out\s+of\s+date"
        r"|(?:that|this|it)(?:'?s|\s+is)?(?:\s+\w+){0,3}\s+no\s+longer\s+"
        r"(?:true|correct|the\s+case|applies)"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:that'?s\s+not\s+what\s+i\b|i\s+never\s+(?:said|asked|told))",
        re.IGNORECASE,
    ),
)


# A prompt that OPENS by assigning a role or framing a task is a task, however
# many negation imperatives follow (2026-08-07). Both benchmark false fires were
# the harness's own instruction prompts — "Do NOT answer from the index alone"
# is a clause-initial negation imperative, and an instruction about the task
# ahead rather than a correction of anything already done. Corrections point
# backward; task setups point forward. Mirror of the wrapper's table.
_TASK_FRAMING_OPENERS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"^\s*you\s+are\s+(?:an?|the)\s+\w+",
        r"^\s*you\s+have\s+(?:persistent|access\s+to|been\s+given)\b",
        r"^\s*your\s+task\s+is\b",
        r"^\s*below\s+is\b",
        r"^\s*answer\s+the\s+(?:question|following)\b",
        r"^\s*(?:earlier|prior|previous)\s+sessions?\s+with\s+you\b",
    )
)


# PATCH-189 / Phase 3.1: blacklist filter for Claude Code internal events.
_NON_CORRECTION_PREFIX_BLACKLIST: tuple[str, ...] = (
    "<task-notification>",
    "<system-reminder>",
    "<command-message>",
    "<command-name>",
    "<bash-",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "[Request interrupted by user]",
)


# Single-sourced from the metabolism pillar (pre-release item 25a) — that
# copy is locked to scripts/token_check.py THRESHOLDS; aliasing it here
# keeps the dispatcher in lockstep by construction.
TOKEN_THRESHOLDS: tuple[int, ...] = metabolism.TOKEN_THRESHOLDS


def _is_operator_correction(prompt: str) -> bool:
    """True iff the prompt looks like an operator correction.

    Mirrors wrapper-side `user-prompt-submit.py::_is_operator_correction_prompt`.
    PATCH-189 adds a blacklist gate so Claude Code internal events don't get
    fingerprinted as operator corrections."""
    if not prompt:
        return False
    p_lstrip = prompt.lstrip()
    if any(p_lstrip.startswith(prefix) for prefix in _NON_CORRECTION_PREFIX_BLACKLIST):
        return False
    if any(p.search(p_lstrip) for p in _TASK_FRAMING_OPENERS):
        return False
    return any(p.search(prompt) for p in _CORRECTION_PATTERNS)


def _threshold_crossed(
    cumulative_tokens: int, last_surfaced: int = 0
) -> int | None:
    """Return the highest token threshold crossed at this count that is
    strictly greater than `last_surfaced`, or None.

    PATCH-140 v1.1.0 (Lever D): the prior implementation returned the
    highest crossed threshold on every prompt where cumulative >= threshold,
    so once at 60k the 50k red box fired every subsequent turn for ~90k of
    further work. Now: pass `last_surfaced` from the wrapper's state.json
    `last_surfaced_threshold` field, and the rule fires once per band per
    session. Wrapper writes state.json after a successful red-box emit.

    last_surfaced=0 is the session-start default — first crossing of any
    band still fires normally.
    """
    crossed: int | None = None
    for tk in TOKEN_THRESHOLDS:
        if cumulative_tokens >= tk and tk > last_surfaced:
            crossed = tk
    return crossed


# ============================================================================
# Per-hook dispatch logic
# ============================================================================


def dispatch_session_start_logic(
    excerpt: ExcerptPayload, event: dict[str, Any]
) -> DispatchResponse:
    """SessionStart dispatch — state-decision + dawn-phase context.

    event keys (all optional, default False/None):
      has_allostat_state: bool — operator has .allostat/state.json
      has_existing_context: bool — operator has CLAUDE.md / memory
      has_other_projects: bool
      local_only: bool — ALLOSTAT_LOCAL_ONLY env override

    v0.2.6 PATCH-128 A4: also surfaces excerpt.pending_dusk_surface entries
    persisted by the previous session's dispatch_stop (Claude Code's Stop
    event silently drops additionalContext, so dusk content rides this
    dawn surface instead).
    """
    decisions: list[tuple[str, DecisionPayload | None]] = []
    decisions.append((
        "hypothalamic-axis",
        _invoke_pillar(
            "hypothalamic-axis", excerpt, "session_start_state",
            {
                "has_allostat_state": bool(event.get("has_allostat_state", False)),
                "has_existing_context": bool(event.get("has_existing_context", False)),
                "has_other_projects": bool(event.get("has_other_projects", False)),
                "local_only": bool(event.get("local_only", False)),
            },
        ),
    ))
    response = _aggregate(decisions)

    # PATCH-128 A4: surface pending dusk content from last session's Stop.
    dusk = list(excerpt.pending_dusk_surface or [])
    if dusk:
        dusk_text = _compose_dawn_dusk_surface(dusk)
        if dusk_text:
            existing = response.additional_context or ""
            response.additional_context = (
                (existing + "\n\n" + dusk_text) if existing else dusk_text
            )
            # The hypothalamic-axis skill is already cited in any aggregate
            # directive above; the dusk content is contextual carry-over,
            # not a new pillar firing — no extra directive needed.

    return response


def _compose_dawn_dusk_surface(dusk: list[dict[str, Any]]) -> str:
    """Render the previous session's pending dusk-phase content as a
    dawn-phase carry-over block. Each entry's body is preserved verbatim;
    we just add a section header so the operator knows it's carryover.
    """
    if not dusk:
        return ""
    lines: list[str] = ["[allostat / dawn-phase] Carryover from previous session:"]
    for entry in dusk:
        body = str(entry.get("body", "")).strip()
        if not body:
            continue
        pillar = entry.get("pillar")
        if pillar:
            lines.append(f"  • ({pillar}) {body}")
        else:
            lines.append(f"  • {body}")
    if len(lines) == 1:
        return ""  # Header only, no actual content
    return "\n".join(lines)


def dispatch_user_prompt_submit_logic(
    excerpt: ExcerptPayload, event: dict[str, Any]
) -> DispatchResponse:
    """UserPromptSubmit dispatch — token thresholds, correction detection,
    drift baseline.

    event keys:
      prompt: str (up to 500 chars; operator-consented per round4 brief Â§3.1)
      cumulative_tokens: int | None
    """
    decisions: list[tuple[str, DecisionPayload | None]] = []
    prompt = event.get("prompt") or ""
    if not isinstance(prompt, str):
        prompt = ""
    # Enforce the documented cap server-side: the operator consented to
    # <=500-char prompt capture (round4 brief §3.1) — never trust the
    # wrapper to truncate.
    prompt = prompt[:500]
    cumulative_tokens = event.get("cumulative_tokens")

    # 1. Token-threshold crossing â†’ innate-enforcer
    # PATCH-140 v1.1.0 (Lever D): event carries `last_surfaced_threshold`
    # so the rule fires once per band per session (was: every turn).
    if isinstance(cumulative_tokens, (int, float)):
        last_surfaced_raw = event.get("last_surfaced_threshold", 0)
        try:
            last_surfaced = int(last_surfaced_raw) if last_surfaced_raw else 0
        except (TypeError, ValueError):
            last_surfaced = 0
        threshold = _threshold_crossed(
            int(cumulative_tokens), last_surfaced=last_surfaced
        )
        if threshold is not None:
            decisions.append((
                "innate-enforcer",
                _invoke_pillar(
                    "innate-enforcer", excerpt, "check_signal",
                    {
                        "signal_type": "usage_threshold_crossed",
                        "threshold": threshold,
                    },
                ),
            ))

    # 2. Operator correction â†’ pattern-observer fingerprint + drift silo inscribe
    if _is_operator_correction(prompt):
        decisions.append((
            "pattern-observer",
            _invoke_pillar(
                "pattern-observer", excerpt, "fingerprint_event",
                {
                    "event": {
                        "event": "operator_correction",
                        "details": {
                            "subject": prompt[:80],
                            "operator_language": prompt,
                        },
                    },
                },
            ),
        ))
        # W1 KEYSTONE (pillar-wiring 2026-06-05) — recall-silos RETRIEVE.
        # Before inscribing this correction, query the drift silo: was the SAME
        # correction made before? The wrapper ships every silo entry in
        # excerpt.retrievals every turn; until now nothing queried them. An
        # exact fingerprint match here = drift recurrence (the sharpest
        # sensitization signal: "you've corrected this exact thing before"). The
        # query tokens use compute_class_tokens("drift") — the lockstep
        # single-source the inscribe composer mirrors byte-for-byte — so a
        # recurrence fingerprint-matches the inscribed entry. Dispatched BEFORE
        # inscribe_drift so the recall reflects PRIOR occurrences, not the one
        # we're about to write. The recall_match decision carries each match's
        # fingerprint_hash so the wrapper joins wrapper-local canonical text
        # (W5, privacy moat: the server can't read canonical_resolutions.jsonl);
        # its body surfaces the recurrence to the operator via _aggregate.
        _drift_query_tokens = recall_silos.compute_class_tokens(
            "drift", {"claim": prompt, "claimed_in": "response_text"}
        )
        if _drift_query_tokens:
            decisions.append((
                "recall-silos",
                _invoke_pillar(
                    "recall-silos", excerpt, "query_silo",
                    {
                        "class_name": "drift",
                        "key_tokens": _drift_query_tokens,
                        "top_n": 3,
                    },
                ),
            ))

        # PATCH-194 (2026-05-23) — Phase 3.4b drift silo inscribe. Persist
        # the correction as a drift SiloEntry on operator's local
        # `<state_dir>/silos/drift.jsonl` (routed by the wrapper's
        # client_state_writer based on content.silo_class). Token
        # construction mirrors compute_class_tokens("drift") byte-for-byte
        # so future encounters of the same claim fingerprint-match against
        # the inscribed entry. Silent inscribe (no body) — pattern-observer
        # above is the felt-presence anchor for this dispatch site.
        _drift_session_id = (
            event.get("session_id") if isinstance(event, dict) else None
        )
        decisions.append((
            "recall-silos",
            _invoke_pillar(
                "recall-silos", excerpt, "inscribe_drift",
                {
                    "prompt": prompt,
                    "session_id": _drift_session_id,
                },
            ),
        ))

    # 3. Drift_check baseline — always fires when there are observations
    if excerpt.observations:
        decisions.append((
            "stress-response",
            _invoke_pillar("stress-response", excerpt, "drift_check", {}),
        ))
        # v0.2.6 PATCH-128 (advisor Â§1.2 + dev empirical proof): orphan
        # tool_call detector. Claude Code's harness skips PostToolUse for
        # some pre-validation failures (e.g., Read against non-existent
        # file), so the existing detect_repeated_tool_failure path at
        # PostToolUse cannot observe them. UserPromptSubmit fires between
        # operator turns and can scan the full observation window for
        # orphan-call patterns the other detector misses.
        #
        # Item 50 (2026-06-10) — orphan-salience round-trip. The wrapper
        # owns the salience state and ships it each call as
        # event.orphan_salience_state; pass the slice through so the
        # suppress decision is computed from THIS user's state (the old
        # module-global bled across tenants). Missing → empty default.
        _orphan_prior = event.get("orphan_salience_state")
        decisions.append((
            "stress-response",
            _invoke_pillar(
                "stress-response", excerpt, "detect_orphan_tool_failures",
                {
                    "failure_threshold": 3,
                    "orphan_salience_state": (
                        _orphan_prior if isinstance(_orphan_prior, dict) else {}
                    ),
                },
            ),
        ))

        # PATCH-129 (2026-05-19): v2.3-parity cascade for user_prompt_submit.
        # Operator-authorized prod hot-patch per explicit P9 suspension of
        # prod-freeze rule for this work-stream. Restores regulator-presence
        # chrome row operator felt every turn in the plugin.
        # W6 derivation lives in _compose_stress_nudge_decision (item 25b).
        decisions.append(_compose_stress_nudge_decision(excerpt))
        # PATCH-171 (2026-05-22): extract legacy_file_count at this call
        # site too — sibling of the line-688 (PATCH-170) bug. Surfaced by
        # dispatcher-probe-pass1 line-471 probe; same constant-stub-family
        # pattern: dispatcher invoked volume-control but passed empty
        # details, handler's legacy_aging branch never fired despite the
        # session's observation tail carrying legacy_aging events.
        decisions.append((
            "volume-control",
            _invoke_pillar(
                "volume-control", excerpt, "compose_volume_control_nudge",
                _volume_control_details_from_obs(excerpt.observations),
            ),
        ))
        # PATCH-162 (2026-05-22): real state computation. Pre-PATCH-162 the
        # parameters below were hard-coded constants — baseline="normal" /
        # work_mode="normal" for the metabolic status line, and
        # cascade_active=False / events_since_threat=0 / thriving=False for
        # the state marker. Effect: format_metabolic_status_line always
        # returned the same body, and compute_state_marker always returned
        # "stressed" (because events_since_threat=0 falls into the
        # `< 3 → "stressed"` branch of compute_state_marker's logic). Both
        # confirmed by advisor 2026-05-22 direct-probe brief; the constant
        # `stressed` chrome line was injected on every operational user's
        # turn for the lifetime of the v0.2.0 dispatch architecture.
        #
        # Fix: derive each parameter from excerpt.observations using the
        # pillar's existing pure functions (compute_baseline + infer_work_mode
        # + compute_thriving_signal) plus inline counters for the things the
        # pure modules don't compute (events_since_threat + cascade_active
        # read from the preceding drift_check decision in this dispatch).
        cascade_active = False
        for prior_pillar, prior_decision in reversed(decisions):
            if (
                prior_pillar == "stress-response"
                and prior_decision is not None
                and isinstance(prior_decision.details, dict)
                and "fast_pathway_fires" in prior_decision.details
            ):
                cascade_active = bool(
                    prior_decision.details.get("fast_pathway_fires")
                    or prior_decision.details.get(
                        "slow_pathway_active_until_pending"
                    )
                )
                break

        # Walk observations newest-first, counting non-threat events until a
        # threat is encountered. No threat found → use a large sentinel so
        # compute_state_marker's `events_since_threat < 5` branches fall
        # through to the thriving/healthy logic instead of the cortisol-
        # afterglow "stressed" branch. Matches the semantic of check_recovery
        # (stress_response.py line 492-508) which returns True (recovered)
        # on no-threat-found.
        _events_since_threat = 0
        _threat_seen = False
        for _ev in reversed(excerpt.observations):
            _evt = _ev.get("event")
            if isinstance(_evt, str) and _evt in stress_response.THREAT_SEVERITIES:
                _threat_seen = True
                break
            _events_since_threat += 1
        if not _threat_seen:
            _events_since_threat = 999

        # H-34: route these three DIRECT pillar computations through the same
        # try/except isolation _invoke_pillar gives every other pillar call, so
        # a malformed observation degrades to the pillar's own neutral defaults
        # (thriving=False / baseline="normal" / work_mode="normal") instead of
        # raising AttributeError out of the async handler.
        _thriving = _safe_pillar_compute(
            "compute_thriving_signal",
            stress_response.compute_thriving_signal,
            excerpt.observations,
            False,
        )
        _baseline = _safe_pillar_compute(
            "compute_baseline",
            metabolism.compute_baseline,
            excerpt.observations,
            "normal",
        )
        _work_mode = _safe_pillar_compute(
            "infer_work_mode",
            metabolism.infer_work_mode,
            excerpt.observations,
            "normal",
        )

        decisions.append((
            "metabolism",
            _invoke_pillar(
                "metabolism", excerpt, "format_metabolic_status_line",
                {"baseline": _baseline, "work_mode": _work_mode},
            ),
        ))
        decisions.append((
            "stress-response",
            _invoke_pillar(
                "stress-response", excerpt, "compute_state_marker",
                {
                    "cascade_active": cascade_active,
                    "events_since_threat": _events_since_threat,
                    "thriving": _thriving,
                },
            ),
        ))

        # PATCH-180 (2026-05-23): Finding #3 fix — wire detect_proposals
        # into UserPromptSubmit to bypass the dispatch_stop wrapper-debounce
        # bottleneck. Pattern-observer's N=4 cross-session learning loop was
        # previously only reachable via dispatch_stop, which the wrapper
        # short-circuits at hooks/stop.py line 231-232 (`if _is_repeat_stop:
        # return 0`) AFTER the first Stop of each session. detect_proposals
        # therefore ran exactly once per session at the lowest accumulation
        # point and never again. This was confirmed empirically (PATCH-179
        # diagnostic run, since removed): 234 records across 4 sessions, ZERO
        # dispatch_stop entries (wrapper short-circuit is BEFORE MCP call).
        #
        # Fix: invoke detect_proposals on every UserPromptSubmit (already
        # gated above on `excerpt.observations` non-empty). Pillar reads
        # observations server-side and counts operator_correction events;
        # returns no_op until N=4 same-subject cluster detected, then
        # propose_rules with body + pending_proposals.jsonl client_state_write.
        #
        # Idempotency: PATCH-149 dedup (5-min window per session_id+pillar+
        # body_fingerprint) prevents repeat surfaces of the same proposal.
        # If detect_proposals fires every UserPromptSubmit and clustering
        # holds, operator sees one proposal surface per 5-min window
        # regardless of how many invocations happened. detect_in_session_
        # overrides remains dispatch_stop-only for now (separate follow-on;
        # different architectural rationale — N=2 same-session detection
        # naturally aligns with session boundaries).
        decisions.append((
            "pattern-observer",
            _invoke_pillar(
                "pattern-observer", excerpt, "detect_proposals", {},
            ),
        ))

    # PATCH-149 (2026-05-22): cascade dedup. Suppress same-(session, pillar,
    # body) decisions that fired within the 5-min window. Closes the
    # "stress: too_many_edits" continuous-emission failure mode.
    session_id = event.get("session_id") if isinstance(event, dict) else None
    decisions = _filter_decisions_by_dedup(session_id, decisions)
    response = _aggregate(decisions)
    return response


def dispatch_pre_tool_use_logic(
    excerpt: ExcerptPayload, event: dict[str, Any]
) -> DispatchResponse:
    """PreToolUse dispatch — innate guardrail rules.

    event keys:
      tool_name: str (required; empty â†’ no_op)
      command: str | None (for Bash tools)
      file_path: str | None (for Edit/Write tools)
      context_flags: list[str] (canonical-verification rule inputs)
      sibling_list: list[str]
      operation: closed wrapper semantic operation (advisory only)
      operation_risk: closed risk class (never trusted authority)
    """
    tool_name = event.get("tool_name") or ""
    if not tool_name:
        return DispatchResponse()

    details: dict[str, Any] = {"tool_name": tool_name}
    if event.get("command"):
        details["command"] = event["command"]
    if event.get("file_path"):
        details["file_path"] = event["file_path"]
    _operation = event.get("operation")
    if isinstance(_operation, str) and _operation in {
        "write", "edit", "read", "external_private_send",
        "broadcast_message", "smtp_send", "gmail_send", "outlook_send",
        "post_to_social", "post_to_forum", "publish_blog_post", "mass_email",
        "webhook_send_with_payload", "oauth_authorize", "sso_complete",
        "terms_of_service_accept", "privacy_policy_accept", "cookie_consent",
        "sharing_permission_grant", "api_key_grant", "webhook_endpoint_register",
        "stripe_charge", "payment_complete", "subscription_activate",
        "account_setting_change", "password_set", "2fa_setup",
    }:
        details["operation"] = _operation
    _risk = event.get("operation_risk")
    if isinstance(_risk, str) and _risk in {
        "ordinary", "trusted_internal_coordination", "external_private_send",
        "public_or_mass_send", "destructive", "permission_or_financial",
    }:
        # Evidence only. The server has no host-authenticated provider claim
        # and therefore never treats this field as an authorization upgrade.
        details["operation_risk"] = _risk
    # H-32 (input-validation): only coerce when the wire value is actually a
    # collection of the shape the pillar expects (list/tuple/set of flag
    # names). A truthy scalar (e.g. `context_flags: 1`) or a bare string would
    # otherwise reach `list(...)` here and raise TypeError — or, for a string,
    # be silently exploded into single-character "flags" — BEFORE
    # innate_enforcer.check_pre_tool_call gets to run its own defensive
    # type-check (pillars/innate_enforcer.py:620-624 / 652-658). Anything else
    # is dropped so the pillar sees a missing key and defaults to empty.
    _ctx_flags = event.get("context_flags")
    if isinstance(_ctx_flags, (list, tuple, set)):
        details["context_flags"] = list(_ctx_flags)
    # sibling_list: the wrapper may send either a list OR a pre-formatted string
    # (innate_enforcer renders a str as-is — pillars/innate_enforcer.py str
    # branch). Pass a str THROUGH (review fix: a list-only guard dropped the
    # string form, so rule-04's message lost its folder names); only genuinely
    # unusable types (scalar int, dict) are dropped for the pillar to default.
    _sibling_list = event.get("sibling_list")
    if isinstance(_sibling_list, (list, tuple, set)):
        details["sibling_list"] = list(_sibling_list)
    elif isinstance(_sibling_list, str):
        details["sibling_list"] = _sibling_list

    decisions: list[tuple[str, DecisionPayload | None]] = [(
        "innate-enforcer",
        _invoke_pillar(
            "innate-enforcer", excerpt, "check_pre_tool_call", details,
        ),
    )]
    response = _aggregate(decisions)
    return response


def dispatch_post_tool_use_logic(
    excerpt: ExcerptPayload, event: dict[str, Any]
) -> DispatchResponse:
    """PostToolUse dispatch — drift baseline on every call + cascade on
    error. Replaces the 1:120 dispatch gate at the wrapper.

    event keys:
      tool_name: str
      is_error: bool
      exit_code: int | None
      file_path: str | None
      command: str | None
    """
    decisions: list[tuple[str, DecisionPayload | None]] = []
    tool_name = event.get("tool_name") or ""
    is_error = bool(event.get("is_error", False))

    if not tool_name:
        return DispatchResponse()

    # Lightweight drift baseline on every tool call.
    if excerpt.observations:
        decisions.append((
            "stress-response",
            _invoke_pillar("stress-response", excerpt, "drift_check", {}),
        ))

    # Repeated-failure cascade on error.
    if is_error:
        decisions.append((
            "stress-response",
            _invoke_pillar(
                "stress-response", excerpt, "detect_repeated_tool_failure",
                {"failure_threshold": 3},
            ),
        ))

    # PATCH-129 (2026-05-19): invoke compose_stress_nudge so KISS detector
    # can surface overcomplication nudges (too_many_edits, plan_too_granular,
    # doc_for_tactical_work) on every post-tool-use.
    # Counts derived from excerpt.observations server-side — wrapper's PATCH-129
    # hot-patch now emits file_edited / file_created / file_written classified
    # events on Edit/Write tool calls (was previously emitting only generic
    # tool_call_succeeded which KISS counter never saw).
    if excerpt.observations:
        # W6 derivation lives in _compose_stress_nudge_decision (item 25b).
        decisions.append(_compose_stress_nudge_decision(excerpt))

    # PATCH-149 (2026-05-22): cascade dedup applied here too — post_tool_use
    # also fires compose_stress_nudge per PATCH-129 + can produce repeated
    # KISS / drift bodies. Same dedup logic as dispatch_user_prompt_submit.
    session_id = event.get("session_id") if isinstance(event, dict) else None
    decisions = _filter_decisions_by_dedup(session_id, decisions)
    response = _aggregate(decisions)
    return response


def dispatch_stop_logic(
    excerpt: ExcerptPayload, event: dict[str, Any]
) -> DispatchResponse:
    """Stop dispatch — dusk-phase sweep.

    event keys:
      session_id: str (wrapper handles debounce; server treats as pass-through)

    v0.2.6 PATCH-128 A4: Claude Code's Stop event silently drops
    additionalContext per the hook docs. So this dispatch's body content
    cannot reach the agent's context via the standard surface. Instead,
    we emit a `pending_dusk_surface.jsonl` client_state_write capturing
    the body for next-session dawn rendering. The agent at this Stop
    won't see the content (the harness drops it), but the operator's
    next session-start composer reads pending_dusk_surface.jsonl back
    and surfaces it as dawn-phase carryover.
    """
    # PATCH-166 (2026-05-22): pass current_session_id to detect_in_session_overrides.
    # Pre-PATCH-166 this dispatcher passed empty {} to the question; the handler
    # fell back to inferring session_id from observation details.sessionId. When
    # observations don't carry sessionId (which is the common case for
    # wrapper-appended events that don't include session_id in details), the
    # function returns []. PATCH-166 passes event.session_id directly so the
    # N=2 same-session override loop has a concrete session boundary to filter
    # on. Found during advisor 2026-05-22 §3.3 audit of _invoke_pillar call
    # sites for the constant-stub family pattern.
    _stop_session_id = event.get("session_id") if isinstance(event, dict) else None

    # PATCH-170 (2026-05-22): extract legacy_file_count via shared helper.
    # Pre-PATCH-170 every dispatcher call site passed empty {} to
    # compose_volume_control_nudge — handler's legacy_aging branch never
    # ran. Centralizing extraction in _volume_control_details_from_obs
    # ensures consistency across Stop/UserPromptSubmit/etc.
    decisions: list[tuple[str, DecisionPayload | None]] = [
        (
            "volume-control",
            _invoke_pillar(
                "volume-control", excerpt, "compose_volume_control_nudge",
                _volume_control_details_from_obs(excerpt.observations),
            ),
        ),
        (
            "pattern-observer",
            _invoke_pillar(
                "pattern-observer", excerpt, "detect_proposals", {},
            ),
        ),
        (
            "pattern-observer",
            _invoke_pillar(
                "pattern-observer", excerpt, "detect_in_session_overrides",
                {"current_session_id": _stop_session_id} if _stop_session_id else {},
            ),
        ),
    ]
    response = _aggregate(decisions)

    # A4: Stop event drops additionalContext. Convert body content to a
    # pending_dusk_surface.jsonl persistence write so next dawn surfaces it.
    if response.additional_context:
        for pillar, decision in decisions:
            if decision is None or not decision.body:
                continue
            if decision.action in ("no_op", "no_match"):
                continue
            response.client_state_writes.append(ClientStateWrite(
                target="pending_dusk_surface.jsonl",
                mode="append",
                content={
                    "timestamp": _utc_now_iso(),
                    "pillar": pillar,
                    "body": decision.body,
                    "session_id": event.get("session_id"),
                },
                pillar_id=pillar,
            ))
        # Clear additional_context — the harness drops it at Stop anyway,
        # and the persistence path above carries the content forward.
        response.additional_context = ""

    return response


# ============================================================================
# MCP tool registration
# ============================================================================


def register_dispatch_tools(server: FastMCP) -> None:
    """Register the v0.2.0 dispatch_* MCP tools.

    Called from mcp_handlers.build_mcp_server after pillar registration.
    """

    @server.tool(
        name="dispatch_session_start",
        description=(
            "v0.2.0 dispatch tool for the wrapper's SessionStart hook. "
            "Returns the state decision (state 1/2/3/4) + any dawn-phase "
            "nudges + client_state_writes for cross-session persistence."
        ),
    )
    async def dispatch_session_start(
        excerpt: ExcerptPayload, event: dict[str, Any]
    ) -> DispatchResponse:
        # async wrapper so FastMCP runs us in the request's event loop
        # (sync tools land in a thread pool, which breaks ContextVar
        # propagation for pillar_ids_invoked).
        return dispatch_session_start_logic(excerpt, event)

    @server.tool(
        name="dispatch_user_prompt_submit",
        description=(
            "v0.2.0 dispatch tool for the wrapper's UserPromptSubmit hook. "
            "Token-threshold crossing + operator-correction detection + drift "
            "baseline. Operator-consented 500-char prompt capture."
        ),
    )
    async def dispatch_user_prompt_submit(
        excerpt: ExcerptPayload, event: dict[str, Any]
    ) -> DispatchResponse:
        return dispatch_user_prompt_submit_logic(excerpt, event)

    @server.tool(
        name="dispatch_pre_tool_use",
        description=(
            "v0.2.0 dispatch tool for the wrapper's PreToolUse hook. "
            "Innate guardrail rule matching (secrets, destructive, canonical)."
        ),
    )
    async def dispatch_pre_tool_use(
        excerpt: ExcerptPayload, event: dict[str, Any]
    ) -> DispatchResponse:
        return dispatch_pre_tool_use_logic(excerpt, event)

    @server.tool(
        name="dispatch_post_tool_use",
        description=(
            "v0.2.0 dispatch tool for the wrapper's PostToolUse hook. "
            "Drift baseline on every call + repeated-failure cascade on "
            "error. Replaces the 1:120 dispatch gate."
        ),
    )
    async def dispatch_post_tool_use(
        excerpt: ExcerptPayload, event: dict[str, Any]
    ) -> DispatchResponse:
        return dispatch_post_tool_use_logic(excerpt, event)

    @server.tool(
        name="dispatch_stop",
        description=(
            "v0.2.0 dispatch tool for the wrapper's Stop hook (dusk sweep). "
            "Volume control + pattern-observer proposals + in-session "
            "overrides. Wrapper debounces 30:1 misfires via session_id."
        ),
    )
    async def dispatch_stop(
        excerpt: ExcerptPayload, event: dict[str, Any]
    ) -> DispatchResponse:
        return dispatch_stop_logic(excerpt, event)
