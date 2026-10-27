"""Session-end debrief block — visibility surface for autonomic-silent pillars.

Per advisor visibility-finding §4.2 + advisor reply §3.2: most pillars
(pattern-observer, recall-silos, stress-response, volume-control, voice-keeper,
metabolism) fire autonomic-silent. The operator's PC debrief 2026-05-17
flagged this as the visibility-utility inversion — pillars doing real work
disappear; HPA which often isn't load-bearing fires visibly. This module
implements the session-end surface that restores broader visibility.

Format (advisor §3.2):

    ─────────────────────────────────────────────────────────
    Allostat session summary

    Caught:
      - innate-enforcer blocked N destructive command(s)
      - voice-keeper nudged M instances of off-voice phrasing

    Shaped:
      - file-location prior: 3 writes routed to operator-default location
      - memory schema: 4 memory writes followed canon+cwd structure

    Quiet:
      - pattern-observer: no new pattern accumulation
      - drift-detection: no drift signals
      - recall-silos: 0 hits

    For full detail: /allostat-why
    ─────────────────────────────────────────────────────────

Toggle: ALLOSTAT_SESSION_DEBRIEF=0 disables. Default ON.

Counterfactual labels for the Shaped section are HARDCODED per advisor §3.2
("Don't try to compute counterfactuals dynamically") — they're tied to the
highest-leverage operator-innate priors that Allostat actively enforces.
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Import-safe references — these resolve via _base.py sys.path setup when
# called from a hook; here we use the local_state helpers if available.


_DEFAULT_ENABLED = True
_TOGGLE_ENV_VAR = "ALLOSTAT_SESSION_DEBRIEF"
_SESSION_OBSERVATION_WINDOW = 500   # last N observations to scan
_BANNER_DIVIDER = "─" * 65


@dataclass
class DebriefCounts:
    """Aggregated counts for the session-end debrief render."""

    # Caught — innate-enforcer blocks + voice-keeper nudges
    innate_blocks: int = 0
    voice_nudges: int = 0

    # Shaped — counterfactual-labeled prior activations
    file_location_routes: int = 0
    memory_schema_writes: int = 0
    permission_gates: int = 0
    safety_confirmations: int = 0

    # Quiet — silent pillars (rendered only if 0 to surface negative-space)
    pattern_observer_events: int = 0
    drift_signals: int = 0
    recall_hits: int = 0
    stress_cascade_events: int = 0

    # PATCH-168 — Session work narrative. Sourced from per-tool observations
    # (file_edited / file_written / file_created) + operator_message events.
    # Surfaces the actual session activity in the handoff so next sessions
    # can orient on what was done, not just on which pillars were quiet.
    file_edit_count: int = 0
    file_write_count: int = 0
    file_create_count: int = 0
    distinct_files_touched: list[str] = field(default_factory=list)
    operator_messages: list[str] = field(default_factory=list)


def is_enabled() -> bool:
    """Session-end debrief is on unless operator explicitly opts out."""
    raw = os.environ.get(_TOGGLE_ENV_VAR)
    if raw is None:
        return _DEFAULT_ENABLED
    return raw.strip().lower() not in ("0", "false", "off", "no")


_SESSION_WORK_MAX_FILES = 15  # cap distinct file paths surfaced in handoff
_SESSION_WORK_MAX_MESSAGES = 8  # cap operator-message excerpts surfaced
_SESSION_WORK_MESSAGE_EXCERPT_CHARS = 140

# PATCH-168a item 4 — noise paths filtered from the file-path list. Each
# entry matches as a path-component substring (forward-slash normalized
# before match) so Windows + POSIX paths both work.
_NOISE_PATH_SUBSTRINGS = (
    "/.venv/",
    "/.git/objects/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/appdata/local/temp/",  # Windows %TEMP%
    "/tmp/",                  # POSIX /tmp and any embedded /tmp/ segment
)


def _is_noise_path(file_path: str) -> bool:
    """True iff the path looks like temp/scratch/cache noise that
    shouldn't surface in the session-work narrative."""
    if not file_path:
        return True
    normalized = file_path.replace("\\", "/").lower()
    return any(noise in normalized for noise in _NOISE_PATH_SUBSTRINGS)


