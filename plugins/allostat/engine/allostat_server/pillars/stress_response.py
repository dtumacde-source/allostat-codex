"""Stress-response pillar — drift detection + cascade + sensitization + KISS family.

Server-side decision logic for every stress-response decision the v2.3 plugin
exposes in `plugin/lib/stress_response_runtime.py` (1289 lines). The wrapper
plugin composes a context excerpt + question on each call; the server
returns a decision + stats. No filesystem reads, no per-request persistence
— stateless pillar.

Tool exposed:

    stress_response_evaluate(excerpt, question) → PillarToolResponse

question.question_type ∈ {
    # Core threat detection (γ-landed)
    "drift_check",                       # classify observations into threats
    "should_fire_cascade",               # severity → (fast, slow) pathway decision
    "detect_repeated_tool_failure",      # ≥N same-operation failures → synthesize threat

    # Allostatic state (parity-fix)
    "compute_thriving_signal",           # eustress detection over rolling window
    "format_thriving_surface",           # operator-facing surface line when thriving
    "check_recovery",                    # N events since last threat → recovered
    "detect_chronic_stress",             # threats > N in 30 days → chronic
    "compute_state_marker",              # avatar state marker from stress_state

    # Sensitization layer (parity-fix)
    "detect_drift_recurrence",           # previously-corrected fingerprint recurring
    "compute_salience",                  # exponential-decay salience for a fingerprint
    "should_suppress_by_salience",       # suppress re-fire when salience above threshold

    # KISS / overcomplication family (parity-fix)
    "detect_idle_resumption",            # long gap since last event → fire idle nudge
    "detect_kiss_drift",                 # overcomplication detector (5 sub-classes)
    "detect_completion_unverified",      # completion claim without verification

    # Integrator (parity-fix)
    "compose_stress_nudge",              # priority-ordered detector cascade → nudge line
}

Each decision is a faithful port of its v2.3 plugin counterpart. Filesystem
reads in the plugin (observations.jsonl, drift_seen.json) become wrapper-
supplied parameters: the wrapper reads its local state and sends the relevant
slice via the excerpt or question.details. Server is stateless per request.

Drift between v2.3 plugin's `stress_response_runtime.py` and this module is
PATENT-AFFECTING — the v2.4 hosted-MCP embodiment must reproduce v2.3
decisions verbatim. THREAT_SEVERITIES + STRESS_SKILL_MAP + KISS_THRESHOLDS
+ THRIVING_THRESHOLDS + IDLE_RESUMPTION_THRESHOLD_MINUTES + half-life
constants are all locked in lockstep with the plugin until θ moves shared
data to MCP resources.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .._time import parse_iso as _parse_iso
from ..pillar_protocol import (
    DecisionPayload,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    coerce_int,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "stress-response"


# ============================================================================
# Constants — locked to plugin/lib/stress_response_runtime.py (v2.3 reference
# state 2026-05-13). Drift between these and the plugin's copies must stay
# zero through v2.4-v2.5 until θ moves shared data to MCP resources.
# ============================================================================

# Severity → rank for highest-severity selection (pre-release item 25d —
# was duplicated inline in _drift_check + _summarize_threats).
SEVERITY_RANK: dict[str, int] = {"lethal": 3, "high": 2, "medium": 1}

# Threat class → severity. Mirrors plugin's THREAT_CLASSES verbatim.
THREAT_SEVERITIES: dict[str, str] = {
    "innate_violation_attempt": "lethal",
    "destructive_command_attempt": "lethal",
    "tool_failure_repeated": "high",
    "contradiction_surge": "high",
    "scraper_bot_block": "high",
    "multi_pillar_disagreement": "medium",
    "allostatic_load_threshold": "medium",
    "operator_distress_signal": "medium",
    "fork_rolled_back": "medium",
}

# Drift class → corrective-skill hint string. Mirrors plugin's STRESS_SKILL_MAP.
STRESS_SKILL_MAP: dict[str, str] = {
    "overcomplication_kiss": "→ keep it simple",
    "too_many_edits": "→ simplest viable change first",
    "plan_too_granular": "→ break into smaller plans OR cut tasks",
    "doc_for_tactical_work": "→ ship the work, not the doc",
    "wall_of_text": "→ match brevity to question size",
    "scope_expansion_mid_task": "→ confirm scope with operator",
    "tool_failure_cluster": "→ superpowers:systematic-debugging",
    "completion_unverified": "→ superpowers:verification-before-completion",
    "no_plan_before_action": "→ superpowers:writing-plans",
    "step_boundary_no_review": "→ superpowers:requesting-code-review",
    "drift_recurrence": "→ this drift was corrected before, apply prior correction",
    "idle_resumption": "→ re-read recent context before continuing",
}

# Eustress thresholds. Mirrors plugin's THRIVING_THRESHOLDS.
THRIVING_THRESHOLDS = {
    "max_corrections_per_session": 2.0,
    "min_sessions_observed": 3,
    "max_drift_events": 0,
    "max_fork_rollbacks": 0,
    "max_contradiction_events": 0,
    "window_days": 7,
}

# KISS thresholds. Mirrors plugin's KISS_THRESHOLDS.
KISS_THRESHOLDS = {
    "max_edit_calls_per_session": 15,
    # max_new_files_per_session RETIRED 2026-07-04 (see detect_kiss_drift).
    "max_plan_tasks": 10,
    "max_response_to_prompt_ratio": 5.0,
    "min_prompt_chars_for_ratio_check": 20,
}

# Idle-gap threshold. Mirrors plugin's IDLE_RESUMPTION_THRESHOLD_MINUTES.
IDLE_RESUMPTION_THRESHOLD_MINUTES = 30

# Salience decay defaults. Mirror plugin's _DEFAULT_HALF_LIFE_MINUTES /
# _DEFAULT_SUPPRESSION_THRESHOLD. The plugin allows env-var override via
# ALLOSTAT_STRESS_HALF_LIFE_MINUTES; the server honors the same env var.
_DEFAULT_HALF_LIFE_MINUTES = 30
_DEFAULT_SUPPRESSION_THRESHOLD = 0.4


# PATCH-129 (2026-05-19) → pre-release item 50 (2026-06-10): salience
# suppression for the orphan-tool-call detector (Path-A additive code).
# PATCH-129 kept this state in a module-level dict keyed by tool_name ONLY —
# a multi-tenant bleed (one user's orphan fire suppressed every other user's
# same-tool threat) that also contradicted the module docstring's
# stateless-per-request contract. Item 50 de-statefuls it via the watchdog
# round-trip pattern: the wrapper owns the state (persisted at
# `<state_dir>/state/stress_orphan_salience.json`) and ships it each call
# inside event.details as
#   orphan_salience_state = {"entries": [
#       {"tool_name": <str>, "last_orphan_fire_timestamp": <ISO8601 str>}, ...]}
# The server computes the suppress decision from the passed slice, returns
# the updated list inside decision.details.orphan_state, and holds NOTHING
# between requests. Thresholds (half-life 30 min, suppression 0.4) stay
# server-side. Missing field either direction degrades to an empty default
# so both sides roll independently.
#
# Without suppression, orphan tool_calls accumulated in the observation
# window trigger re-emission of the same `tool_failure_repeated` threat on
# every dispatch_user_prompt_submit fire — operator P9 surfaced this as 24x
# same-threat re-emit in a single session.


def _orphan_salience_suppress(
    tool_name: str,
    prior_entries: list[dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    """Return True iff the orphan-detector should suppress re-fire for this
    tool_name because it was fired within the salience half-life.

    `prior_entries` is the wrapper-supplied per-request state slice (item 50
    round-trip contract). Caller MUST call `_orphan_salience_record` after a
    non-suppressed fire to build the updated list. Suppress-check +
    record-fire are two separate ops to keep the suppression decision pure
    and side-effect-free."""
    if now is None:
        now = datetime.now(timezone.utc)
    last_fired: datetime | None = None
    for entry in prior_entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("tool_name", "")) != tool_name:
            continue
        last_fired = _parse_iso(str(entry.get("last_orphan_fire_timestamp", "")))
        break
    if last_fired is None:
        return False
    half_life_seconds = _half_life_minutes() * 60.0
    elapsed_seconds = (now - last_fired).total_seconds()
    if elapsed_seconds < 0:
        # Clock skew — treat as just-fired (suppress).
        return True
    if half_life_seconds <= 0:
        return False
    # Decay: salience = exp(-ln(2) * elapsed / half_life).
    # Suppress if salience exceeds threshold (matches should_suppress_by_salience).
    decay_lambda = math.log(2) / half_life_seconds
    salience = math.exp(-decay_lambda * elapsed_seconds)
    return salience > _DEFAULT_SUPPRESSION_THRESHOLD


def _orphan_salience_record(
    tool_name: str,
    prior_entries: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return a NEW entries list with this tool_name's fire timestamp set to
    `now`. No global mutation (item 50) — the caller round-trips the result
    back to the wrapper inside decision.details.orphan_state for
    persistence."""
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    updated: list[dict[str, Any]] = []
    recorded = False
    for entry in prior_entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("tool_name", "")) == tool_name:
            updated.append(
                {"tool_name": tool_name, "last_orphan_fire_timestamp": ts}
            )
            recorded = True
        else:
            updated.append(dict(entry))
    if not recorded:
        updated.append(
            {"tool_name": tool_name, "last_orphan_fire_timestamp": ts}
        )
    return updated


