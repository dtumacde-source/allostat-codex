"""Handoff watchdog — wrapper-side enforcement layer for the A3 protocol.

A3 (2026-05-24) Fix Now memory-architecture refactor: Claude authors
rolling handoffs to the project-rooted path `resolve_memory_root()` returns
per the protocol shipped in `data/appendix_seed/claude_handoff_protocol.md`.
The watchdog ensures Claude doesn't silently drift off-protocol.

How it works:

1. Stop hook fires `check_and_set_reminder_state()` on every fire (debounced
   upstream by _is_repeat_stop for the heavy dispatch; the watchdog check
   runs cheap every Stop boundary).
2. UserPromptSubmit hook fires `consume_reminder_if_due()` on every prompt
   and injects escalation-level-appropriate text into additionalContext
   when a reminder is queued.

Anti-pattern detection (R5): a handoff file existing isn't enough — if
content is <500 bytes OR all sections empty, treat as a no-write. Prevents
Claude from satisfying the watchdog by writing an empty skeleton just to
update mtime.

Escalation (R4): the cascade-prevention requirement. Reminder level
escalates 0 → 1 (soft) → 2 (strong) → 3 (stub injection). Resets when
Claude writes a non-junk handoff. Without escalation, watchdog would fire
identical reminder every Stop and operator would disable it via env var,
collapsing A3 back to "Claude reliably follows protocol" — exactly what
we were trying NOT to depend on.

Env vars:
- `ALLOSTAT_HANDOFF_WATCHDOG=0` — disabled entirely
- `ALLOSTAT_HANDOFF_WATCHDOG_TURNS=5` — turn-count threshold
- `ALLOSTAT_HANDOFF_WATCHDOG_TOKENS=50000` — token-count threshold

Privacy invariant: watchdog is wrapper-side only. No new server payload,
no new observation content. State file is local-disk only.

State file: `<state_dir>/state/handoff_watchdog.json`.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# Per-session state files: `state/handoff_watchdog_<session_id>.json`. One file
# per session so concurrent sessions in the SAME project directory never stomp
# each other (pre-2026-06-20 a single shared file keyed to one session_id meant
# concurrent same-cwd sessions reset each other and the watchdog never fired).
_STATE_FILE_STEM = "handoff_watchdog"
# Per-session state files older than this are pruned (one accrues per session).
_DEFAULT_STATE_MAX_AGE_DAYS = 14

# Anti-pattern thresholds (R5 — advisor refinement).
# M3 byte threshold lowered 500 → 200 (S1 closure 2026-05-26). 500 was
# over-conservative; legitimate terse handoffs (~250 bytes with two real
# sections) were borderline. 200 gives more headroom; anti-pattern check
# still requires 2+ non-empty sections so size-alone-doesn't-pass.
_MIN_HANDOFF_BYTES = 200
_REQUIRED_SECTION_HEADERS = (
    "## Focus",
    "## Decisions",
    "## Memory pointers",
    "## Open threads",
)

# Instrumented-observation mode for TODO-filter + Focus-substantive
# (S1 closure item #8, advisor signoff 2026-05-26). These detectors flag
# what they WOULD reject and write `handoff_instrumented_observation`
# events to the observations log, but do NOT reject. After N=5+ session
# data accumulates, operator + advisor decide whether to flip either
# detector to actually-reject in a follow-up ship.
_TODO_LINE_RE = None  # populated lazily via re.compile to avoid import cost
_FOCUS_MIN_SUBSTANTIVE_CHARS = 80  # one full sentence floor

# Default thresholds (advisor + dev concur).
_DEFAULT_TURNS_THRESHOLD = 5
_DEFAULT_TOKENS_THRESHOLD = 50_000

# A2 (2026-06-26) — elapsed-time threshold. The turns/tokens thresholds miss a
# session that goes idle with unsaved work (few turns, low token delta, but a
# long wall-clock gap since the last handoff). 2700s = 45 min: long enough that
# a brief pause doesn't nag, short enough to catch a genuinely-idle session
# before context is lost. Env-tunable. COUPLING NOTE: elapsed only fires when
# the check actually RUNS — A1's resume_flush makes it run at SessionStart, and
# Stop already runs it, so a pure-idle-then-prompt session improves at the next
# Stop/SessionStart boundary (no background timer; that residual is documented).
_DEFAULT_ELAPSED_THRESHOLD_SEC = 2700

# Maximum escalation level. After level 3 stays at 3 (stub-inject re-fires
# every Stop until Claude writes a non-junk handoff). Public constant —
# hooks (stop / session-start / handoff_autocapture) read it directly; the
# underscore alias remains for back-compat with older callers.
MAX_ESCALATION_LEVEL = 3
_MAX_ESCALATION_LEVEL = MAX_ESCALATION_LEVEL

# PATCH-205 (2026-05-28) — overfire calibration. Once escalation hits MAX,
# the pre-PATCH-205 logic re-queued the reminder on EVERY subsequent Stop
# without a substantive write. Long focused sessions paid a false-positive
# tax: same operator-facing stub injected after every single prompt during
# a single-task autonomous run. Empirical observation: v1.4.32 ship session
# fired LEVEL 3 stub ~9 times in <2h.
#
# This cap throttles re-queues at MAX level. After the first MAX-level
# reminder, the watchdog requires this many more Stops (without a
# substantive write) before re-firing. Substantive write resets to 0 as
# always.
#
# Default 3 — operator-facing reminders surface once per ~3 Stops at MAX,
# not every Stop. Tunable via env var. Set to 0 to restore pre-PATCH-205
# every-Stop behavior.
_DEFAULT_MAX_LEVEL_REFIRE_GAP = 3


def max_level_refire_gap() -> int:
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG_MAX_REFIRE_GAP", "")
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_LEVEL_REFIRE_GAP


# Handoff-redesign (2026-06-14) — oversize soft-nudge threshold (characters).
# The protocol caps the LEAN handoff at ~1500 tokens; verbose overflow belongs
# in the sibling <session_id>.detail.md. chars/4 proxy: 1500 tok ~= 6000 chars,
# so ~2x cap ~= 12000 chars. A single generous threshold (no hysteresis): normal
# handoffs sit well under it; it trips only on genuine bloat, as a soft
# once-per-write nudge. Measured as len(text) (A5 council call — not st_size,
# which under-counts multibyte). Env-tunable.
_DEFAULT_OVERSIZE_CHARS = 12_000


def oversize_chars_threshold() -> int:
    """Configurable via ALLOSTAT_HANDOFF_WATCHDOG_OVERSIZE_CHARS. Default 12000."""
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG_OVERSIZE_CHARS", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_OVERSIZE_CHARS
        return v if v > 0 else _DEFAULT_OVERSIZE_CHARS
    except (ValueError, AttributeError):
        return _DEFAULT_OVERSIZE_CHARS


@dataclass
class WatchdogState:
    """Per-session watchdog state. Persisted to state file."""
    session_id: str = ""
    last_handoff_mtime: float = 0.0
    last_handoff_path: str = ""
    last_handoff_size: int = 0
    turns_since_last_write: int = 0
    tokens_at_last_write: int = 0
    last_seen_cumulative_tokens: int = 0
    escalation_level: int = 0
    last_reminder_turn: Optional[int] = None
    reminder_pending: bool = False
    # Visibility counters for /allostat-handoff-status (R6).
    session_handoff_count: int = 0
    session_reminder_count: int = 0
    session_response_count: int = 0
    anti_pattern_count: int = 0
    # PATCH-205 (2026-05-28) — overfire calibration. Tracks Stops since the
    # most-recent MAX-level reminder was queued. Used to throttle MAX-level
    # re-queue frequency (see max_level_refire_gap()). Zero on session
    # start; bumps each Stop while escalation == MAX + no substantive
    # write; resets to 0 on substantive write OR on every fresh MAX-level
    # queue.
    stops_since_last_max_level_queue: int = 0
    # Real root-cause fix (2026-05-28). The handoff file's mtime captured at
    # the moment the most-recent reminder was queued (0.0 if no handoff existed
    # then). The escalation path consults this: if a substantive handoff has a
    # newer mtime, it was written in response to the nag -> reset instead of
    # escalating. Makes firing a function of "handoff stale since last fire",
    # robust to the turns counter drifting past its resets during interrupt /
    # resume churn (consume_reminder_if_due fires on UserPromptSubmit without a
    # paired Stop).
    last_reminder_handoff_mtime: float = 0.0
    # Handoff-redesign (2026-06-14, council A1 reframe) — secondary soft one-shot
    # nudges, both independent of the under-write escalation above. Each is set
    # on a fresh substantive write, consumed once on the next UserPromptSubmit,
    # then cleared (re-set only on the next qualifying fresh write). No
    # escalation, no hysteresis — a fresh write happens at most every 3-5 turns,
    # so neither can per-turn nag.
    oversize_nudge_pending: bool = False      # lean handoff > ~2x cap (didn't offload enough)
    breadcrumb_nudge_pending: bool = False    # <sid>.detail.md exists but handoff never references it
    session_oversize_nudge_count: int = 0
    session_breadcrumb_nudge_count: int = 0


def disabled() -> bool:
    """True when ALLOSTAT_HANDOFF_WATCHDOG=0. Default: enabled."""
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG", "")
    return raw.strip() in ("0", "false", "no", "off")


def turns_threshold() -> int:
    """Configurable via ALLOSTAT_HANDOFF_WATCHDOG_TURNS. Default 5."""
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG_TURNS", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_TURNS_THRESHOLD
        return v if v > 0 else _DEFAULT_TURNS_THRESHOLD
    except (ValueError, AttributeError):
        return _DEFAULT_TURNS_THRESHOLD


def tokens_threshold() -> int:
    """Configurable via ALLOSTAT_HANDOFF_WATCHDOG_TOKENS. Default 50000."""
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG_TOKENS", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_TOKENS_THRESHOLD
        return v if v > 0 else _DEFAULT_TOKENS_THRESHOLD
    except (ValueError, AttributeError):
        return _DEFAULT_TOKENS_THRESHOLD


def elapsed_threshold() -> int:
    """A2 — seconds of wall-clock since the last handoff before an idle session
    with unsaved work trips. Configurable via ALLOSTAT_HANDOFF_WATCHDOG_ELAPSED_SEC.
    Default 2700 (45 min)."""
    raw = os.environ.get("ALLOSTAT_HANDOFF_WATCHDOG_ELAPSED_SEC", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_ELAPSED_THRESHOLD_SEC
        return v if v > 0 else _DEFAULT_ELAPSED_THRESHOLD_SEC
    except (ValueError, AttributeError):
        return _DEFAULT_ELAPSED_THRESHOLD_SEC


def _sanitize_session_id(session_id: str) -> str:
    """Session ids are UUIDs, but sanitize defensively so a malformed id can't
    escape the state dir or collide on path-hostile chars."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "")[:80]


