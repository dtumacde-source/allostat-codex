"""Client-side pattern-fingerprint derivation — LOCKSTEP port of the server's
pattern_observer fingerprinting.

Data-min Option A (operator-chosen 2026-07-14). The pattern-learner is the last
detector still shipping operator language over the wire: the wrapper writes
`operator_language`/`subject` into the observation tail so the SERVER can
fingerprint it into a learned-rule proposal. To keep the words on the operator's
machine, the wrapper computes the fingerprint HERE — subject + direction + a
non-reversible hash — and (in later slices) sends only the derived hash/tokens,
never the raw prompt. This mirrors the wire-privacy pattern already used by
`drift_recurrence_local.py` and `voice_keeper_local.py`.

Faithful port of server/allostat_server/pillars/pattern_observer.py (itself a
mirror of the v2.3 plugin's client original — a port back home).

LOCKSTEP with the server. `Fingerprint.hash_key()` is the cluster key the server
counts by, so if either side's DIRECTION_KEYWORDS / _CLASS_MAP / normalize_subject
/ extract_direction / Fingerprint changes, the N=4 learning loop silently
miscounts. Change both sides together; the cross-side parity test
(tests/test_pattern_fingerprint_local_port.py) pins byte-equality.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Operator-language -> direction tag heuristics. LOCKSTEP with server
# pattern_observer.DIRECTION_KEYWORDS (mirror of plugin DIRECTION_KEYWORDS).
DIRECTION_KEYWORDS = [
    (re.compile(r"\b(don'?t|do not|stop|avoid|never)\s+(\w+)", re.IGNORECASE), "avoid_{}"),
    (re.compile(r"\b(prefer|always|please)\s+(\w+)", re.IGNORECASE), "prefer_{}"),
    (re.compile(r"\b(add|include)\s+(\w+)", re.IGNORECASE), "add_{}"),
    (re.compile(r"\b(remove|delete|cut)\s+(\w+)", re.IGNORECASE), "remove_{}"),
    (re.compile(r"\b(escalate|emphasize|highlight)\s+(\w+)", re.IGNORECASE), "escalate_{}"),
    (re.compile(r"\b(silent|silently|quietly|without\s+noise)", re.IGNORECASE), "silent_apply"),
]

# Normalize various event/class spellings to canonical class names.
# LOCKSTEP with server pattern_observer._CLASS_MAP.
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


def normalize_subject(text: str) -> str:
    """Strip surface variation, return canonical subject token string.

    LOCKSTEP with server pattern_observer.normalize_subject.
    """
    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    parts = s.split("-")[:8]
    return "-".join(parts)


def extract_direction(text: str) -> str:
    """Map operator language to a direction tag (e.g., avoid_X).

    LOCKSTEP with server pattern_observer.extract_direction.
    """
    if not text:
        return "unknown"
    for pattern, template in DIRECTION_KEYWORDS:
        m = pattern.search(text)
        if m:
            verb_target = m.group(2) if m.lastindex and m.lastindex >= 2 else "general"
            return template.format(verb_target.lower())
    return "unspecified"


def fingerprint_event(event: dict[str, Any]) -> Fingerprint | None:
    """Extract a Fingerprint from a single observation event. Returns None when
    the event doesn't carry enough signal to fingerprint.

    LOCKSTEP with server pattern_observer.fingerprint_event.
    """
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


# Cross-session learning-loop threshold. LOCKSTEP with server DEFAULT_N_THRESHOLD.
DEFAULT_N_THRESHOLD = 4


def _opposite_direction(direction: str) -> str | None:
    """Return the opposite direction tag if there is one (for contradiction reset).

    LOCKSTEP with server pattern_observer._opposite_direction.
    """
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
    """Generate a suggested rule wording from the fingerprint.

    LOCKSTEP with server pattern_observer._suggest_wording.
    """
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
    """True if event represents an operator overriding a Claude recommendation.

    LOCKSTEP with server pattern_observer._is_override_event.
    """
    event_type = event.get("event", "")
    if event_type == "recommendation_override":
        return True
    if event_type == "operator_correction":
        details = event.get("details") or {}
        if details.get("override_target") or details.get("is_override"):
            return True
    return False


def detect_proposals(
    events: list[dict[str, Any]],
    n_threshold: int = DEFAULT_N_THRESHOLD,
) -> list[dict[str, Any]]:
    """Walk events, fingerprint each, count with contradiction reset, return
    proposals where count >= n_threshold.

    LOCKSTEP with server pattern_observer.detect_proposals.
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
                    "occurrences": list(occurrences),
                    "occurrence_count": len(occurrences),
                    "first_seen": min(timestamps) if timestamps else "",
                    "last_seen": max(timestamps) if timestamps else "",
                    "sessions_spanning": len(sessions),
                    "suggested_rule_wording": _suggest_wording(fp),
                }
            )
    return proposals


def detect_in_session_overrides(
    events: list[dict[str, Any]],
    *,
    current_session_id: str | None = None,
    n_threshold: int = 2,
) -> list[dict[str, Any]]:
    """Detect N=2 in-session override patterns (fast-promotion lane).

    LOCKSTEP with server pattern_observer.detect_in_session_overrides.
    """
    if not events:
        return []

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
                    "occurrences": list(occurrences),
                    "occurrence_count": len(occurrences),
                    "first_seen": min(timestamps) if timestamps else "",
                    "last_seen": max(timestamps) if timestamps else "",
                    "sessions_spanning": 1,
                    "suggested_rule_wording": _suggest_wording(fp),
                    "flags": ["fast-promotion-lane", "n2-in-session"],
                }
            )
    return proposals
