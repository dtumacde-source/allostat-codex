"""Pattern-observer pillar — N=4 cross-session learning loop.

The pattern-observer detects when the same operator behavior pattern
appears N times in the recent observation buffer. When the threshold
hits, it proposes a learned rule the operator can promote. Geometric
ladder: N=2 in-session fast lane (detect_in_session_overrides) →
N=2²=4 cross-session (detect_proposals).

Server is stateless per call. The wrapper plugin maintains the rolling
observation buffer client-side and sends the relevant slice on each
call. The server fingerprints + counts + returns proposals.

Tool exposed by δ:

    pattern_observer_evaluate(excerpt, question) → PillarToolResponse

  question.question_type ∈ {"detect_proposals", "fingerprint_event"}

  For "detect_proposals", question.details may include:
    n_threshold: int (default 4) — count required for a proposal

  Decision logic is a faithful port of plugin/lib/pattern_observer.py's
  detect_proposals + fingerprint_event + _opposite_direction +
  _suggest_wording, minus the filesystem read (operates on the wrapper-
  supplied excerpt.observations).
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP

from .._time import utc_now_iso as _utc_now_iso
from ..pillar_protocol import (
    ClientStateWrite,
    DecisionPayload,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "pattern-observer"

# Cross-session learning-loop threshold. Geometric ladder: N=2 in-session
# (fast lane, detect_in_session_overrides) → N=2²=4 cross-session (this).
# (Was 5; aligned to the N=2/N=4 ladder 2026-06-04.)
DEFAULT_N_THRESHOLD = 4

# Operator-language → direction tag heuristics. Mirrors
# plugin/lib/pattern_observer.py::DIRECTION_KEYWORDS.
DIRECTION_KEYWORDS = [
    (re.compile(r"\b(don'?t|do not|stop|avoid|never)\s+(\w+)", re.IGNORECASE), "avoid_{}"),
    (re.compile(r"\b(prefer|always|please)\s+(\w+)", re.IGNORECASE), "prefer_{}"),
    (re.compile(r"\b(add|include)\s+(\w+)", re.IGNORECASE), "add_{}"),
    (re.compile(r"\b(remove|delete|cut)\s+(\w+)", re.IGNORECASE), "remove_{}"),
    (re.compile(r"\b(escalate|emphasize|highlight)\s+(\w+)", re.IGNORECASE), "escalate_{}"),
    (re.compile(r"\b(silent|silently|quietly|without\s+noise)", re.IGNORECASE), "silent_apply"),
]

# Normalize various event/class spellings to canonical class names.
_CLASS_MAP = {
    "operator_correction": "correction",
    "manual_action": "manual_action",
    "manual": "manual_action",
    "recovery": "recovery",
    "rec": "recovery",
    "edit": "edit",
    "ed": "edit",
    "correction": "correction",
    "recommendation_override": "override",
}

_FINGERPRINTABLE_CLASSES = set(_CLASS_MAP.keys())


@dataclass(frozen=True)
class Fingerprint:
    class_: str
    subject: str
    direction: str

    def hash_key(self) -> str:
        s = f"{self.class_}:{self.subject}:{self.direction}"
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    def __str__(self) -> str:
        return f"{self.class_}:{self.subject}:{self.direction}"


# ---------- Decision helpers (pure functions, no I/O) ----------


def normalize_subject(text: str) -> str:
    """Strip surface variation, return canonical subject token string.

    Mirrors plugin/lib/pattern_observer.py::normalize_subject.
    """
    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    parts = s.split("-")[:8]
    return "-".join(parts)


def extract_direction(text: str) -> str:
    """Map operator language to a direction tag (e.g., avoid_X)."""
    if not text:
        return "unknown"
    for pattern, template in DIRECTION_KEYWORDS:
        m = pattern.search(text)
        if m:
            verb_target = m.group(2) if m.lastindex and m.lastindex >= 2 else "general"
            return template.format(verb_target.lower())
    return "unspecified"


def fingerprint_event(event: dict[str, Any]) -> Fingerprint | None:
    """Extract a Fingerprint from a single observation event. Returns
    None when the event doesn't carry enough signal to fingerprint."""
    class_ = event.get("class") or event.get("event")
    if class_ not in _FINGERPRINTABLE_CLASSES:
        return None
    class_norm = _CLASS_MAP.get(class_, class_)

    details = event.get("details") or {}
    subject_text = details.get("subject") or details.get("text") or event.get("subject", "")
    subject = normalize_subject(subject_text)
    if not subject:
        return None

    direction_text = details.get("operator_language") or details.get("direction") or subject_text
    direction = extract_direction(direction_text)
    return Fingerprint(class_=class_norm, subject=subject, direction=direction)