def _state_file_path(state_dir: Path, session_id: str = "") -> Path:
    """Per-session state file. Empty session_id falls back to the legacy
    single-file name (back-compat for callers without an id; real hooks always
    pass one)."""
    sid = _sanitize_session_id(session_id)
    name = f"{_STATE_FILE_STEM}_{sid}.json" if sid else f"{_STATE_FILE_STEM}.json"
    return state_dir / "state" / name


def _most_recent_session_id(state_dir: Path) -> str:
    """The sanitized id of the most-recently-written per-session state file, or
    "" if none. Lets get_status_summary show the active session without the
    caller knowing the id."""
    state_subdir = state_dir / "state"
    if not state_subdir.is_dir():
        return ""
    try:
        files = list(state_subdir.glob(f"{_STATE_FILE_STEM}_*.json"))
    except OSError:
        return ""
    if not files:
        return ""
    # Stat each candidate defensively: a file can vanish between glob and
    # stat when another concurrent session's prune_stale_state runs (the
    # module explicitly supports concurrent same-cwd sessions).
    newest_name = ""
    newest_mtime = -1.0
    for p in files:
        try:
            m = p.stat().st_mtime
        except OSError:
            # Best-effort: file vanished between glob and stat (concurrent
            # session's prune) — skip it; the survivors decide the answer.
            continue
        if m > newest_mtime:
            newest_mtime = m
            newest_name = p.name
    if not newest_name:
        return ""
    return newest_name[len(_STATE_FILE_STEM) + 1:-len(".json")]