def _truncate_at_word_boundary(text: str, cap: int) -> str:
    """Return `text` truncated to ≤cap chars, cutting at the last
    whitespace before the cap rather than mid-character. Appends `...`
    when truncation occurred. PATCH-168a item 2."""
    if len(text) <= cap:
        return text
    head = text[:cap]
    # Find the last whitespace position in head.
    last_ws = head.rfind(" ")
    if last_ws == -1 or last_ws < cap // 2:
        # Whole `head` is a single long word (no whitespace, or whitespace
        # too early to be useful). Fall through to char-truncate to avoid
        # producing a near-empty excerpt.
        return head.rstrip() + "..."
    return head[:last_ws].rstrip() + "..."


def compute_debrief_counts(observations: Iterable[dict]) -> DebriefCounts:
    """Aggregate event counts from a session's observation log.

    Maps observation event types into the Caught / Shaped / Quiet / Session
    work categories per the advisor §3.2 spec + PATCH-168 work narrative.
    Unknown event types are silently ignored (forward-compat — adding a new
    event type doesn't break the debrief).
    """
    counts = DebriefCounts()
    event_counts: Counter[str] = Counter()
    distinct_files: list[str] = []
    seen_files: set[str] = set()
    operator_messages: list[str] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        event = obs.get("event") or ""
        if event:
            event_counts[event] += 1

        # PATCH-168 — capture session-work signals as we iterate so we can
        # render a real activity narrative in the handoff.
        details = obs.get("details") or {}
        if not isinstance(details, dict):
            details = {}

        if event in ("file_edited", "file_written", "file_created"):
            file_path = details.get("file_path")
            if isinstance(file_path, str) and file_path:
                # PATCH-168a item 4 — filter temp/cache/scratch paths.
                if _is_noise_path(file_path):
                    pass
                elif file_path not in seen_files:
                    seen_files.add(file_path)
                    distinct_files.append(file_path)

        if event == "operator_message":
            prompt = details.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                operator_messages.append(prompt)

    # Caught — actively-prevented or actively-nudged
    counts.innate_blocks = (
        event_counts.get("innate_blocked", 0)
        + event_counts.get("innate_enforcer_blocked", 0)
    )
    counts.voice_nudges = (
        event_counts.get("voice_keeper_nudged", 0)
        + event_counts.get("voice_drift_nudged", 0)
    )

    # Shaped — counterfactual-labeled prior activations
    counts.file_location_routes = (
        event_counts.get("file_location_prior_applied", 0)
        + event_counts.get("downloads_default_applied", 0)
    )
    counts.memory_schema_writes = event_counts.get("memory_schema_conforming_write", 0)
    counts.permission_gates = event_counts.get("permission_gate_enforced", 0)
    counts.safety_confirmations = event_counts.get("safety_confirmation_required", 0)

    # Quiet — pillar activity that fired silently
    counts.pattern_observer_events = (
        event_counts.get("pattern_accumulated", 0)
        + event_counts.get("pattern_proposal_emitted", 0)
    )
    counts.drift_signals = event_counts.get("drift_recurrence", 0)
    counts.recall_hits = event_counts.get("recall_silo_hit", 0)
    counts.stress_cascade_events = event_counts.get("stress_cascade_fired", 0)

    # Session work — PATCH-168 narrative inputs
    counts.file_edit_count = event_counts.get("file_edited", 0)
    counts.file_write_count = event_counts.get("file_written", 0)
    counts.file_create_count = event_counts.get("file_created", 0)
    counts.distinct_files_touched = distinct_files
    counts.operator_messages = operator_messages

    return counts


def _render_caught(counts: DebriefCounts) -> list[str]:
    lines: list[str] = []
    if counts.innate_blocks > 0:
        plural = "command" if counts.innate_blocks == 1 else "commands"
        lines.append(
            f"  - innate-enforcer blocked {counts.innate_blocks} destructive {plural}"
        )
    if counts.voice_nudges > 0:
        plural = "instance" if counts.voice_nudges == 1 else "instances"
        lines.append(
            f"  - voice-keeper nudged {counts.voice_nudges} {plural} of off-voice phrasing"
        )
    if not lines:
        lines.append("  - no destructive commands or voice drift this session")
    return lines