def _opposite_direction(direction: str) -> str | None:
    """Return the opposite direction tag if there is one (for contradiction reset)."""
    if direction.startswith("avoid_"):
        return "prefer_" + direction[6:]
    if direction.startswith("prefer_"):
        return "avoid_" + direction[7:]
    if direction.startswith("add_"):
        return "remove_" + direction[4:]
    if direction.startswith("remove_"):
        return "add_" + direction[7:]
    return None


def _suggest_wording(fp: Fingerprint) -> str:
    """Generate a suggested rule wording from the fingerprint."""
    direction = fp.direction
    subject = fp.subject.replace("-", " ")
    if direction.startswith("avoid_"):
        return f"Default behavior: avoid {direction[6:]} when working on {subject}."
    if direction.startswith("prefer_"):
        return f"Default behavior: prefer {direction[7:]} when working on {subject}."
    if direction.startswith("add_"):
        return f"Default behavior: add {direction[4:]} to outputs related to {subject}."
    if direction.startswith("remove_"):
        return f"Default behavior: remove {direction[7:]} from outputs related to {subject}."
    if direction == "silent_apply":
        return f"Default behavior: apply learned pattern silently for {subject}."
    return f"Pattern detected on {subject}: {direction}. Rule wording needs operator amplification."


def _is_override_event(event: dict[str, Any]) -> bool:
    """Mirror plugin/lib/pattern_observer.py::_is_override_event.

    True if event represents an operator overriding a Claude recommendation.
    Either a direct `recommendation_override` event, or an
    `operator_correction` event whose details mark it as an override.
    """
    event_type = event.get("event", "")
    if event_type == "recommendation_override":
        return True
    if event_type == "operator_correction":
        details = event.get("details") or {}
        if details.get("override_target") or details.get("is_override"):
            return True
    return False


def detect_in_session_overrides(
    events: list[dict[str, Any]],
    *,
    current_session_id: str | None = None,
    n_threshold: int = 2,
) -> list[dict[str, Any]]:
    """Detect N=2 in-session override patterns (fast-promotion lane).

    Mirrors plugin/lib/pattern_observer.py::detect_in_session_overrides.

    Differs from `detect_proposals` in three ways:
      1. N threshold is 2 (faster lane than the 4 cross-session threshold).
      2. Contradiction reset is NOT applied — same-session operator chose
         this direction explicitly; a single waffle shouldn't reset.
      3. Filters to events with matching sessionId AND override semantics.

    If `current_session_id` is None, infers it from the most-recent event
    in the input that carries a `details.sessionId` field. If none found,
    returns [].

    Returns list of proposal dicts (same shape as detect_proposals) with
    `flags=["fast-promotion-lane", "n2-in-session"]`.
    """
    if not events:
        return []

    # Resolve current_session_id from the most-recent event if not provided.
    if current_session_id is None:
        for e in reversed(events):
            sid = (e.get("details") or {}).get("sessionId")
            if sid:
                current_session_id = sid
                break
    if current_session_id is None:
        return []

    session_overrides = [
        e for e in events
        if (e.get("details") or {}).get("sessionId") == current_session_id
        and _is_override_event(e)
    ]

    counters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fp_by_key: dict[str, Fingerprint] = {}
    for e in session_overrides:
        fp = fingerprint_event(e)
        if fp is None:
            continue
        key = fp.hash_key()
        fp_by_key[key] = fp
        counters[key].append(e)

    proposals: list[dict[str, Any]] = []
    for key, occurrences in counters.items():
        if len(occurrences) >= n_threshold:
            fp = fp_by_key[key]
            timestamps = [o.get("timestamp", "") for o in occurrences]
            proposals.append(
                {
                    "fingerprint": str(fp),
                    "hash": key,
                    "class": fp.class_,
                    "subject": fp.subject,
                    "direction": fp.direction,
                    # The matched events themselves — the renderers
                    # (format_in_session_proposal_surface) read `occurrences`
                    # for the Examples block + the "+N more" line. Without
                    # this the persisted-then-rendered surface shows an EMPTY
                    # examples block. Kept in lockstep with occurrence_count
                    # so len(occurrences) == occurrence_count (the renderer
                    # derives both the example list and the count from it).
                    "occurrences": list(occurrences),
                    "occurrence_count": len(occurrences),
                    "first_seen": min(timestamps) if timestamps else "",
                    "last_seen": max(timestamps) if timestamps else "",
                    "sessions_spanning": 1,  # by definition, same session
                    "suggested_rule_wording": _suggest_wording(fp),
                    "flags": ["fast-promotion-lane", "n2-in-session"],
                }
            )
    return proposals