def read_state(state_dir: Path, session_id: str = "") -> WatchdogState:
    """Read this session's watchdog state. Default state on missing/error."""
    path = _state_file_path(state_dir, session_id)
    if not path.is_file():
        return WatchdogState(session_id=session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WatchdogState(session_id=session_id)
    if not isinstance(data, dict):
        return WatchdogState(session_id=session_id)
    # Filter to fields the dataclass knows about AND whose value matches the
    # field's expected type (resilient to schema drift and corrupt/hand-edited
    # files — dataclasses don't type-check at construction, and e.g. an int
    # session_id would crash _sanitize_session_id on the next write_state).
    _defaults = WatchdogState()
    filtered: dict = {}
    for k, v in data.items():
        if k not in WatchdogState.__dataclass_fields__:
            continue
        default_v = getattr(_defaults, k)
        if default_v is None:
            filtered[k] = v  # Optional field — accept as-is
        elif isinstance(default_v, bool):  # bool before int: bool is an int subclass
            if isinstance(v, bool):
                filtered[k] = v
        elif isinstance(default_v, float):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                filtered[k] = float(v)
        elif isinstance(default_v, int):
            if isinstance(v, int) and not isinstance(v, bool):
                filtered[k] = v
        elif isinstance(v, type(default_v)):
            filtered[k] = v
    try:
        st = WatchdogState(**filtered)
    except (TypeError, ValueError):
        return WatchdogState(session_id=session_id)
    # Per-session file: the requested id is authoritative; adopt it if the
    # stored value is blank so writes land back in the same file.
    if session_id and not st.session_id:
        st.session_id = session_id
    return st


def write_state(state_dir: Path, state: WatchdogState) -> None:
    """Persist watchdog state to this session's file. Best-effort.

    H-17: routes through the shared atomic-write primitive (temp file +
    os.replace) instead of a bare path.write_text. A crash mid-write leaves the
    prior state file intact rather than truncated — a truncated/partial file
    fails json.loads and read_state silently returns a zeroed default
    WatchdogState, dropping escalation/turn counters. os.replace is atomic on
    POSIX and Windows, so it also protects concurrent same-session writers from
    reading a half-written file."""
    path = _state_file_path(state_dir, state.session_id)
    try:
        from local_state import atomic_write_text  # noqa: E402
        atomic_write_text(path, json.dumps(asdict(state), indent=2), encoding="utf-8")
    except OSError:
        pass


def prune_stale_state(state_dir: Path, max_age_days: int = _DEFAULT_STATE_MAX_AGE_DAYS) -> list[str]:
    """Remove per-session state files not modified within max_age_days, so the
    state dir doesn't accumulate one file per historical session. Best-effort;
    returns the filenames removed."""
    removed: list[str] = []
    state_subdir = state_dir / "state"
    if not state_subdir.is_dir():
        return removed
    cutoff = time.time() - max_age_days * 86400
    try:
        candidates = list(state_subdir.glob(f"{_STATE_FILE_STEM}_*.json"))
    except OSError:
        return removed
    for p in candidates:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p.name)
        except OSError:
            # Best-effort cleanup: skip any file we can't stat or unlink.
            continue
    return removed


def _resolve_canonical_handoff_path(project_root: Path, session_id: str) -> Path:
    """Return the expected location of Claude's rolling handoff for this session."""
    from session_handoff import resolve_canonical_handoff_dir  # noqa: E402
    return resolve_canonical_handoff_dir(project_root) / f"{session_id}.md"