def _half_life_minutes() -> float:
    """Half-life from env var or default. Mirrors plugin's _half_life_minutes."""
    try:
        return float(
            os.environ.get(
                "ALLOSTAT_STRESS_HALF_LIFE_MINUTES",
                str(_DEFAULT_HALF_LIFE_MINUTES),
            )
        )
    except (TypeError, ValueError):
        return float(_DEFAULT_HALF_LIFE_MINUTES)


def _hash_drift_payload(text: str) -> str:
    """First 16 chars of sha256. Mirrors plugin's _hash_drift_payload."""
    return hashlib.sha256(
        (text or "").encode("utf-8", errors="ignore")
    ).hexdigest()[:16]


# ============================================================================
# Section 1 — Threat detection (γ-landed; preserved for parity)
# ============================================================================


def _classify_threats(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk the excerpt observations + label any that match a known threat.

    Mirrors plugin's `detect_threats` (operates on the wrapper-supplied list
    instead of reading observations.jsonl).
    """
    out: list[dict[str, Any]] = []
    for event in observations:
        event_type = event.get("event")
        if not isinstance(event_type, str):
            continue
        severity = THREAT_SEVERITIES.get(event_type)
        if severity is None:
            continue
        out.append(
            {
                "timestamp": event.get("timestamp", ""),
                "threat_class": event_type,
                "severity": severity,
                "details": event.get("details", {}) or {},
            }
        )
    return out


def _detect_repeated_tool_failures(
    observations: list[dict[str, Any]],
    failure_threshold: int = 3,
) -> list[dict[str, Any]]:
    """Mirror plugin's `detect_repeated_tool_failure` — same operation
    failing ≥failure_threshold times synthesizes a `tool_failure_repeated`
    high-severity threat."""
    failure_counter: dict[str, list[dict[str, Any]]] = {}
    for event in observations:
        if event.get("event") != "tool_failure":
            continue
        op = (event.get("details") or {}).get("operation", "unknown")
        failure_counter.setdefault(op, []).append(event)

    synthetic: list[dict[str, Any]] = []
    for op, events in failure_counter.items():
        if len(events) >= failure_threshold:
            synthetic.append(
                {
                    "timestamp": events[-1].get("timestamp", ""),
                    "threat_class": "tool_failure_repeated",
                    "severity": "high",
                    "details": {"operation": op, "failure_count": len(events)},
                }
            )
    return synthetic


def _should_fire_cascade(severity: str) -> tuple[bool, bool]:
    """Mirror plugin's `should_fire_cascade(threat)` — (fast, slow) per severity."""
    if severity in ("lethal", "high"):
        return (True, True)
    if severity == "medium":
        return (True, False)
    return (False, False)