def _event_excerpt(event: dict[str, Any]) -> str:
    """Short excerpt of an event for proposal display.

    Mirrors plugin/lib/pattern_observer.py::_event_excerpt.
    """
    details = event.get("details") or {}
    text = (
        details.get("text")
        or details.get("subject")
        or details.get("operator_language", "")
    )
    text = str(text)[:80]
    ts = event.get("timestamp", "")[:10]
    return f"[{ts}] {text}"


def format_proposal_surface(proposal: dict[str, Any]) -> str:
    """Format an N=4 cross-session proposal for operator review.

    Mirrors plugin/lib/pattern_observer.py::format_proposal_surface — same
    text, same option keys, same icon, so the wrapper renders identically
    to the v2.3 plugin.
    """
    occurrences = proposal.get("occurrences", [])
    examples_text = "\n".join(
        f"     {i+1}. {_event_excerpt(e)}"
        for i, e in enumerate(occurrences[:3])
    )
    extra = (
        f"\n     (+{len(occurrences) - 3} more)"
        if len(occurrences) > 3 else ""
    )
    sessions_spanning = proposal.get("sessions_spanning", 0)
    # Pre-release item 26: the count was a stale hardcoded "five times"
    # (the threshold moved to N=4 on 2026-06-04). Interpolate the real
    # occurrence count, falling back to the threshold constant.
    occurrence_count = len(occurrences) or DEFAULT_N_THRESHOLD
    return f"""
🧬  Allostat Pattern Observer

I've seen this pattern {occurrence_count} times across {sessions_spanning} session(s):

  Class:    {proposal.get('class', '')}
  Pattern:  {proposal.get('subject', '').replace('-', ' ')}
  Direction: {proposal.get('direction', '')}
  Examples:
{examples_text}{extra}

Suggested rule:
  {proposal.get('suggested_rule_wording', '')}

Should this become a rule?

  [a] Yes, add as a learned rule (project-scoped)
  [b] Yes, and promote across all linked projects (umbrella scope)
  [c] Yes, and promote to user-level (applies everywhere)
  [d] No, don't add — but suppress this pattern for the rest of the session
  [e] Not now — ask again in 24 hours

  Edit the rule wording before saving? Type "edit" instead.
""".strip()