def _passes_anti_pattern_check(handoff_path: Path) -> bool:
    """R5 anti-pattern detection (advisor refinement).

    A handoff file existing isn't enough. Return True only when the file
    is substantive:
      - File size >= _MIN_HANDOFF_BYTES (200 bytes — lowered from 500
        in S1 closure 2026-05-26 after empirical recalibration); AND
      - Has body content under at least 2 of the 4 required section headers
        (skeleton-only files with all sections empty fail)

    Returns False for missing files, read errors, or content that fails
    either gate. Caller treats False as "no real handoff written".

    Note: TODO-filter + Focus-substantive checks are NOT applied here.
    They run via `_detect_instrumented_signals` in observation-only mode
    until N=5+ session data informs whether to flip to rejection.
    """
    try:
        stat = handoff_path.stat()
    except OSError:
        return False
    if stat.st_size < _MIN_HANDOFF_BYTES:
        return False
    try:
        # H-19: read BOM-tolerantly (utf-8-sig strips a leading BOM; plain
        # utf-8 does not). Mirrors memory_reader.py / handoff_discoverer.py /
        # session_handoff.py, which all read handoffs as utf-8-sig.
        text = handoff_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False

    # A3 caveat #3 (2026-06-26): an autocaptured transcript-floor handoff is
    # continuity INSURANCE, not "protocol satisfied". It must NEVER count as a
    # substantive write, so the watchdog keeps expecting a human-authored
    # replacement. (The marker is written by handoff_autocapture into the
    # leading frontmatter; match it there, not anywhere in the body.)
    #
    # H-19: strip any leading BOM/whitespace before the startswith('---') check.
    # A BOM re-save (or a stray leading blank line) otherwise defeated the plain
    # startswith, so an untouched floor handoff scored as substantive and the
    # watchdog reset escalation and stopped nagging. utf-8-sig above handles the
    # common BOM case; lstrip covers pre-frontmatter whitespace too.
    marker_text = text.lstrip("﻿").lstrip()
    if marker_text.startswith("---"):
        fm_end = marker_text.find("\n---", 3)
        frontmatter = marker_text[:fm_end] if fm_end > 0 else marker_text[:400]
        if "autocaptured: true" in frontmatter:
            return False

    # Count non-empty sections. A section is "non-empty" if there's at least
    # one non-blank, non-"(none)" line between its header and the next ## header
    # (or end of file).
    non_empty_section_count = 0
    for header in _REQUIRED_SECTION_HEADERS:
        start = text.find(header)
        if start < 0:
            continue
        # Find the next ## header AFTER this one (any heading, not just
        # required ones — Claude may add structure).
        section_body_start = start + len(header)
        next_h2 = text.find("\n## ", section_body_start)
        section_body = (
            text[section_body_start:next_h2]
            if next_h2 > 0
            else text[section_body_start:]
        )
        # Strip and check whether anything substantive sits in this section.
        body_stripped = section_body.strip()
        if not body_stripped:
            continue
        # Skip "(none)" placeholder — that's protocol-correct empty marker
        # but doesn't count as substantive content for the anti-pattern check.
        # However, if 2+ sections are "(none)", that's fine — Claude is being
        # honest. We require at least 2 sections to have REAL content, not
        # all 4 to be "(none)".
        substantive_lines = [
            ln for ln in body_stripped.splitlines()
            if ln.strip() and ln.strip() != "(none)"
        ]
        if substantive_lines:
            non_empty_section_count += 1
    return non_empty_section_count >= 2


def _detect_instrumented_signals(handoff_path: Path) -> dict:
    """S1 closure item #8 (2026-05-26): observation-only detectors.

    Surfaces what TODO-filter + Focus-substantive WOULD reject without
    actually rejecting. Caller appends a `handoff_instrumented_observation`
    event when any signal fires; the watchdog otherwise treats the handoff
    per `_passes_anti_pattern_check` alone.

    Returns:
      {
        "todo_filter_would_reject": bool,  # all sections' bodies are TODO-only
        "focus_substantive_would_reject": bool,  # Focus body < 80 chars substantive
        "samples": {
          "first_todo_section": "<section header>" | None,
          "focus_substantive_chars": int,
        },
      }
    """
    global _TODO_LINE_RE
    if _TODO_LINE_RE is None:
        import re
        _TODO_LINE_RE = re.compile(
            r"^\s*[-*]?\s*TODO[:\s]", flags=re.IGNORECASE,
        )

    result = {
        "todo_filter_would_reject": False,
        "focus_substantive_would_reject": False,
        "samples": {
            "first_todo_section": None,
            "focus_substantive_chars": 0,
        },
    }
    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    # TODO-filter signal: a STUB-SHAPED handoff is one where most/all
    # present sections are entirely TODO-lines (the level-3 stub-inject
    # pattern). A single legitimate "TODO: rerun benchmarks" line in the
    # Queued section of an otherwise-substantive handoff is NOT stub-shaped
    # and must NOT trigger the filter (F-1 fix, 2026-05-27).
    #
    # Track found_count (sections actually present with substantive body)
    # alongside todo_only_sections. Only flag when len(todo_only_sections)
    # >= 2 AND equals found_count (i.e., every present section is TODO-only).
    todo_only_sections: list[str] = []
    found_count = 0
    for header in _REQUIRED_SECTION_HEADERS:
        start = text.find(header)
        if start < 0:
            continue
        section_body_start = start + len(header)
        next_h2 = text.find("\n## ", section_body_start)
        section_body = (
            text[section_body_start:next_h2]
            if next_h2 > 0
            else text[section_body_start:]
        )
        body_lines = [
            ln for ln in section_body.strip().splitlines()
            if ln.strip() and ln.strip() != "(none)"
        ]
        if not body_lines:
            continue
        found_count += 1
        if all(_TODO_LINE_RE.match(ln) for ln in body_lines):
            todo_only_sections.append(header)
    if len(todo_only_sections) >= 2 and len(todo_only_sections) == found_count:
        result["todo_filter_would_reject"] = True
        result["samples"]["first_todo_section"] = todo_only_sections[0]

    # Focus-substantive signal: Focus section body must clear the
    # _FOCUS_MIN_SUBSTANTIVE_CHARS floor (after stripping placeholder).
    focus_start = text.find("## Focus")
    if focus_start >= 0:
        body_start = focus_start + len("## Focus")
        next_h2 = text.find("\n## ", body_start)
        focus_body = (
            text[body_start:next_h2] if next_h2 > 0 else text[body_start:]
        )
        substantive = "\n".join(
            ln for ln in focus_body.strip().splitlines()
            if ln.strip() and ln.strip() != "(none)"
        )
        char_count = len(substantive)
        result["samples"]["focus_substantive_chars"] = char_count
        if 0 < char_count < _FOCUS_MIN_SUBSTANTIVE_CHARS:
            result["focus_substantive_would_reject"] = True

    return result