def _render_shaped(counts: DebriefCounts) -> list[str]:
    lines: list[str] = []
    if counts.file_location_routes > 0:
        plural = "write" if counts.file_location_routes == 1 else "writes"
        lines.append(
            f"  - file-location prior: {counts.file_location_routes} "
            f"{plural} routed to operator-default location"
        )
    if counts.memory_schema_writes > 0:
        plural = "write" if counts.memory_schema_writes == 1 else "writes"
        lines.append(
            f"  - memory schema: {counts.memory_schema_writes} "
            f"{plural} followed canon+cwd structure (operator-imprinted)"
        )
    if counts.permission_gates > 0:
        plural = "operation" if counts.permission_gates == 1 else "operations"
        lines.append(
            f"  - permission-gating: {counts.permission_gates} "
            f"sensitive {plural} required operator confirmation"
        )
    if counts.safety_confirmations > 0:
        plural = "confirmation" if counts.safety_confirmations == 1 else "confirmations"
        lines.append(
            f"  - safety prior: {counts.safety_confirmations} "
            f"destructive {plural} surfaced before action"
        )
    if not lines:
        lines.append("  - no shaped operations this session")
    return lines


def _render_quiet(counts: DebriefCounts) -> list[str]:
    lines: list[str] = []
    # Surface the silent pillars honestly. The "Quiet" section is value-
    # affirming when nothing fires (confirms autonomic-silent intent) and
    # value-revealing when something does (operator sees the work).
    if counts.pattern_observer_events == 0:
        lines.append("  - pattern-observer: no new pattern accumulation")
    else:
        plural = "event" if counts.pattern_observer_events == 1 else "events"
        lines.append(
            f"  - pattern-observer: {counts.pattern_observer_events} "
            f"{plural} accumulated"
        )

    if counts.drift_signals == 0:
        lines.append("  - drift-detection: no drift signals")
    else:
        plural = "signal" if counts.drift_signals == 1 else "signals"
        lines.append(
            f"  - drift-detection: {counts.drift_signals} drift {plural} surfaced"
        )

    if counts.recall_hits == 0:
        lines.append("  - recall-silos: 0 hits")
    else:
        plural = "hit" if counts.recall_hits == 1 else "hits"
        lines.append(f"  - recall-silos: {counts.recall_hits} {plural}")

    if counts.stress_cascade_events == 0:
        lines.append("  - stress-response: no cascade events")
    else:
        plural = "event" if counts.stress_cascade_events == 1 else "events"
        lines.append(
            f"  - stress-response: {counts.stress_cascade_events} cascade {plural}"
        )

    return lines