def format_in_session_proposal_surface(proposal: dict[str, Any]) -> str:
    """Format an N=2 in-session-override proposal for operator review.

    Mirrors plugin/lib/pattern_observer.py::format_in_session_proposal_surface
    — faster lane, more confident framing.
    """
    occurrences = proposal.get("occurrences", [])
    examples_text = "\n".join(
        f"     {i+1}. {_event_excerpt(e)}"
        for i, e in enumerate(occurrences[:2])
    )
    extra = (
        f"\n     (+{len(occurrences) - 2} more)"
        if len(occurrences) > 2 else ""
    )
    return f"""
🧬⚡  Allostat Pattern Observer (fast-promotion lane)

You overrode this recommendation class TWICE in this session:

  Class:    {proposal.get('class', '')}
  Pattern:  {proposal.get('subject', '').replace('-', ' ')}
  Direction: {proposal.get('direction', '')}
  Examples:
{examples_text}{extra}

Suggested rule (skip the relitigation next time):
  {proposal.get('suggested_rule_wording', '')}

Promote now?

  [a] Yes, project-scoped (next time on this project, default to your override)
  [b] Yes, umbrella-scoped (applies to all linked verticals — next CRNA/DPT/etc. inherits)
  [c] Yes, user-tier (applies operator-wide via CLAUDE.md append)
  [d] No, this was situational — don't generalize
  [e] Edit wording before saving — type "edit"
""".strip()


# v0.2.6 PATCH-128 body composer — one-line summary for chrome-row surfacing.
# Multi-line `format_proposal_surface` / `format_in_session_proposal_surface`
# above are for the interactive /allostat-promote dawn-phase prompt;
# `_compose_proposals_body` is the concise inline-nudge form `_aggregate` uses.
def _compose_proposals_body(
    proposals: list[dict[str, Any]],
    n_threshold: int,
    *,
    kind: str = "n5",
) -> str:
    count = len(proposals)
    first = proposals[0] if proposals else {}
    first_class = first.get("class", "?")
    first_subject = str(first.get("subject", "")).replace("-", " ")[:60]
    if kind == "fast_promotion":
        prefix = "Pattern (fast-promotion)"
    else:
        prefix = f"Pattern N={n_threshold}"
    if count == 1:
        return (
            f"{prefix}: 1 proposal queued — `{first_class}` / "
            f"\"{first_subject}\". Review at next `/allostat-promote`."
        )
    return (
        f"{prefix}: {count} proposals queued (first: `{first_class}` / "
        f"\"{first_subject}\"). Review at next `/allostat-promote`."
    )


def detect_proposals(
    events: list[dict[str, Any]],
    n_threshold: int = DEFAULT_N_THRESHOLD,
) -> list[dict[str, Any]]:
    """Walk events, fingerprint each, count occurrences with contradiction
    reset, return proposals where count ≥ n_threshold.

    Mirrors plugin/lib/pattern_observer.py::detect_proposals but operates
    on the wrapper-supplied events list rather than reading observations.jsonl.

    Returns list of proposal dicts: {fingerprint, hash, occurrences,
    occurrence_count, first_seen, last_seen, sessions_spanning,
    suggested_rule_wording}.
    """
    fingerprinted: list[tuple[Fingerprint, dict[str, Any]]] = []
    for e in events:
        fp = fingerprint_event(e)
        if fp:
            fingerprinted.append((fp, e))

    counters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fp_by_key: dict[str, Fingerprint] = {}

    for fp, e in fingerprinted:
        key = fp.hash_key()

        # Contradiction reset: opposite direction same subject zeros both counters.
        opposite = _opposite_direction(fp.direction)
        if opposite:
            opposite_fp = Fingerprint(class_=fp.class_, subject=fp.subject, direction=opposite)
            opposite_key = opposite_fp.hash_key()
            if opposite_key in counters and counters[opposite_key]:
                counters[opposite_key] = []
                counters[key] = []
                continue

        fp_by_key[key] = fp
        counters[key].append(e)

    proposals: list[dict[str, Any]] = []
    for key, occurrences in counters.items():
        if len(occurrences) >= n_threshold:
            fp = fp_by_key[key]
            timestamps = [o.get("timestamp", "") for o in occurrences]
            sessions = {
                o.get("details", {}).get("sessionId")
                or o.get("details", {}).get("session_id")
                for o in occurrences
                if o.get("details")
            }
            sessions.discard(None)
            proposals.append(
                {
                    "fingerprint": str(fp),
                    "hash": key,
                    "class": fp.class_,
                    "subject": fp.subject,
                    "direction": fp.direction,
                    # The matched events themselves — the renderers
                    # (format_proposal_surface) read `occurrences` for the
                    # Examples block, the "+N more" line, AND the headline
                    # count (`len(occurrences) or DEFAULT_N_THRESHOLD`).
                    # Emitting only occurrence_count (the prior behavior) left
                    # the persisted-then-rendered surface with an EMPTY
                    # examples block + the hardcoded fallback count. Kept in
                    # lockstep with occurrence_count.
                    "occurrences": list(occurrences),
                    "occurrence_count": len(occurrences),
                    "first_seen": min(timestamps) if timestamps else "",
                    "last_seen": max(timestamps) if timestamps else "",
                    "sessions_spanning": len(sessions),
                    "suggested_rule_wording": _suggest_wording(fp),
                }
            )
    return proposals