def _evaluate_lean_handoff_quality(state: WatchdogState, handoff_path: Path) -> None:
    """Handoff-redesign (council 2026-06-14): on a fresh substantive write, set
    the secondary soft one-shot nudges. Reads the handoff text ONCE.

    (1) Oversize: len(text) over the threshold → the lean handoff didn't offload
        enough verbose overflow (mild bloat). A5: len(text), not st_size.
    (2) Missing breadcrumb: a `<sid>.detail.md` sibling exists but the handoff
        text never references `.detail.md` → orphan-detail risk (a resuming
        session reads only the lean handoff and would never learn the sibling
        exists). Higher priority than oversize (orphan > bloat).

    SECONDARY backstop only. The continuity guarantee is the protocol
    write-template (fixed sections never offload); the watchdog is blind to the
    sibling's contents and cannot verify a section wasn't gutted.
    """
    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if len(text) > oversize_chars_threshold():
        state.oversize_nudge_pending = True
    sibling = handoff_path.with_name(handoff_path.stem + ".detail.md")
    if sibling.exists() and ".detail.md" not in text:
        state.breadcrumb_nudge_pending = True


def _credit_substantive_write(
    state: WatchdogState,
    handoff_path: Path,
    mtime: float,
    size: int,
) -> None:
    """Account for ONE substantive handoff write and re-arm the watchdog.

    Every path that decides "the agent wrote a real handoff" must go through
    here. Two of them exist (the is_fresh branch and the wrote-since-last-
    reminder gate) and the second-cycle defect was one of them doing four of
    the five required updates.

    The load-bearing line is the LAST one. `last_reminder_handoff_mtime` is
    the gate's baseline, and before 2026-08-03 its only setter lived inside
    the reminder-queue branch — so neither reset path advanced it. Once any
    handoff newer than the last reminder existed, `mtime > baseline` stayed
    true for the remainder of the session: every later breach took the reset
    path, re-credited the SAME write, and never nagged. D2 closed the first
    fire; this closes the one after it. Field shape it produced: 662k-token
    sessions with 15 credited writes and zero reminders.

    Advancing the baseline to the mtime just credited makes the gate mean
    what its name says — "a write happened since the last reminder AND has
    not already been counted" — so a stale handoff escalates on the next
    breach while a genuine response-write still resets (the drift case the
    gate exists for).
    """
    prior_escalation = state.escalation_level
    state.last_handoff_mtime = mtime
    state.last_handoff_path = str(handoff_path)
    state.last_handoff_size = size
    state.turns_since_last_write = 0
    state.tokens_at_last_write = state.last_seen_cumulative_tokens
    state.escalation_level = 0
    state.reminder_pending = False
    state.stops_since_last_max_level_queue = 0
    state.session_handoff_count += 1
    if prior_escalation > 0:
        state.session_response_count += 1
    # Consume the credit: this write can never be counted a second time.
    state.last_reminder_handoff_mtime = mtime