def _render_session_work(counts: DebriefCounts) -> list[str]:
    """PATCH-168 — Session work section: surface file activity + operator
    messages so the handoff orients the next session on what was done.

    Format:
        Session work:
          - N file operations across M distinct files (edits/writes/creates)
          - files touched:
              <file_path>
              ... (further files truncated past _SESSION_WORK_MAX_FILES)
          - operator messages:
              "<excerpt>"
              ... (further messages truncated past _SESSION_WORK_MAX_MESSAGES)
    """
    lines: list[str] = []
    edit_total = counts.file_edit_count + counts.file_write_count + counts.file_create_count
    distinct = len(counts.distinct_files_touched)
    if edit_total > 0:
        ops_word = "operation" if edit_total == 1 else "operations"
        files_word = "file" if distinct == 1 else "files"
        if distinct > 0:
            lines.append(
                f"  - {edit_total} file {ops_word} across "
                f"{distinct} distinct {files_word} "
                f"(edits={counts.file_edit_count}, "
                f"writes={counts.file_write_count}, "
                f"creates={counts.file_create_count})"
            )
        else:
            lines.append(
                f"  - {edit_total} file {ops_word} "
                f"(edits={counts.file_edit_count}, "
                f"writes={counts.file_write_count}, "
                f"creates={counts.file_create_count})"
            )

    if counts.distinct_files_touched:
        lines.append("  - files touched:")
        # PATCH-168a item 1 — newest-first ordering. distinct_files_touched
        # is appended in encounter order (oldest-first); reverse before
        # capping so the cap retains the MOST RECENT files. Long sessions
        # touching 100+ files would otherwise hide the most-relevant edits.
        recent_first = list(reversed(counts.distinct_files_touched))
        for fp in recent_first[:_SESSION_WORK_MAX_FILES]:
            lines.append(f"      {fp}")
        overflow = len(recent_first) - _SESSION_WORK_MAX_FILES
        if overflow > 0:
            file_word = "file" if overflow == 1 else "files"
            lines.append(f"      ... ({overflow} additional {file_word})")

    if counts.operator_messages:
        msg_word = "message" if len(counts.operator_messages) == 1 else "messages"
        lines.append(f"  - operator {msg_word}:")
        # Phase 3.3d (2026-05-24): newest-last ordering for operator messages.
        # Pre-fix, slice took FIRST N — handoff captured session-start
        # context but dropped end-of-session work. Operator's framing:
        # next session needs information from the END of work, not
        # truncated from the beginning. Mirrors files-touched fix in
        # PATCH-168a item 1 (newest-first there because file relevance
        # is recency-weighted; here we want chronological reading order
        # so render OLDEST-of-the-N-most-recent first, latest message last).
        recent_messages = counts.operator_messages[-_SESSION_WORK_MAX_MESSAGES:]
        overflow = len(counts.operator_messages) - _SESSION_WORK_MAX_MESSAGES
        if overflow > 0:
            msg_overflow_word = "message" if overflow == 1 else "messages"
            lines.append(f"      ... ({overflow} earlier {msg_overflow_word} omitted)")
        for prompt in recent_messages:
            excerpt = prompt.strip().replace("\n", " ")
            # PATCH-168a item 2 — truncate at word boundary, not mid-character.
            excerpt = _truncate_at_word_boundary(
                excerpt, _SESSION_WORK_MESSAGE_EXCERPT_CHARS
            )
            lines.append(f'      "{excerpt}"')

    # PATCH-168a item 3 — return empty list when no work was observed.
    # format_debrief() then omits the section header entirely (the empty
    # block was noise; the absence of the header signals "nothing to report").
    return lines


def format_debrief(counts: DebriefCounts) -> str:
    """Render the full session-end debrief block.

    Returns the multi-line string ready to emit as additionalContext.
    Empty string if all sections would be empty (don't emit a hollow block).
    """
    caught_lines = _render_caught(counts)
    shaped_lines = _render_shaped(counts)
    quiet_lines = _render_quiet(counts)
    session_work_lines = _render_session_work(counts)

    lines: list[str] = [
        _BANNER_DIVIDER,
        "Allostat session summary",
        "",
    ]
    # PATCH-168a item 3 — omit the Session work header + section when no
    # work was observed. An empty block with header is noise; absence
    # is a clearer signal of "nothing to report."
    if session_work_lines:
        lines.append("Session work:")
        lines.extend(session_work_lines)
        lines.append("")
    lines.extend([
        "Caught:",
        *caught_lines,
        "",
        "Shaped:",
        *shaped_lines,
        "",
        "Quiet:",
        *quiet_lines,
        "",
        "For full detail: /allostat-why-fired",
        _BANNER_DIVIDER,
    ])
    return "\n".join(lines)


def build_session_end_debrief(state_dir: Path) -> str | None:
    """Top-level entry point — read observations + render block. Returns
    None on disable / no state dir / read failure (don't crash the Stop hook).
    """
    if not is_enabled():
        return None
    try:
        # Lazy import: local_state module is on sys.path when the Stop hook
        # invokes this function; importing at module load would break unit
        # tests that exercise format_debrief without the wrapper installed.
        from local_state import read_recent_observations  # noqa: E402

        observations = read_recent_observations(
            state_dir, window=_SESSION_OBSERVATION_WINDOW
        )
    except Exception:
        return None

    counts = compute_debrief_counts(observations)
    return format_debrief(counts)