# ---------- MCP tool ----------


def pattern_observer_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below.

    Dispatches the four question_types:
      detect_proposals           — N=4 cross-session learning loop (with contradiction reset)
      detect_in_session_overrides — N=2 same-session override fast-promotion lane
      fingerprint_event          — single-event fingerprint helper
      format_proposal_surface    — operator-facing surface text for N=4 OR N=2 proposals
    """
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        if qt == "fingerprint_event":
            return _fingerprint_single_event(question)
        if qt == "detect_proposals":
            return _detect_proposals_response(excerpt, question)
        if qt == "detect_in_session_overrides":
            return _detect_in_session_overrides_response(excerpt, question)
        if qt == "format_proposal_surface":
            return _format_proposal_response(question)
        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


def _detect_proposals_response(
    excerpt: ExcerptPayload, question: QuestionPayload
) -> PillarToolResponse:
    n_threshold = question.details.get("n_threshold", DEFAULT_N_THRESHOLD)
    try:
        n_threshold = int(n_threshold)
    except (TypeError, ValueError):
        n_threshold = DEFAULT_N_THRESHOLD
    proposals = detect_proposals(excerpt.observations, n_threshold=n_threshold)
    if not proposals:
        # PATCH-165 (2026-05-22): `events_seen` is the COUNT OF OBSERVATIONS
        # PASSED IN, not the count of events that successfully fingerprinted.
        # Advisor 2026-05-22 probe interpreted `events_seen: 5` + `proposals: []`
        # as a bug; the actual semantics are "we received 5 observations but
        # none of them produced a fingerprintable signal" (e.g., events
        # missing details.subject / details.text / event.subject — the keys
        # fingerprint_event() reads). Adding `fingerprintable_count` so
        # downstream audits can distinguish "received N, fingerprinted M".
        fingerprintable_count = sum(
            1 for e in excerpt.observations if fingerprint_event(e) is not None
        )
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={
                    "proposals": [],
                    "events_seen": len(excerpt.observations),
                    "fingerprintable_count": fingerprintable_count,
                },
            ),
            stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="no_proposals"),
        )
    # v0.2.0: persist pending proposals to client-side pending_proposals.jsonl
    # so the next session's dawn-phase can surface them via /allostat-promote.
    writes = [
        ClientStateWrite(
            target="pending_proposals.jsonl",
            mode="append",
            content={
                "timestamp": _utc_now_iso(),
                "kind": "n5_proposal",
                "n_threshold": n_threshold,
                "hash": p["hash"],
                "proposal": p,
            },
            pillar_id=PILLAR_ID,
        )
        for p in proposals
    ]
    return PillarToolResponse(
        decision=DecisionPayload(
            action="propose_rules",
            body=_compose_proposals_body(proposals, n_threshold, kind="n5"),
            details={"proposals": proposals, "n_threshold": n_threshold},
            client_state_writes=writes,
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="proposed",
            fingerprint=proposals[0]["hash"],
        ),
    )


def _detect_in_session_overrides_response(
    excerpt: ExcerptPayload, question: QuestionPayload
) -> PillarToolResponse:
    details = question.details or {}
    n_threshold = details.get("n_threshold", 2)
    try:
        n_threshold = int(n_threshold)
    except (TypeError, ValueError):
        n_threshold = 2
    current_session_id = details.get("current_session_id")
    proposals = detect_in_session_overrides(
        excerpt.observations,
        current_session_id=current_session_id,
        n_threshold=n_threshold,
    )
    if not proposals:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={
                    "proposals": [],
                    "events_seen": len(excerpt.observations),
                    "current_session_id": current_session_id,
                },
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID, decision_class="no_in_session_overrides"
            ),
        )
    # v0.2.0: persist fast-promotion proposals to pending_proposals.jsonl
    writes = [
        ClientStateWrite(
            target="pending_proposals.jsonl",
            mode="append",
            content={
                "timestamp": _utc_now_iso(),
                "kind": "in_session_override",
                "n_threshold": n_threshold,
                "current_session_id": current_session_id,
                "hash": p["hash"],
                "proposal": p,
            },
            pillar_id=PILLAR_ID,
        )
        for p in proposals
    ]
    return PillarToolResponse(
        decision=DecisionPayload(
            action="propose_rules_fast_promotion",
            body=_compose_proposals_body(
                proposals, n_threshold, kind="fast_promotion"
            ),
            details={
                "proposals": proposals,
                "n_threshold": n_threshold,
                "current_session_id": current_session_id,
            },
            client_state_writes=writes,
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="proposed_fast_promotion",
            fingerprint=proposals[0]["hash"],
        ),
    )


def _format_proposal_response(question: QuestionPayload) -> PillarToolResponse:
    """Format a proposal dict into operator-facing surface text.

    `question.details.proposal` carries the proposal dict (from either
    detect_proposals or detect_in_session_overrides). The flags field on
    the proposal selects which formatter to use — N=2 uses the
    fast-promotion-lane surface, N=4 uses the standard surface.
    """
    details = question.details or {}
    proposal = details.get("proposal")
    if not isinstance(proposal, dict):
        return _no_op(
            decision_class="missing_proposal",
            reason="question.details.proposal (dict) required",
        )
    flags = proposal.get("flags") or []
    if "fast-promotion-lane" in flags:
        body = format_in_session_proposal_surface(proposal)
        decision_class = "fast_promotion_surface"
    else:
        body = format_proposal_surface(proposal)
        decision_class = "standard_proposal_surface"
    return PillarToolResponse(
        decision=DecisionPayload(
            action="emit_proposal_surface",
            body=body,
            details={"hash": proposal.get("hash"), "flags": flags},
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class=decision_class,
            fingerprint=proposal.get("hash"),
        ),
    )


def _fingerprint_single_event(question: QuestionPayload) -> PillarToolResponse:
    event = question.details.get("event")
    if not isinstance(event, dict):
        return _no_op(
            decision_class="missing_event",
            reason="question.details.event (dict) required",
        )
    fp = fingerprint_event(event)
    if fp is None:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_fingerprint",
                details={"reason": "event lacks fingerprintable class or subject"},
            ),
            stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="unfingerprintable"),
        )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="fingerprint",
            details={
                "fingerprint": str(fp),
                "hash": fp.hash_key(),
                "class": fp.class_,
                "subject": fp.subject,
                "direction": fp.direction,
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fingerprinted",
            fingerprint=fp.hash_key(),
        ),
    )


def _no_op(*, decision_class: str, reason: str | None = None) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="pattern_observer_evaluate",
        description=(
            "Pattern observer — learning-loop detector. "
            "question_type=detect_proposals: N=4 cross-session patterns "
            "(with contradiction reset). "
            "question_type=detect_in_session_overrides: N=2 same-session "
            "override fast-promotion lane (no contradiction reset). "
            "question_type=fingerprint_event: compute one event's fingerprint. "
            "question_type=format_proposal_surface: render operator-facing "
            "surface text for a proposal (flags field selects N=4 vs N=2 format)."
        ),
    )
    def pattern_observer_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return pattern_observer_decision(excerpt, question)