# v0.2.6 PATCH-128 Path A additive — orphan tool_call detector.
#
# Background: `_detect_repeated_tool_failures` above counts `event="tool_failure"`
# observations. Those are produced by the wrapper's PostToolUse hook when
# `payload.isError=True`. But Claude Code's harness does NOT fire PostToolUse
# for certain pre-validation failures (e.g., Read against a non-existent file
# returns an error to the agent but never invokes PostToolUse). Result:
# repeated tool-call failures from this class are invisible to the existing
# detector.
#
# This new detector scans for the orthogonal signature: `event="tool_call"`
# entries (PreToolUse always fires) WITHOUT matching `event="tool_call_succeeded"`
# follow-ups within the observation window. ≥failure_threshold orphans of the
# same tool_name synthesize a `tool_failure_repeated` threat.
#
# Strictly additive — does not modify the v2.3-parity-locked
# `_detect_repeated_tool_failures` above; v2.4 + later plugin sync can adopt
# parallel.
def _detect_orphan_tool_call_failures(
    observations: list[dict[str, Any]],
    failure_threshold: int = 3,
    *,
    time_window_minutes: int = 5,
    prior_orphan_state: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect repeated tool-call failures that bypass PostToolUse (e.g.,
    file-not-found Reads). Counts `tool_call` events with no matching
    `tool_call_succeeded` follow-up; if ≥failure_threshold per tool_name,
    synthesize a high-severity threat.

    Returns `(threats, updated_state)` — item 50 round-trip: `prior_orphan_state`
    is the wrapper-supplied salience entries list; `updated_state` is the
    same list with each non-suppressed fire recorded, for the caller to
    hand back to the wrapper (which persists it). Missing/None prior state
    degrades to an empty list (backward-compat with older wrappers).

    Matching pairs by (tool_name, file_path or command). PostToolUse logs
    the same tool_name + file_path in the success event, so missing
    success = orphan call = de-facto failure.

    PATCH-134 (2026-05-19): time-window filter at observation ingestion.
    Pre-PATCH-134 the detector iterated the full `observations` list and
    synthesized threats for tool_calls of any age. Empirical pattern
    (advisor brief 2026-05-19 §2): week-old observations re-fired
    identical threats on every dispatch despite PATCH-129 salience
    suppression. Salience releases after ~30 min (half-life 30 min,
    threshold 0.4) and the stale observation re-synthesizes the same
    threat, creating an effectively-permanent re-fire loop. Hard time-
    window filter on observation ingestion (default 5 min, configurable
    via kwarg) prevents that loop — stale observations are dropped
    BEFORE pair-matching + orphan counting (i.e., BEFORE the salience
    suppress below), so they cannot synthesize threats no matter what
    salience says.
    """
    # PATCH-134: filter observations by timestamp BEFORE pair-matching.
    # Anything outside the window is dropped — neither the success-set
    # builder nor the orphan counter sees stale events.
    now_dt = datetime.now(timezone.utc)
    window_threshold = now_dt - timedelta(minutes=time_window_minutes)

    def _within_window(ev: dict[str, Any]) -> bool:
        ts = _parse_iso(ev.get("timestamp", ""))
        return ts is not None and ts >= window_threshold

    observations = [ev for ev in observations if _within_window(ev)]

    # Build matched-pair set: tool_call_succeeded events are the "saw success"
    # set. Tool_call events whose (tool_name, file_path) is in that set are
    # matched; others are orphans.
    succeeded: set[tuple[str, str]] = set()
    for ev in observations:
        if ev.get("event") != "tool_call_succeeded":
            continue
        details = ev.get("details") or {}
        tool_name = str(details.get("tool_name", ""))
        file_path = str(details.get("file_path", "") or details.get("command", ""))
        succeeded.add((tool_name, file_path))

    orphan_counter: dict[str, list[dict[str, Any]]] = {}
    for ev in observations:
        if ev.get("event") != "tool_call":
            continue
        details = ev.get("details") or {}
        tool_name = str(details.get("tool_name", ""))
        file_path = str(details.get("file_path", "") or details.get("command", ""))
        if (tool_name, file_path) in succeeded:
            continue
        # Orphan call — no matching success.
        if not tool_name:
            continue
        orphan_counter.setdefault(tool_name, []).append(ev)

    synthetic: list[dict[str, Any]] = []
    updated_state: list[dict[str, Any]] = [
        dict(entry)
        for entry in (prior_orphan_state or [])
        if isinstance(entry, dict)
    ]
    now = datetime.now(timezone.utc)
    for tool_name, evs in orphan_counter.items():
        if len(evs) >= failure_threshold:
            # PATCH-129 (2026-05-19): salience suppression. If the same
            # tool_name's orphan-detector threat fired within the half-life
            # window, the operator has already seen this threat — suppress
            # re-emission. Item 50 (2026-06-10): the suppress decision now
            # reads the wrapper-supplied per-request slice instead of a
            # module global, so one user's fire can't suppress another's.
            if _orphan_salience_suppress(tool_name, updated_state, now=now):
                continue
            updated_state = _orphan_salience_record(
                tool_name, updated_state, now=now
            )
            synthetic.append(
                {
                    "timestamp": evs[-1].get("timestamp", ""),
                    "threat_class": "tool_failure_repeated",
                    "severity": "high",
                    "details": {
                        "operation": tool_name,
                        "failure_count": len(evs),
                        "failure_source": "orphan_tool_call",
                    },
                }
            )
    return synthetic, updated_state


# ============================================================================
# Section 2 — Allostatic state (parity-fix)
# ============================================================================


def compute_thriving_signal(
    observations: list[dict[str, Any]],
    *,
    window_days: int = THRIVING_THRESHOLDS["window_days"],
    now: datetime | None = None,
) -> bool:
    """Return True iff ALL thriving conditions hold over the rolling window.

    Mirrors plugin's `compute_thriving_signal` — operates on wrapper-supplied
    observations instead of reading observations.jsonl. The plugin reads
    `details.sessionId` to count distinct sessions; we honor the same field.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    threshold_dt = now - timedelta(days=window_days)

    sessions: set[str] = set()
    correction_count = 0
    drift_count = 0
    rollback_count = 0
    contradiction_count = 0

    for event in observations:
        ts = _parse_iso(event.get("timestamp", ""))
        if ts is None or ts < threshold_dt:
            continue

        event_type = event.get("event", "")
        session_id = (event.get("details") or {}).get("sessionId")
        if session_id:
            sessions.add(session_id)

        if event_type in ("operator_correction", "rule_violation_caught"):
            correction_count += 1
        if event_type == "drift_detected":
            drift_count += 1
        if event_type == "fork_rolled_back":
            rollback_count += 1
        if event_type == "contradiction_detected":
            contradiction_count += 1

    sessions_observed = len(sessions)
    if sessions_observed < THRIVING_THRESHOLDS["min_sessions_observed"]:
        return False

    avg_corrections = correction_count / sessions_observed if sessions_observed else 999
    if avg_corrections > THRIVING_THRESHOLDS["max_corrections_per_session"]:
        return False
    if drift_count > THRIVING_THRESHOLDS["max_drift_events"]:
        return False
    if rollback_count > THRIVING_THRESHOLDS["max_fork_rollbacks"]:
        return False
    if contradiction_count > THRIVING_THRESHOLDS["max_contradiction_events"]:
        return False

    return True


def format_thriving_surface(thriving: bool) -> str:
    """If thriving, return the operator-facing surface line; else empty.

    Mirrors plugin's `format_thriving_surface` — pure formatting.
    """
    if not thriving:
        return ""
    return (
        "  🌟  Thriving — sustained low-correction, no-drift state across "
        "recent sessions. The regulator detects nothing wrong AND positive "
        "convergence signal."
    )


def check_recovery(
    observations: list[dict[str, Any]],
    *,
    recovery_event_threshold: int = 10,
) -> bool:
    """Return True iff N events have elapsed since the most recent threat.

    Mirrors plugin's `check_recovery` — walks the observations list in
    reverse, counting non-threat events since the last threat. Empty list
    or no-threat-found means recovered.
    """
    events_since = 0
    for event in reversed(observations):
        if event.get("event") in THREAT_SEVERITIES:
            return events_since >= recovery_event_threshold
        events_since += 1
    return True


def detect_chronic_stress(
    observations: list[dict[str, Any]],
    *,
    window_days: int = 30,
    chronic_threshold: int = 10,
    now: datetime | None = None,
) -> bool:
    """Return True iff threats in window > chronic_threshold.

    Mirrors plugin's `detect_chronic_stress`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    count = 0
    for event in observations:
        if event.get("event") not in THREAT_SEVERITIES:
            continue
        ts = _parse_iso(event.get("timestamp", ""))
        if ts is None or ts < cutoff:
            continue
        count += 1
    return count > chronic_threshold


def compute_state_marker(
    *,
    cascade_active: bool,
    events_since_threat: int,
    thriving: bool = False,
) -> str:
    """Compute the avatar state marker.

    Mirrors plugin's `compute_state_marker(stress_state, project_root)`:
      1. cascade_active → "stressed"
      2. events_since_threat < 3 → "stressed" (cortisol afterglow)
      3. events_since_threat 3-4 → "healthy" (recovered, slightly elevated)
      4. No threat AND thriving=True → "thriving"
      5. Default → "healthy"

    Server takes thriving as a precomputed bool (the wrapper calls
    compute_thriving_signal first, then passes the result). Splitting the
    two evaluations means each can be cached client-side independently.
    """
    if cascade_active:
        return "stressed"
    if events_since_threat < 5:
        return "stressed" if events_since_threat < 3 else "healthy"
    if thriving:
        return "thriving"
    return "healthy"


# ============================================================================
# Section 3 — Sensitization layer (parity-fix)
# ============================================================================


def compute_salience(
    *,
    last_seen: str | None,
    now: datetime | None = None,
    half_life_minutes: float | None = None,
) -> float:
    """Exponential-decay salience [0..1] for a previously-fired fingerprint.

    Mirrors plugin's `compute_salience` — but takes last_seen ISO directly
    rather than reading the drift_seen state. The wrapper passes the
    last_seen value from its local .allostat/state/stress_drift_seen.json.

    No prior fire (last_seen None or unparseable) → 0.0.
    Clock skew (now before last_seen) → 1.0 (treat as just-fired).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    hl_minutes = half_life_minutes if half_life_minutes is not None else _half_life_minutes()
    last = _parse_iso(last_seen) if last_seen else None
    if last is None:
        return 0.0
    elapsed_seconds = (now - last).total_seconds()
    if elapsed_seconds < 0:
        return 1.0
    half_life_seconds = hl_minutes * 60.0
    if half_life_seconds <= 0:
        return 0.0
    decay_lambda = math.log(2) / half_life_seconds
    return math.exp(-decay_lambda * elapsed_seconds)


def should_suppress_by_salience(
    *,
    salience: float | None = None,
    last_seen: str | None = None,
    now: datetime | None = None,
    half_life_minutes: float | None = None,
    threshold: float = _DEFAULT_SUPPRESSION_THRESHOLD,
) -> bool:
    """Return True iff salience exceeds threshold (re-fire suppressed).

    Mirrors plugin's `should_suppress_by_salience`. Caller can either pass
    a precomputed `salience` or pass `last_seen` and let this function
    compute salience inline.
    """
    if salience is None:
        salience = compute_salience(
            last_seen=last_seen, now=now, half_life_minutes=half_life_minutes
        )
    return salience > threshold


def detect_drift_recurrence(
    *,
    corrections: list[dict[str, Any]],
    current_session_activity: list[dict[str, Any]],
    drift_seen_entries: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    half_life_minutes: float | None = None,
    suppression_threshold: float = _DEFAULT_SUPPRESSION_THRESHOLD,
) -> dict[str, Any] | None:
    """Detect a previously-corrected drift fingerprint recurring NOW.

    Mirrors plugin's `_detect_drift_recurrence` — the sensitization detector
    with PATCH-087 (newer-correction supersession + stricter overlap +
    exact-phrase fallback) and PATCH-095 (word-boundary regex match).

    Inputs:
      - corrections: list of operator_correction events the wrapper has
        accumulated across sessions. Each has {timestamp, details.subject,
        details.operator_language}.
      - current_session_activity: events emitted in the CURRENT session
        (per PATCH-050e — corrections persist cross-session but matches
        require current-session activity).
      - drift_seen_entries: the wrapper's local drift_seen.json["entries"]
        slice for salience suppression. None disables suppression.

    Returns dict {prior_correction_text, match_count, subject} when
    recurrence detected; None otherwise.
    """
    if not corrections or not current_session_activity:
        return None

    # Normalize the correction list to plugin's expected shape (lowercased
    # subjects, kept original language for return value).
    normalized_corrections: list[dict[str, Any]] = []
    for c in corrections:
        details = c.get("details") or {}
        normalized_corrections.append(
            {
                "subject": (details.get("subject") or "").lower(),
                "language": details.get("operator_language") or "",
                "timestamp": c.get("timestamp", ""),
            }
        )

    # Cap activity to 30 most recent.
    recent_activity = current_session_activity[-30:]

    # PATCH-087: newer-correction supersession. If a more recent correction
    # shares ≥2 distinctive subject words with an older one, treat newer as
    # authoritative and skip older from the candidate pool.
    superseded: set[int] = set()
    for j, newer in enumerate(normalized_corrections):
        newer_words = {
            w for w in (newer["subject"] or "").split() if len(w) > 3
        }
        if len(newer_words) < 2:
            continue
        for k, older in enumerate(normalized_corrections[:j]):
            older_words = {
                w for w in (older["subject"] or "").split() if len(w) > 3
            }
            if len(older_words) < 2:
                continue
            if len(newer_words & older_words) >= 2:
                superseded.add(k)

    # Build drift_seen lookup if provided (for salience suppression).
    drift_seen_by_hash: dict[str, dict[str, Any]] = {}
    if drift_seen_entries:
        for entry in drift_seen_entries:
            h = entry.get("hash")
            if h:
                drift_seen_by_hash[h] = entry

    # Walk corrections newest-first; for each non-superseded one, try to
    # match against recent activity newest-first.
    for idx in range(len(normalized_corrections) - 1, -1, -1):
        if idx in superseded:
            continue
        correction = normalized_corrections[idx]
        subj = correction["subject"].strip()
        if not subj or len(subj) < 8:
            continue
        subj_words = {w for w in subj.split() if len(w) > 3}
        if len(subj_words) < 2:
            continue

        # PATCH-087: stricter overlap. Threshold scales with subject length.
        required_overlap = 3 if len(subj_words) <= 6 else max(3, len(subj_words) // 2)

        # PATCH-095: word-boundary regex match (was substring match).
        subj_word_patterns = {
            w: re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE)
            for w in subj_words
        }
        for activity in reversed(recent_activity):
            blob = json.dumps(activity.get("details", {}))
            matched_words = sum(
                1 for pat in subj_word_patterns.values() if pat.search(blob)
            )
            if matched_words >= required_overlap:
                result = {
                    "prior_correction_text": correction["language"][:120],
                    "match_count": matched_words,
                    "subject": subj,
                }

                # PATCH-048 Phase 2: salience suppression.
                if drift_seen_entries is not None:
                    payload_hash = _hash_drift_payload(result["prior_correction_text"])
                    entry = drift_seen_by_hash.get(payload_hash)
                    if entry:
                        if should_suppress_by_salience(
                            last_seen=entry.get("last_seen") or entry.get("first_seen"),
                            now=now,
                            half_life_minutes=half_life_minutes,
                            threshold=suppression_threshold,
                        ):
                            return None
                return result
    return None


# ============================================================================
# Section 4 — KISS / overcomplication family (parity-fix)
# ============================================================================


def detect_idle_resumption(
    *,
    last_event_iso: str | None,
    now: datetime | None = None,
    threshold_minutes: int = IDLE_RESUMPTION_THRESHOLD_MINUTES,
) -> bool:
    """Return True iff the gap from last_event_iso to now exceeds threshold.

    Mirrors plugin's `_detect_idle_resumption` — but takes last_event_iso
    directly rather than reading observations.jsonl. The wrapper passes the
    timestamp of its most-recent observation.
    """
    last_ts = _parse_iso(last_event_iso) if last_event_iso else None
    if last_ts is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    gap = now - last_ts
    return gap > timedelta(minutes=threshold_minutes)


def detect_kiss_drift(
    *,
    event_counts: dict[str, int],
    recent_plan_task_count: int | None = None,
    thresholds: dict[str, Any] | None = None,
) -> str | None:
    """Heuristic detector for overcomplication patterns.

    Mirrors plugin's `_detect_kiss_drift`. Wrapper pre-aggregates event
    counts client-side (session-scoped per PATCH-050) and passes them in.

    Priority order — first match wins (most actionable):
      1. plan_too_granular (plan_written event with task_count > threshold)
      2. too_many_edits (edit_total > threshold)
      3. doc_for_tactical_work
    (too_many_new_files retired 2026-07-04 — over-fired noise.)
    Returns drift class string or None.
    """
    th = thresholds if thresholds is not None else KISS_THRESHOLDS

    plan_count = event_counts.get("plan_written", 0)
    if plan_count >= 1 and recent_plan_task_count is not None:
        if recent_plan_task_count > th["max_plan_tasks"]:
            return "plan_too_granular"

    # PATCH-050: tool_call dropped from edit_total — only file_edited +
    # file_written count as edits.
    edit_total = (
        event_counts.get("file_edited", 0)
        + event_counts.get("file_written", 0)
    )
    if edit_total > th["max_edit_calls_per_session"]:
        return "too_many_edits"

    # too_many_new_files RETIRED 2026-07-04 (operator): the count was
    # session-cumulative with no dedupe, so once >5 files existed it re-fired
    # the nudge on every subsequent turn (82x in one session), and normal dev
    # + asset-generating projects blow past 5 immediately. Advisory-only, so
    # removing it changes no control flow — just stops the noise.

    doc_total = event_counts.get("audit_written", 0) + event_counts.get("doc_written", 0)
    if doc_total >= 2 and edit_total < doc_total:
        return "doc_for_tactical_work"

    return None


def detect_completion_unverified(
    observations: list[dict[str, Any]],
) -> str | None:
    """Detect a completion_claim_logged event without a subsequent
    verification within 5 events.

    Mirrors plugin's `_detect_completion_unverified`.
    """
    recent = observations[-50:]
    if not recent:
        return None

    for i, ev in enumerate(recent):
        if ev.get("event") == "completion_claim_logged":
            window = recent[i : i + 5]
            verified = any(
                e.get("event") in ("verification_run", "test_run", "command_executed")
                for e in window
            )
            if not verified:
                return "completion_unverified"
    return None


# ============================================================================
# Section 5 — Integrator (parity-fix)
# ============================================================================


def compose_stress_nudge(
    *,
    excerpt: ExcerptPayload,
    event_counts: dict[str, int] | None = None,
    recent_plan_task_count: int | None = None,
    drift_seen_entries: list[dict[str, Any]] | None = None,
    last_event_iso: str | None = None,
    current_session_activity: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Stress-response pillar's nudge composer. Returns one nudge line or None.

    Mirrors plugin's `compose_stress_nudge` — sensitization (drift recurrence)
    fires first because recurrent threats are sharper signals than mere
    overcomplication. KISS is next; completion-unverified, tool-failure
    cluster, idle-resumption, scope-expansion follow.

    Caller provides the wrapper-aggregated state. None for any optional
    parameter means "skip that detector". When everything optional is None,
    only drift_check, completion-unverified, and tool-failure-repeated
    (which read excerpt.observations directly) can fire.
    """
    # 1. Drift recurrence (sensitization) — highest priority
    if current_session_activity is not None and excerpt.corrections:
        recurrence = detect_drift_recurrence(
            corrections=excerpt.corrections,
            current_session_activity=current_session_activity,
            drift_seen_entries=drift_seen_entries,
            now=now,
        )
        if recurrence:
            prior = recurrence["prior_correction_text"]
            prior_snippet = prior[:80] + "..." if len(prior) > 80 else prior
            return (
                f"stress: drift_recurrence. "
                f"{STRESS_SKILL_MAP['drift_recurrence']} "
                f"(prior: '{prior_snippet}')"
            )

    # 2. KISS check
    if event_counts is not None:
        kiss_drift = detect_kiss_drift(
            event_counts=event_counts,
            recent_plan_task_count=recent_plan_task_count,
        )
        if kiss_drift:
            skill_hint = STRESS_SKILL_MAP.get(kiss_drift, "")
            return f"stress: {kiss_drift}. {skill_hint}".strip()

    # 3. Completion-unverified
    completion_drift = detect_completion_unverified(excerpt.observations)
    if completion_drift:
        skill_hint = STRESS_SKILL_MAP.get(completion_drift, "")
        return f"stress: {completion_drift}. {skill_hint}".strip()

    # 4. Tool-failure cluster
    failures = _detect_repeated_tool_failures(excerpt.observations)
    if failures:
        skill_hint = STRESS_SKILL_MAP.get("tool_failure_cluster", "")
        return f"stress: tool_failure_cluster ({len(failures)}). {skill_hint}".strip()

    # 5. Idle resumption
    if last_event_iso and detect_idle_resumption(
        last_event_iso=last_event_iso, now=now
    ):
        skill_hint = STRESS_SKILL_MAP.get("idle_resumption", "")
        return f"stress: idle_resumption. {skill_hint}".strip()

    # 6. Scope expansion — plugin has this as a stub returning None.
    # Preserved-for-parity but no detection logic until both sides implement it.
    return None


# ============================================================================
# Section 6 — MCP tool dispatch
# ============================================================================


def stress_response_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below.

    Sets `current_pillar_id` for the duration of the call so the
    RequestLoggingMiddleware can attribute the request to this pillar.
    """
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        details = question.details or {}

        # Section 1 — γ-landed
        if qt == "drift_check":
            return _drift_check(excerpt)
        if qt == "should_fire_cascade":
            return _evaluate_cascade(question)
        if qt == "detect_repeated_tool_failure":
            return _evaluate_repeated_failure(excerpt, question)
        # v0.2.6 PATCH-128 Path A additive — orphan tool_call detector
        # for cases where Claude Code's harness skips PostToolUse and
        # `_detect_repeated_tool_failures` can't see failures.
        if qt == "detect_orphan_tool_failures":
            return _evaluate_orphan_failures(excerpt, question)

        # Section 2 — allostatic state
        if qt == "compute_thriving_signal":
            return _compute_thriving_response(excerpt, details)
        if qt == "format_thriving_surface":
            thriving = bool(details.get("thriving", False))
            return _pure_text_response(
                action="emit_thriving_surface" if thriving else "no_op",
                body=format_thriving_surface(thriving),
                decision_class="thriving" if thriving else "not_thriving",
            )
        if qt == "check_recovery":
            threshold = coerce_int(details.get("recovery_event_threshold"), 10)
            recovered = check_recovery(
                excerpt.observations, recovery_event_threshold=threshold
            )
            return _bool_response(
                action="recovered" if recovered else "still_stressed",
                details_extra={"recovery_event_threshold": threshold},
                decision_class="recovered" if recovered else "still_stressed",
                value=recovered,
                body=(
                    "Recovery observed (no threats in last "
                    f"{threshold} events)."
                    if recovered else
                    f"Still stressed (threat within last {threshold} events)."
                ),
            )
        if qt == "detect_chronic_stress":
            window_days = coerce_int(details.get("window_days"), 30)
            chronic_threshold = coerce_int(details.get("chronic_threshold"), 10)
            chronic = detect_chronic_stress(
                excerpt.observations,
                window_days=window_days,
                chronic_threshold=chronic_threshold,
            )
            return _bool_response(
                action="chronic_stress_detected" if chronic else "no_chronic_stress",
                details_extra={
                    "window_days": window_days,
                    "chronic_threshold": chronic_threshold,
                },
                decision_class="chronic" if chronic else "no_chronic",
                value=chronic,
                body=(
                    f"Chronic stress detected: ≥{chronic_threshold} threats "
                    f"in last {window_days} days."
                    if chronic else None
                ),
            )
        if qt == "compute_state_marker":
            marker = compute_state_marker(
                cascade_active=bool(details.get("cascade_active", False)),
                events_since_threat=coerce_int(details.get("events_since_threat"), 0),
                thriving=bool(details.get("thriving", False)),
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="state_marker",
                    body=f"Allostat state: `{marker}`.",
                    details={"marker": marker},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=marker,
                    fingerprint=marker,
                ),
            )

        # Section 3 — sensitization
        if qt == "compute_salience":
            salience = compute_salience(
                last_seen=details.get("last_seen"),
                half_life_minutes=details.get("half_life_minutes"),
            )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="salience",
                    details={"salience": salience},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="salience_computed",
                ),
            )
        if qt == "should_suppress_by_salience":
            suppress = should_suppress_by_salience(
                last_seen=details.get("last_seen"),
                half_life_minutes=details.get("half_life_minutes"),
                threshold=float(
                    details.get("threshold", _DEFAULT_SUPPRESSION_THRESHOLD)
                ),
            )
            return _bool_response(
                action="suppress" if suppress else "allow_fire",
                details_extra={
                    "threshold": float(
                        details.get("threshold", _DEFAULT_SUPPRESSION_THRESHOLD)
                    )
                },
                decision_class="suppressed" if suppress else "fire_allowed",
                value=suppress,
            )
        if qt == "detect_drift_recurrence":
            return _drift_recurrence_response(excerpt, details)

        # Section 4 — KISS family
        if qt == "detect_idle_resumption":
            idle = detect_idle_resumption(
                last_event_iso=details.get("last_event_iso"),
                threshold_minutes=int(
                    details.get(
                        "threshold_minutes", IDLE_RESUMPTION_THRESHOLD_MINUTES
                    )
                ),
            )
            return _bool_response(
                action="idle_resumption" if idle else "no_op",
                details_extra={"last_event_iso": details.get("last_event_iso")},
                decision_class="idle" if idle else "not_idle",
                value=idle,
                body=(
                    f"Resumption after idle gap "
                    f"(threshold: {IDLE_RESUMPTION_THRESHOLD_MINUTES}min)."
                    if idle else None
                ),
            )
        if qt == "detect_kiss_drift":
            drift_class = detect_kiss_drift(
                event_counts=details.get("event_counts", {}) or {},
                recent_plan_task_count=details.get("recent_plan_task_count"),
                thresholds=details.get("thresholds"),
            )
            if drift_class is None:
                return PillarToolResponse(
                    decision=DecisionPayload(
                        action="no_op",
                        details={"drift_class": None},
                    ),
                    stats=StatsPayload(
                        pillar_id=PILLAR_ID,
                        decision_class="no_kiss_drift",
                    ),
                )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_kiss_nudge",
                    body=f"stress: {drift_class}. {STRESS_SKILL_MAP.get(drift_class, '')}".strip(),
                    details={"drift_class": drift_class},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="kiss_fired",
                    fingerprint=drift_class,
                ),
            )
        if qt == "detect_completion_unverified":
            drift_class = detect_completion_unverified(excerpt.observations)
            if drift_class is None:
                return PillarToolResponse(
                    decision=DecisionPayload(action="no_op"),
                    stats=StatsPayload(
                        pillar_id=PILLAR_ID,
                        decision_class="no_completion_unverified",
                    ),
                )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_completion_nudge",
                    body=f"stress: {drift_class}. {STRESS_SKILL_MAP.get(drift_class, '')}".strip(),
                    details={"drift_class": drift_class},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="completion_unverified_fired",
                    fingerprint=drift_class,
                ),
            )

        # Section 5 — integrator
        if qt == "compose_stress_nudge":
            nudge = compose_stress_nudge(
                excerpt=excerpt,
                event_counts=details.get("event_counts"),
                recent_plan_task_count=details.get("recent_plan_task_count"),
                drift_seen_entries=details.get("drift_seen_entries"),
                last_event_iso=details.get("last_event_iso"),
                current_session_activity=details.get("current_session_activity"),
            )
            if nudge is None:
                return PillarToolResponse(
                    decision=DecisionPayload(action="no_op"),
                    stats=StatsPayload(
                        pillar_id=PILLAR_ID, decision_class="silent"
                    ),
                )
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="emit_stress_nudge",
                    body=nudge,
                    details={"composer": "compose_stress_nudge"},
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="composed",
                    fingerprint=_hash_drift_payload(nudge),
                ),
            )

        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