def check_and_set_reminder_state(
    state_dir: Path,
    project_root: Path,
    session_id: Optional[str],
) -> dict:
    """Called from Stop hook. Returns a dict summarizing what happened.

    Logic:
    1. Read current state.
    2. If session_id changed → reset per-session counters (new session).
    3. Check canonical handoff file existence + mtime + anti-pattern.
    4. If handoff written substantively since last check → reset counters,
       decrement escalation, increment success counters.
    5. If not → check thresholds; if exceeded, set reminder_pending and
       escalate.
    6. Persist state.

    Returns: {"action": <str>, "escalation_level": int, ...} for observability.
    """
    if disabled() or not session_id:
        return {"action": "skipped", "reason": "disabled_or_no_session_id"}

    state = read_state(state_dir, session_id)
    result = {"action": "no_change", "escalation_level": state.escalation_level}

    # Detect session change. Distinguish:
    #   (a) Initial adoption: state.session_id == "" (consume_reminder_if_due
    #       ran first, accumulated turns, but didn't know session_id yet).
    #       Adopt the incoming session_id WITHOUT resetting counters — the
    #       turns/tokens accumulated belong to this session.
    #   (b) Real session change: state.session_id non-empty AND different.
    #       Wipe counters; new session starts fresh.
    if state.session_id == "":
        state.session_id = session_id
    elif state.session_id != session_id:
        state = WatchdogState(session_id=session_id)
        result["action"] = "session_changed_reset"

    # Check for handoff file.
    handoff_path = _resolve_canonical_handoff_path(project_root, session_id)
    handoff_exists = handoff_path.is_file()

    # Every later use of mtime/size is guarded by handoff_exists, but keep the
    # invariant explicit rather than relying on short-circuit coupling.
    mtime = 0.0
    size = 0
    if handoff_exists:
        try:
            mtime = handoff_path.stat().st_mtime
            size = handoff_path.stat().st_size
        except OSError:
            mtime = 0.0
            size = 0

        # Has the file been touched since our last check?
        is_fresh = mtime > state.last_handoff_mtime
        if is_fresh:
            passes_check = _passes_anti_pattern_check(handoff_path)
            # S1 closure item #8 — instrumented-observation mode.
            # Surfaces what TODO-filter + Focus-substantive WOULD reject
            # without acting on it. Operator + advisor tune thresholds
            # from accumulated data before flipping either detector to
            # actually-reject.
            signals = _detect_instrumented_signals(handoff_path)
            if signals["todo_filter_would_reject"] or signals["focus_substantive_would_reject"]:
                try:
                    from local_state import append_observation  # noqa: E402
                    append_observation(state_dir, "handoff_instrumented_observation", details={
                        "session_id": session_id,
                        "handoff_path": str(handoff_path),
                        "todo_filter_would_reject": signals["todo_filter_would_reject"],
                        "focus_substantive_would_reject": signals["focus_substantive_would_reject"],
                        "first_todo_section": signals["samples"]["first_todo_section"],
                        "focus_substantive_chars": signals["samples"]["focus_substantive_chars"],
                        "passed_anti_pattern_check": passes_check,
                    })
                except Exception:
                    # Best-effort: instrumented-observation logging must
                    # never block the watchdog. H3 audit-gate compliance —
                    # this is data-collection only; silent on failure.
                    pass
            if passes_check:
                # Real handoff — reset counters, decrement escalation.
                _credit_substantive_write(state, handoff_path, mtime, size)
                result["action"] = "handoff_written_substantive"
                result["escalation_level"] = 0
                _evaluate_lean_handoff_quality(state, handoff_path)
            else:
                # Anti-pattern hit — junk handoff. Treat as no-write; bump
                # escalation if thresholds also exceeded.
                state.anti_pattern_count += 1
                state.last_handoff_mtime = mtime  # remember mtime so we don't repeatedly
                                                  # flag the same junk file
                # Escalation only happens via the threshold path below; just
                # tag the result here so observability logs it.
                result["action"] = "handoff_anti_pattern_detected"

    # Threshold check — only relevant if we didn't just reset above.
    if result["action"] in ("no_change", "session_changed_reset", "handoff_anti_pattern_detected"):
        over_turn = state.turns_since_last_write >= turns_threshold()
        token_delta = max(
            0, state.last_seen_cumulative_tokens - state.tokens_at_last_write
        )
        over_tokens = token_delta >= tokens_threshold()
        # A2 (2026-06-26) — elapsed-time trigger for idle sessions. Fires only
        # when there is unsaved work (turns or tokens accrued since the last
        # write) AND a known last-handoff mtime is now older than the elapsed
        # threshold. has_unsaved guards against nagging a fully-saved idle
        # session; the last_handoff_mtime guard means a session with no handoff
        # yet relies on the turns/tokens triggers (no mtime baseline to measure
        # idle against).
        has_unsaved = state.turns_since_last_write > 0 or token_delta > 0
        over_elapsed = (
            has_unsaved
            and bool(state.last_handoff_mtime)
            and (time.time() - state.last_handoff_mtime) >= elapsed_threshold()
        )
        if over_turn or over_tokens or over_elapsed:
            # Real root-cause fix (2026-05-28; supersedes the PATCH-205
            # throttle band-aid as the primary mechanism). The escalation
            # driver above is the turns/token counter, which drifts past its
            # resets during interrupt/resume churn (consume_reminder_if_due
            # fires on UserPromptSubmit without a paired Stop) and can be
            # missed by the strict is_fresh mtime check. Before escalating,
            # consult the source of truth — the handoff file. If a substantive
            # handoff has been written/updated since the LAST reminder fired,
            # the protocol is being followed: reset instead of re-nagging.
            # Firing is now "handoff stale since last fire", not "N turns".
            # D2 (2026-08-03): `session_reminder_count > 0` is the whole fix.
            # The mtime baseline defaults to 0.0 and its ONLY setter is
            # inside the escalate branch this gate guards — so any session
            # with an existing valid handoff satisfied `mtime > 0.0` on
            # every threshold crossing: full reset, session_handoff_count
            # += 1 (the phantom 65), and never a reminder. Measured: 2 fires
            # in 499 sessions. Drift protection has nothing to protect
            # before the first reminder has fired — and the fired-yet signal
            # must be the REMINDER COUNT, not the baseline itself: a
            # reminder fired before any handoff existed legitimately leaves
            # the baseline at 0.0, and the response-write that follows must
            # still be recognized (test_handoff_written_since_reminder_
            # halts_escalation pins that case).
            if (
                handoff_exists
                and state.session_reminder_count > 0
                and mtime > state.last_reminder_handoff_mtime
                and _passes_anti_pattern_check(handoff_path)
            ):
                _credit_substantive_write(state, handoff_path, mtime, size)
                result["action"] = "handoff_fresh_since_last_reminder"
                result["escalation_level"] = 0
                _evaluate_lean_handoff_quality(state, handoff_path)
                write_state(state_dir, state)
                return result
            # Escalate one level (capped at MAX).
            new_level = min(state.escalation_level + 1, _MAX_ESCALATION_LEVEL)
            # PATCH-205 overfire calibration: at MAX level, throttle the
            # re-queue cadence. Without this gate, the pre-PATCH-205 logic
            # re-queued the level-3 stub on EVERY subsequent Stop without
            # a substantive write — operator-facing nag tax on long focused
            # sessions. The gate keeps the cap from regressing to mid-level
            # (which still re-queue normally) while preventing the spam.
            should_queue = (
                new_level != state.escalation_level
                or not state.reminder_pending
            )
            already_at_max = (
                state.escalation_level == _MAX_ESCALATION_LEVEL
                and new_level == _MAX_ESCALATION_LEVEL
            )
            if already_at_max:
                state.stops_since_last_max_level_queue += 1
                # Throttle: re-queue only after the configured Stop-gap has
                # accumulated since the last MAX-level queue.
                if state.stops_since_last_max_level_queue <= max_level_refire_gap():
                    should_queue = False
            if should_queue:
                state.escalation_level = new_level
                state.reminder_pending = True
                state.last_reminder_turn = state.turns_since_last_write
                # Baseline for the wrote-since-last-fire gate above: capture the
                # handoff's mtime (0.0 if absent) at the moment we nag, so the
                # next cycle can tell whether Claude wrote in response.
                state.last_reminder_handoff_mtime = mtime if handoff_exists else 0.0
                state.session_reminder_count += 1
                if new_level == _MAX_ESCALATION_LEVEL:
                    state.stops_since_last_max_level_queue = 0
                result["action"] = "reminder_queued"
                result["escalation_level"] = new_level
                result["over_turn"] = over_turn
                result["over_tokens"] = over_tokens
                result["over_elapsed"] = over_elapsed
                result["token_delta"] = token_delta
            elif already_at_max:
                result["action"] = "max_level_refire_throttled"
                result["escalation_level"] = _MAX_ESCALATION_LEVEL
                result["stops_since_last_max_level_queue"] = state.stops_since_last_max_level_queue

    write_state(state_dir, state)
    return result


