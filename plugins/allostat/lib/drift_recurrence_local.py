"""Client-side drift-recurrence detection — "you corrected this before."

Deep-audit 2026-07-02 wire fix (release-gating; advisor greenlit 2026-07-02).
The server's `stress_response.detect_drift_recurrence` could never fire in
production: it needs `excerpt.corrections`, which no wrapper hook ever sent —
and SHOULDN'T send, because correction records carry operator content (the
captured prompt language). The corrections, the current-session activity, and
the drift_seen salience state all already live on the operator's machine, so
the detector runs HERE, entirely locally. Nothing crosses the wire.

Faithful port of the server's implementation (itself documented as a mirror of
the v2.3 plugin's client-side original — this is a port BACK home):
  - PATCH-087 newer-correction supersession + stricter length-scaled overlap
  - PATCH-095 word-boundary regex matching
  - PATCH-048 salience suppression (exponential decay, half-life 30min default,
    suppression threshold 0.4) against .allostat/state/stress_drift_seen.json
Constants are LOCKSTEP with server stress_response.py — if one side changes,
change both.

Entry point: `check_and_render(state_dir, session_id)` — returns the rendered
nudge line (server-parity format: "stress: drift_recurrence. → ...") or None,
and updates drift_seen on fire so salience suppression works next turn.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# LOCKSTEP with server stress_response.py (and the v2.3 plugin).
_DEFAULT_SUPPRESSION_THRESHOLD = 0.4
_DEFAULT_HALF_LIFE_MINUTES = 30
_SKILL_HINT = "→ this drift was corrected before, apply prior correction"


def _half_life_minutes() -> float:
    try:
        return float(os.environ.get(
            "ALLOSTAT_STRESS_HALF_LIFE_MINUTES", str(_DEFAULT_HALF_LIFE_MINUTES)))
    except (TypeError, ValueError):
        return float(_DEFAULT_HALF_LIFE_MINUTES)


def _hash_drift_payload(text: str) -> str:
    """First 16 chars of sha256. Lockstep with server/plugin."""
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def compute_salience(
    *, last_seen: str | None, now: datetime | None = None,
    half_life_minutes: float | None = None,
) -> float:
    """Exponential-decay salience [0..1]. Lockstep with server."""
    if now is None:
        now = datetime.now(timezone.utc)
    hl = half_life_minutes if half_life_minutes is not None else _half_life_minutes()
    last = _parse_iso(last_seen)
    if last is None:
        return 0.0
    elapsed = (now - last).total_seconds()
    if elapsed < 0:
        return 1.0  # clock skew — treat as just-fired
    if hl <= 0:
        return 0.0
    return math.exp(-math.log(2) / (hl * 60.0) * elapsed)


def detect_drift_recurrence(
    *,
    corrections: list[dict[str, Any]],
    current_session_activity: list[dict[str, Any]],
    drift_seen_entries: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    half_life_minutes: float | None = None,
    suppression_threshold: float = _DEFAULT_SUPPRESSION_THRESHOLD,
) -> dict[str, Any] | None:
    """Detect a previously-corrected drift recurring NOW. Lockstep port of
    server stress_response.detect_drift_recurrence — see module docstring."""
    if not corrections or not current_session_activity:
        return None

    normalized: list[dict[str, Any]] = []
    for c in corrections:
        details = c.get("details") or {}
        normalized.append({
            "subject": (details.get("subject") or "").lower(),
            "language": details.get("operator_language") or "",
            "timestamp": c.get("timestamp", ""),
        })

    recent_activity = current_session_activity[-30:]

    # PATCH-087: newer-correction supersession.
    superseded: set[int] = set()
    for j, newer in enumerate(normalized):
        newer_words = {w for w in (newer["subject"] or "").split() if len(w) > 3}
        if len(newer_words) < 2:
            continue
        for k, older in enumerate(normalized[:j]):
            older_words = {w for w in (older["subject"] or "").split() if len(w) > 3}
            if len(older_words) < 2:
                continue
            if len(newer_words & older_words) >= 2:
                superseded.add(k)

    drift_seen_by_hash: dict[str, dict[str, Any]] = {}
    if drift_seen_entries:
        for entry in drift_seen_entries:
            h = entry.get("hash")
            if h:
                drift_seen_by_hash[h] = entry

    for idx in range(len(normalized) - 1, -1, -1):
        if idx in superseded:
            continue
        correction = normalized[idx]
        subj = correction["subject"].strip()
        if not subj or len(subj) < 8:
            continue
        subj_words = {w for w in subj.split() if len(w) > 3}
        if len(subj_words) < 2:
            continue
        # PATCH-087: overlap threshold scales with subject length.
        required = 3 if len(subj_words) <= 6 else max(3, len(subj_words) // 2)
        # PATCH-095: word-boundary regex match.
        patterns = {
            w: re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE)
            for w in subj_words
        }
        for activity in reversed(recent_activity):
            blob = json.dumps(activity.get("details", {}))
            matched = sum(1 for p in patterns.values() if p.search(blob))
            if matched >= required:
                result = {
                    "prior_correction_text": correction["language"][:120],
                    "match_count": matched,
                    "subject": subj,
                }
                if drift_seen_entries is not None:
                    h = _hash_drift_payload(result["prior_correction_text"])
                    entry = drift_seen_by_hash.get(h)
                    if entry:
                        sal = compute_salience(
                            last_seen=entry.get("last_seen") or entry.get("first_seen"),
                            now=now, half_life_minutes=half_life_minutes,
                        )
                        if sal > suppression_threshold:
                            return None
                return result
    return None


def render_nudge(recurrence: dict[str, Any]) -> str:
    """Server-parity nudge text for a detected recurrence."""
    prior = recurrence.get("prior_correction_text") or ""
    snippet = prior[:80] + "..." if len(prior) > 80 else prior
    return f"stress: drift_recurrence. {_SKILL_HINT} (prior: '{snippet}')"


def _update_drift_seen(state_dir: Path, prior_correction_text: str) -> None:
    """Record the fire in .allostat/state/stress_drift_seen.json so salience
    suppression damps re-fires. Best-effort, atomic write."""
    from local_state import atomic_write_text, utc_iso  # noqa: E402

    path = state_dir / "state" / "stress_drift_seen.json"
    data: dict[str, Any] = {"entries": []}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    h = _hash_drift_payload(prior_correction_text)
    now_iso = utc_iso()
    for entry in data["entries"]:
        if isinstance(entry, dict) and entry.get("hash") == h:
            entry["last_seen"] = now_iso
            break
    else:
        data["entries"].append({"hash": h, "first_seen": now_iso, "last_seen": now_iso})
    try:
        atomic_write_text(path, json.dumps(data, indent=2))
    except Exception:
        pass  # Best-effort: a failed suppression-state write only risks one extra nudge.


def check_and_render(state_dir: Path, session_id: str | None = None) -> str | None:
    """Full local pipeline: read local corrections + current-session activity
    + drift_seen, detect, update suppression state, return the nudge line (or
    None). Never raises — hook never-raise contract."""
    try:
        from local_state import read_recent_observations, read_drift_seen  # noqa: E402

        observations = read_recent_observations(state_dir, window=200)
        corrections = [
            o for o in observations
            if o.get("event") == "operator_correction"
        ]
        if not corrections:
            return None
        if session_id:
            current = [
                o for o in observations
                if (o.get("details") or {}).get("session_id") == session_id
                or o.get("session_id") == session_id
            ] or observations[-30:]
        else:
            current = observations[-30:]
        recurrence = detect_drift_recurrence(
            corrections=corrections,
            current_session_activity=current,
            drift_seen_entries=read_drift_seen(state_dir),
        )
        if not recurrence:
            return None
        _update_drift_seen(state_dir, recurrence["prior_correction_text"])
        return render_nudge(recurrence)
    except Exception:
        return None