# ---------- Question-type handlers ----------


def _drift_check(excerpt: ExcerptPayload) -> PillarToolResponse:
    threats = _classify_threats(excerpt.observations)
    synthetic = _detect_repeated_tool_failures(excerpt.observations)
    all_threats = threats + synthetic
    by_severity: Counter[str] = Counter(t["severity"] for t in all_threats)
    if not all_threats:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={"threats": [], "counts_by_severity": {}},
            ),
            stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="no_threats"),
        )
    highest = max(all_threats, key=lambda t: SEVERITY_RANK.get(t["severity"], 0))
    fast, slow = _should_fire_cascade(highest["severity"])
    return PillarToolResponse(
        decision=DecisionPayload(
            action="emit_stress_nudge" if fast or slow else "no_op",
            body=_summarize_threats(all_threats),
            confidence=1.0 if highest["severity"] == "lethal" else 0.8,
            details={
                "threats": all_threats,
                "counts_by_severity": dict(by_severity),
                "highest_severity": highest["severity"],
                "fast_pathway_fires": fast,
                "slow_pathway_active_until_pending": slow,
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fired" if fast or slow else "no_threats",
            fingerprint=f"{highest['threat_class']}:{highest['severity']}",
        ),
    )


def _evaluate_cascade(question: QuestionPayload) -> PillarToolResponse:
    severity = question.details.get("severity")
    if severity is None:
        return _no_op(
            decision_class="missing_severity",
            reason="question.details.severity required",
        )
    fast, slow = _should_fire_cascade(severity)
    return PillarToolResponse(
        decision=DecisionPayload(
            action="fire_cascade" if (fast or slow) else "no_op",
            body=(
                f"Stress cascade fired (severity=`{severity}`, "
                f"fast={fast}, slow={slow})."
                if (fast or slow) else None
            ),
            details={"fast_pathway_fires": fast, "slow_pathway_fires": slow},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fired" if (fast or slow) else "suppressed",
        ),
    )


def _evaluate_repeated_failure(
    excerpt: ExcerptPayload, question: QuestionPayload
) -> PillarToolResponse:
    threshold_raw = question.details.get("failure_threshold", 3)
    try:
        threshold = int(threshold_raw)
    except (TypeError, ValueError):
        threshold = 3
    synthetic = _detect_repeated_tool_failures(
        excerpt.observations, failure_threshold=threshold
    )
    if not synthetic:
        return _no_op(decision_class="no_repeated_failures")
    return PillarToolResponse(
        decision=DecisionPayload(
            action="emit_stress_nudge",
            body=_summarize_threats(synthetic),
            details={"threats": synthetic},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fired",
            fingerprint="tool_failure_repeated",
        ),
    )


def _evaluate_orphan_failures(
    excerpt: ExcerptPayload, question: QuestionPayload
) -> PillarToolResponse:
    """v0.2.6 PATCH-128 Path A: detect repeated tool failures even when
    PostToolUse doesn't fire (file-not-found Read class). Counts orphan
    tool_call events without matching tool_call_succeeded.

    Item 50 (2026-06-10) round-trip: prior salience state arrives from the
    wrapper in `question.details.orphan_salience_state` ({"entries": [...]});
    the updated list goes back in `decision.details.orphan_state` on EVERY
    path (no_op included) so the wrapper's persist step stays uniform.
    Missing field → empty default (both sides roll independently).
    """
    threshold_raw = question.details.get("failure_threshold", 3)
    try:
        threshold = int(threshold_raw)
    except (TypeError, ValueError):
        threshold = 3
    prior_raw = question.details.get("orphan_salience_state")
    prior_entries: list[dict[str, Any]] = []
    if isinstance(prior_raw, dict):
        raw_entries = prior_raw.get("entries")
        if isinstance(raw_entries, list):
            prior_entries = [e for e in raw_entries if isinstance(e, dict)]
    synthetic, updated_state = _detect_orphan_tool_call_failures(
        excerpt.observations,
        failure_threshold=threshold,
        prior_orphan_state=prior_entries,
    )
    orphan_state = {"entries": updated_state}
    if not synthetic:
        return _no_op(
            decision_class="no_orphan_failures",
            details_extra={"orphan_state": orphan_state},
        )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="emit_stress_nudge",
            body=_summarize_threats(synthetic),
            details={"threats": synthetic, "orphan_state": orphan_state},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fired",
            fingerprint="orphan_tool_call_repeated",
        ),
    )