# Reminder text per escalation level (R4 advisor refinement — soft → strong → stub).

_REMINDER_LEVEL_1 = """\
[Allostat handoff watchdog — protocol reminder]

Your rolling handoff at `{handoff_path}`
is overdue per the MCS protocol. Write or update it now before continuing.

Cadence reminder: every 3-5 turns of substantive work, plus metabolism pulses
and idle. See `claude_handoff_protocol.md` in your additionalContext for full spec.
"""

_REMINDER_LEVEL_2 = """\
[Allostat handoff watchdog — PROTOCOL VIOLATION, write now]

You've drifted past the rolling-handoff threshold a second time. The watchdog
escalated to level 2.

WRITE THE HANDOFF NOW at:
  `{handoff_path}`

Use the structure from `claude_handoff_protocol.md`:
- ## Focus
- ## Decisions
- ## Memory pointers
- ## Open threads
- ## Blocked
- ## Queued

Overwrite in place. One file per session. Do not respond to operator's prompt
until the handoff is written — continuity gap risk compounds every turn you skip.
"""

_REMINDER_LEVEL_3 = """\
[Allostat handoff watchdog — LEVEL 3 STUB INJECTION]

Watchdog has fired three times without a substantive handoff write. Filling
the stub below and writing it counts as the handoff. Edit as needed, then
write to `{handoff_path}` BEFORE
your response to the operator.

```markdown
# <session_id>

_Rolling Claude-authored handoff. Last update: <ISO timestamp UTC>._

## Focus
TODO: one paragraph or 2-3 bullets describing what this session is about.

## Decisions
TODO: numbered list of locked calls made this session.

## Memory pointers
TODO: bullet list of files / paths future-you should read to resume.

## Open threads
TODO: each entry: what's open, what blocks closure, where to pick up.

## Blocked
(none)

## Queued
TODO: brief list of items that came up but aren't being done this session.
```

Anti-pattern detection is active — a file written with all sections still
showing TODO or all sections empty will fail the substantive-content check
and the watchdog will re-fire at level 3 next turn.
"""


_OVERSIZE_NUDGE = """\
[Allostat handoff watchdog — handoff is large]

Your last rolling handoff exceeded ~2x the ~1500-token cap. Keep all six fixed
sections (Focus / Decisions / Memory pointers / Open threads / Blocked / Queued)
as one-liners, and move only the verbose overflow — command logs, full
verification dumps, long rationale — to the sibling file:

  `<session_id>.detail.md`   (same handoffs/ folder, overwrite in place)

Leave a one-line breadcrumb pointing at it. Soft nudge, not a block — fix it on
your next handoff write.
"""

_BREADCRUMB_NUDGE = """\
[Allostat handoff watchdog — detail sibling has no breadcrumb]

A `<session_id>.detail.md` file exists next to your handoff, but the handoff
never references it. A resuming session reads only the lean handoff — without a
pointer, that detail is orphaned. Add a one-line breadcrumb in the relevant
section, e.g.:

  ... → `<session_id>.detail.md`.

Soft nudge — fix it on your next handoff write.
"""


#: Used when a legacy caller supplies no resolved path. It must NOT be a
#: cwd-derived harness pattern: naming `~/.claude/projects/<cwd>/...` is what
#: sent handoffs into the harness tree on 2026-08-04. Pointing at the hook's
#: own surfaced value is always correct and never contradicts it.
_UNKNOWN_HANDOFF_PATH = "the exact handoff path the SessionStart hook surfaced"


def _select_reminder_text(escalation_level: int, handoff_path=None) -> str:
    """Pick reminder text for current escalation level. Returns empty
    string for level 0 (no reminder).

    `handoff_path` is substituted into the template so the reminder names the
    SAME destination SessionStart already computed. Substitution is a literal
    replace rather than str.format() because the level-3 stub contains markdown
    braces that format() would try to interpret.
    """
    if escalation_level <= 0:
        return ""
    if escalation_level == 1:
        text = _REMINDER_LEVEL_1
    elif escalation_level == 2:
        text = _REMINDER_LEVEL_2
    else:
        text = _REMINDER_LEVEL_3
    return text.replace(
        "{handoff_path}",
        str(handoff_path) if handoff_path else _UNKNOWN_HANDOFF_PATH,
    )


def consume_reminder_if_due(
    state_dir: Path,
    session_id: str = "",
    cumulative_tokens: Optional[int] = None,
    handoff_path: Optional[Path] = None,
) -> str:
    """Called from UserPromptSubmit hook. Returns reminder text to inject
    via additionalContext, or empty string.

    Per-session: reads/writes THIS session's state file, so a reminder queued
    for another session in the same cwd is never surfaced here. Also increments
    turn count + updates token estimate, and clears the reminder_pending flag
    (so each reminder fires exactly once per Stop trigger; escalation builds on
    the NEXT Stop fire if Claude still hasn't written).
    """
    if disabled():
        return ""

    # First touch of this session → prune stale per-session files (once).
    if session_id and not _state_file_path(state_dir, session_id).is_file():
        prune_stale_state(state_dir)

    state = read_state(state_dir, session_id)
    if state.session_id == "":
        # No session id known (legacy caller) — just bump the turn counter
        # and bail.
        state.turns_since_last_write += 1
        if cumulative_tokens is not None and cumulative_tokens > state.last_seen_cumulative_tokens:
            state.last_seen_cumulative_tokens = cumulative_tokens
        write_state(state_dir, state)
        return ""

    state.turns_since_last_write += 1
    if cumulative_tokens is not None and cumulative_tokens > state.last_seen_cumulative_tokens:
        state.last_seen_cumulative_tokens = cumulative_tokens

    reminder_text = ""
    if state.reminder_pending and state.escalation_level > 0:
        reminder_text = _select_reminder_text(state.escalation_level, handoff_path)
        state.reminder_pending = False  # consumed; next Stop may re-fire if still overdue
    elif state.breadcrumb_nudge_pending:
        # Orphan-detail risk outranks mild bloat. One-shot.
        reminder_text = _BREADCRUMB_NUDGE
        state.breadcrumb_nudge_pending = False
        state.session_breadcrumb_nudge_count += 1
    elif state.oversize_nudge_pending:
        # Lowest of the three: under-write (continuity gap) > orphan > over-write.
        reminder_text = _OVERSIZE_NUDGE
        state.oversize_nudge_pending = False
        state.session_oversize_nudge_count += 1

    write_state(state_dir, state)
    return reminder_text


def resume_flush(
    state_dir: Path,
    project_root: Path,
    session_id: Optional[str],
    cumulative_tokens: Optional[int] = None,
) -> dict:
    """A1 (2026-06-26) — SessionStart resume-flush.

    The Stop hook is the ONLY place `check_and_set_reminder_state` runs, so a
    hard restart where Stop never fired leaves an overdue handoff unqueued — the
    turns/tokens counters climbed (via UserPromptSubmit's `consume_reminder_if_due`)
    but no reminder was ever armed. `session_id` is stable across a Claude Code
    restart, so the per-session state file is still on disk. Re-running the
    watchdog check at SessionStart with the SAME session_id re-arms an overdue
    reminder so the next UserPromptSubmit surfaces it.

    Records the session identity baseline (prunes stale per-session files,
    adopts cumulative_tokens), then delegates to `check_and_set_reminder_state`
    so the elapsed/turns/tokens trigger logic is shared (no duplicated thresholds).
    Idempotent — re-call on every SessionStart; cheap on a fresh session (no
    accumulated counters → no trip). Best-effort: never raises.

    Returns the `check_and_set_reminder_state` result dict (or a skip marker).
    """
    if disabled() or not session_id:
        return {"action": "skipped", "reason": "disabled_or_no_session_id"}
    # H-18: only prune on this session's FIRST touch (no state file yet), the
    # same guard consume_reminder_if_due uses. Pruning unconditionally before
    # read_state deletes THIS session's own file when it is >=14 days stale (a
    # long-idle resume), so the subsequent read returns a zeroed default and the
    # overdue escalation/turn counters are lost an instant before we flush them.
    if not _state_file_path(state_dir, session_id).is_file():
        prune_stale_state(state_dir)
    state = read_state(state_dir, session_id)
    # Adopt the identity + the token baseline WITHOUT wiping accumulated
    # counters: a genuine restart keeps the same session_id, and those counters
    # are exactly the overdue-work signal we want to flush. Only a brand-new id
    # (no prior file) starts fresh.
    if state.session_id == "":
        state.session_id = session_id
    if cumulative_tokens is not None and cumulative_tokens > state.last_seen_cumulative_tokens:
        state.last_seen_cumulative_tokens = cumulative_tokens
    write_state(state_dir, state)
    # Re-run the shared check so an overdue reminder is re-armed at restart.
    return check_and_set_reminder_state(state_dir, project_root, session_id)


def initialize_session(
    state_dir: Path,
    session_id: str,
    cumulative_tokens: Optional[int] = None,
) -> None:
    """Back-compat alias (A1, 2026-06-26). Superseded by `resume_flush`, which
    additionally re-arms an overdue reminder at restart. Retained because older
    callers / tests reference `initialize_session`. Records the session_id +
    token baseline without raising; does NOT itself run the reminder check (call
    `resume_flush` for that — it needs project_root to locate the handoff file).
    """
    if disabled() or not session_id:
        return
    # H-18: guard the prune (see resume_flush) so a long-idle session's own
    # >=14-day-stale state file isn't deleted right before we read it, which
    # would zero its accumulated counters.
    if not _state_file_path(state_dir, session_id).is_file():
        prune_stale_state(state_dir)
    state = read_state(state_dir, session_id)
    if state.session_id != session_id:
        state = WatchdogState(session_id=session_id)
    if cumulative_tokens is not None and cumulative_tokens > state.last_seen_cumulative_tokens:
        state.last_seen_cumulative_tokens = cumulative_tokens
    write_state(state_dir, state)


def get_status_summary(state_dir: Path, session_id: str = "") -> dict:
    """R6 visibility (advisor refinement) — return per-session counters for
    the `/allostat-handoff-status` slash command. With an empty session_id,
    reports the most-recently-active session's state.
    """
    if not session_id:
        session_id = _most_recent_session_id(state_dir)
    state = read_state(state_dir, session_id)
    return {
        "session_id": state.session_id,
        "session_handoff_count": state.session_handoff_count,
        "session_reminder_count": state.session_reminder_count,
        "session_response_count": state.session_response_count,
        "anti_pattern_count": state.anti_pattern_count,
        "session_oversize_nudge_count": state.session_oversize_nudge_count,
        "session_breadcrumb_nudge_count": state.session_breadcrumb_nudge_count,
        "oversize_chars_threshold": oversize_chars_threshold(),
        "turns_since_last_write": state.turns_since_last_write,
        "tokens_estimate_since_last_write": max(
            0, state.last_seen_cumulative_tokens - state.tokens_at_last_write
        ),
        "current_escalation_level": state.escalation_level,
        "reminder_pending": state.reminder_pending,
        "thresholds": {
            "turns": turns_threshold(),
            "tokens": tokens_threshold(),
            "disabled": disabled(),
        },
        "last_handoff_path": state.last_handoff_path,
        "last_handoff_size": state.last_handoff_size,
    }