def _compute_thriving_response(
    excerpt: ExcerptPayload, details: dict[str, Any]
) -> PillarToolResponse:
    window_days = coerce_int(
        details.get("window_days"), THRIVING_THRESHOLDS["window_days"]
    )
    thriving = compute_thriving_signal(
        excerpt.observations, window_days=window_days
    )
    return _bool_response(
        action="thriving" if thriving else "not_thriving",
        details_extra={
            "window_days": window_days,
            "surface": format_thriving_surface(thriving),
        },
        decision_class="thriving" if thriving else "not_thriving",
        value=thriving,
        body=format_thriving_surface(thriving) if thriving else None,
    )


def _drift_recurrence_response(
    excerpt: ExcerptPayload, details: dict[str, Any]
) -> PillarToolResponse:
    current_session_activity = details.get("current_session_activity")
    if not isinstance(current_session_activity, list):
        return _no_op(
            decision_class="missing_session_activity",
            reason="question.details.current_session_activity (list) required",
        )
    recurrence = detect_drift_recurrence(
        corrections=excerpt.corrections,
        current_session_activity=current_session_activity,
        drift_seen_entries=details.get("drift_seen_entries"),
        suppression_threshold=float(
            details.get("suppression_threshold", _DEFAULT_SUPPRESSION_THRESHOLD)
        ),
    )
    if recurrence is None:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={"recurrence": None},
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="no_recurrence",
            ),
        )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="emit_recurrence_nudge",
            body=(
                f"stress: drift_recurrence. "
                f"{STRESS_SKILL_MAP['drift_recurrence']} "
                f"(prior: '{recurrence['prior_correction_text'][:80]}')"
            ),
            details=recurrence,
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="recurrence_fired",
            fingerprint=_hash_drift_payload(recurrence["prior_correction_text"]),
        ),
    )


# ---------- Response builders ----------


def _bool_response(
    *,
    action: str,
    details_extra: dict[str, Any],
    decision_class: str,
    value: bool,
    body: str | None = None,
) -> PillarToolResponse:
    """Build a bool-flag response. `body` (PATCH-128) is the optional
    operator-facing string for `_aggregate`; if omitted, decision stays
    silent through the dispatch gate even when action is non-no_op."""
    return PillarToolResponse(
        decision=DecisionPayload(
            action=action,
            body=body,
            details={"value": value, **details_extra},
        ),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def _pure_text_response(
    *, action: str, body: str, decision_class: str
) -> PillarToolResponse:
    return PillarToolResponse(
        decision=DecisionPayload(action=action, body=body),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def _summarize_threats(threats: list[dict[str, Any]]) -> str:
    if not threats:
        return ""
    by_class = Counter(t["threat_class"] for t in threats)
    parts = [f"{cnt}× {cls}" for cls, cnt in by_class.most_common()]
    highest = max(
        threats,
        key=lambda t: SEVERITY_RANK.get(t["severity"], 0),
    )
    return (
        f"stress-response: {len(threats)} threat(s) — "
        f"{', '.join(parts)}. Highest severity: {highest['severity']}."
    )


def _no_op(
    *,
    decision_class: str,
    reason: str | None = None,
    details_extra: dict[str, Any] | None = None,
) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    if details_extra:
        details.update(details_extra)
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="stress_response_evaluate",
        description=(
            "Stress-response pillar — drift detection, cascade decisions, "
            "sensitization, KISS overcomplication family, and integrator. "
            "See question_type docs in pillar_protocol for the full set."
        ),
    )
    def stress_response_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return stress_response_decision(excerpt, question)
