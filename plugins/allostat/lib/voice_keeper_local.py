"""Voice-keeper local evaluator — PATCH-190 (Phase 3.2).

Wrapper-side port of `allostat-mcp/allostat_server/pillars/voice_keeper.py`.
The pillar logic (markers + evaluate + composer) runs LOCALLY in the
wrapper so the server never receives raw assistant prose. This preserves
the privacy invariant in `ExcerptPayload`'s docstring:

    NEVER include:
      - operator's literal prompt text [...]
      - LLM response content

Voice evaluation inherently needs assistant prose to scan; running it
wrapper-side keeps the schema clean. Mirrors PATCH-143 precedent (innate
rules ported wrapper-side for the same architectural reason — server
shouldn't see what it can evaluate client-side).

Server-side `voice_keeper_evaluate` MCP tool is preserved as a direct-
tool-call surface (Claude can voluntarily invoke it on text it explicitly
passes). The autonomic firing path — invoked from UserPromptSubmit hook
between operator turns — is the wrapper-local code path defined here.

Constants are duplicated by design between this module and
`allostat_server/pillars/voice_keeper.py`. If either side updates, update
BOTH. Test coverage on both sides asserts parity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


# ============================================================================
# Constants — locked to server-side voice_keeper.py
# ============================================================================

# AI-slop indicators — strong markers; presence means AI-generated text.
AI_SLOP_MARKERS: tuple[str, ...] = (
    "delve into",
    "delving",
    "comprehensive",
    "robust",
    "leverage",
    "tapestry",
    "navigate the",
    "navigate this",
    "cutting-edge",
    "cutting edge",
    "state-of-the-art",
    "state of the art",
    "in today's fast-paced",
    "in this digital age",
    "it's important to note",
    "it is important to note",
    "as we move forward",
    "in the realm of",
    "embark on",
    "harness the power",
    "unlock the potential",
    "seamlessly",
    "vibrant",
)

# Hedge phrases — operator strips these.
HEDGE_MARKERS: tuple[str, ...] = (
    "it could be argued",
    "in a sense",
    "to some extent",
    "arguably",
    "generally speaking",
    "in most cases",
    "for the most part",
    "as a general rule",
    "more or less",
    "kind of",
    "sort of",
)

# Corporate boilerplate.
CORPORATE_MARKERS: tuple[str, ...] = (
    "stakeholder",
    "synergy",
    "going forward",
    "circle back",
    "deep dive",
    "low-hanging fruit",
    "low hanging fruit",
    "at the end of the day",
    "moving the needle",
    "drive engagement",
    "thought leader",
)

EM_DASH_RATIO_THRESHOLD = 0.015
PASSIVE_VOICE_THRESHOLD = 0.30

DEFAULT_BANNED_TERMS: tuple[str, ...] = (
    "revolutionary",
    "groundbreaking",
    "game-changing",
    "next-level",
    "incredibly",
    "extremely",
    "tremendously",
)

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
# Pure evaluation functions (mirror server-side)
# ============================================================================


def _count_passive_voice(text: str) -> tuple[int, int]:
    """Heuristic passive-voice detection. Returns (passive_sentences, total)."""
    sentences = re.split(r"[.!?]+\s+", text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return 0, 0
    passive_patterns = [
        r"\b(?:was|were|is|are|been|being)\s+\w+(?:ed|en)\b",
        r"\b(?:has|have|had)\s+been\s+\w+(?:ed|en)\b",
    ]
    passive_count = 0
    for s in sentences:
        for pat in passive_patterns:
            if re.search(pat, s, re.IGNORECASE):
                passive_count += 1
                break
    return passive_count, len(sentences)


def _strip_excluded_regions(text: str) -> str:
    """Voice-keeper V1-V4 audit fix (2026-05-25, S1): remove regions of
    the assistant's text that should NOT count toward voice-fidelity scans.

    Excluded:
      - Fenced code blocks (``` ... ```). Code is not assistant prose. A
        function literally named `leverage_existing_structure` is code; it
        should not trip the `ai_slop` marker for "leverage."
      - Indented code blocks (4-space leading, common Markdown convention).
      - Blockquote regions (lines starting with `>`). These quote operator
        content — when the assistant faithfully reproduces operator's
        word choices, slop in the QUOTE belongs to the operator, not the
        assistant.
      - URLs (`https?://\\S+`). URL paths often contain marker words by
        coincidence (e.g., `/leverage/` in an SEO blog URL).
      - Explicit double-quoted strings within prose. Operator-quotes are
        most commonly rendered with literal ASCII `"..."`. We strip those
        so quoted operator language doesn't count against the assistant.

    Returns the stripped text. Output preserves enough surrounding
    structure that downstream em-dash and passive-voice counts still
    operate on representative prose. Behavior on adversarial inputs
    (nested fences, unclosed quotes) is best-effort: the regex is
    non-greedy and any unmatched region passes through unchanged.

    Reference: audit_reports/05252026_session_03_stress_test_findings.md
    V1-V4 (all four share this root cause).
    """
    # Fenced code blocks (triple-backtick). Non-greedy DOTALL to handle
    # multi-line blocks; preserves text outside the fences.
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    # Indented code blocks. Conservative: a run of lines each starting with
    # 4+ spaces, separated by blank lines, gets collapsed.
    text = re.sub(
        r"(?:^|\n)(?:(?: {4,}|\t).*(?:\n|$))+",
        "\n",
        text,
    )
    # Blockquote regions: any line starting with > (optional leading whitespace).
    text = re.sub(r"(?m)^[ \t]*>.*$", "", text)
    # URLs.
    text = re.sub(r"https?://\S+", " ", text)
    # Inline code (single-backtick-bounded, single-line). F-2 fix
    # (2026-05-27): function names, variable names, file paths, and other
    # identifier-shaped tokens often render as `inline-code` and contain
    # words that overlap voice markers (e.g., `leverage_existing_structure`
    # is a function name, not slop prose). Stripped before marker scans
    # for the same reason fenced blocks are stripped.
    text = re.sub(r"`[^`\n]+`", " ", text)
    # Double-quoted strings — non-greedy.
    text = re.sub(r'"[^"\n]{1,300}"', " ", text)
    return text


def _scan_markers(
    text_lower: str,
    markers: tuple[str, ...],
    category: str,
    severity: str,
) -> list[dict[str, Any]]:
    """Scan text for marker phrases; return list of violation dicts.

    H-02 (audit 2026-07-08, fixed Wave-2 2026-07-11): violations used to
    carry a raw ±30-char "snippet" of the assistant's response — and
    voice_violation_detected observations are forwarded to the server by
    every hook (excerpt.observations), so response prose crossed the wire.
    Violations now carry NON-REVERSIBLE forms only:
      - `marker` — the matched phrase from a fixed public marker list
      - `marker_offset` — the match position in the scanned (stripped,
        lowercased) text
      - `context_sha256` — truncated sha256 of the old ±30-char context
        window (identity for dedupe, not content)
    Local detection quality is unaffected: nudge composition and silo
    inscribe key off `marker`/`category`, which are more stable
    fingerprint inputs than the variable prose window ever was.
    """
    found: list[dict[str, Any]] = []
    for marker in markers:
        count = text_lower.count(marker)
        if count > 0:
            idx = text_lower.find(marker)
            start = max(0, idx - 30)
            end = min(len(text_lower), idx + len(marker) + 30)
            context = text_lower[start:end].strip()
            found.append({
                "category": category,
                "marker": marker,
                "occurrences": count,
                "severity": severity,
                "marker_offset": idx,
                "context_sha256": hashlib.sha256(
                    context.encode("utf-8")
                ).hexdigest()[:16],
            })
    return found


def evaluate_voice(target_text: str) -> dict[str, Any]:
    """Run heuristic voice-fidelity evaluation on target_text.

    Mirrors server-side `voice_keeper.evaluate_voice`. Returns dict with
    violations, em_dash_count, em_dash_ratio, passive_voice_ratio,
    word_count, loop_status, summary.
    """
    # V1-V4 audit fix (2026-05-25, S1): strip code blocks, blockquotes, URLs,
    # and quoted operator-text BEFORE running marker scans + em-dash counts +
    # passive-voice analysis. These regions reflect operator content or code,
    # not assistant prose — they should not count toward voice-fidelity drift.
    scanned_text = _strip_excluded_regions(target_text)
    text_lower = scanned_text.lower()
    # Word count uses the STRIPPED text so the em-dash ratio reflects
    # assistant prose density, not code-block-inflated denominators.
    word_count = len(scanned_text.split())
    violations: list[dict[str, Any]] = []

    violations.extend(_scan_markers(text_lower, AI_SLOP_MARKERS, "ai_slop", "high"))
    violations.extend(_scan_markers(text_lower, HEDGE_MARKERS, "hedge", "medium"))
    violations.extend(_scan_markers(text_lower, CORPORATE_MARKERS, "corporate", "medium"))
    violations.extend(_scan_markers(text_lower, DEFAULT_BANNED_TERMS, "banned_term", "low"))

    em_dash_count = scanned_text.count("—") + scanned_text.count("--")
    em_dash_ratio = em_dash_count / max(word_count, 1)
    if word_count >= 100 and em_dash_ratio > EM_DASH_RATIO_THRESHOLD:
        violations.append({
            "category": "em_dash_overuse",
            "marker": "em-dash",
            "occurrences": em_dash_count,
            "severity": "medium",
            # H-02: stat string only — renamed from "snippet" so no
            # violation dict ever carries that key (the wire-boundary
            # scrub keys off it for legacy records).
            "detail": (
                f"{em_dash_count} em-dashes in {word_count} words "
                f"({em_dash_ratio:.3f} ratio, threshold {EM_DASH_RATIO_THRESHOLD})"
            ),
        })

    passive_count, total_sentences = _count_passive_voice(scanned_text)
    passive_ratio = passive_count / max(total_sentences, 1)
    if total_sentences >= 5 and passive_ratio > PASSIVE_VOICE_THRESHOLD:
        violations.append({
            "category": "passive_voice",
            "marker": "passive_construction",
            "occurrences": passive_count,
            "severity": "high",
            # H-02: stat string only (see em_dash_overuse note above).
            "detail": (
                f"{passive_count} of {total_sentences} sentences passive "
                f"({passive_ratio:.0%}, threshold {PASSIVE_VOICE_THRESHOLD:.0%})"
            ),
        })

    loop_status = "deviation" if violations else "satisfied"
    summary = (
        "voice fidelity passes — no violations detected"
        if not violations
        else f"{len(violations)} violation(s) detected: "
        + ", ".join(sorted({v["category"] for v in violations}))
    )

    return {
        "violations": violations,
        "em_dash_count": em_dash_count,
        "em_dash_ratio": em_dash_ratio,
        "passive_voice_ratio": passive_ratio,
        "word_count": word_count,
        "loop_status": loop_status,
        "summary": summary,
    }


def compose_voice_nudge(
    recent_violation_events: list[dict[str, Any]],
) -> str | None:
    """Compose a voice-keeper nudge from recent violation events.

    Priority order (most-actionable first):
      1. Sycophancy in most recent event
      2. AI-slop in most recent event (with examples)
      3. Cumulative drift (≥3 recent events)

    Returns the nudge line or None when no nudge fires.
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


# ============================================================================
# Wrapper-side glue — transcript reader + throttle + orchestrator
# ============================================================================


def read_prior_assistant_response(transcript_path: Path) -> str | None:
    """Extract the most-recent assistant response text from Claude Code's
    per-session transcript JSONL.

    Reads the file line-by-line, finds the LAST assistant message, and
    concatenates its text content blocks. Returns None on missing file,
    parse failure, or no assistant message present.

    The transcript path is provided by Claude Code in the hook payload as
    `transcript_path` or `transcriptPath`.
    """
    if not transcript_path or not transcript_path.is_file():
        return None
    last_text: str | None = None
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "assistant":
                    continue
                content = msg.get("message", {}).get("content")
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    # Anthropic format: list of content blocks
                    pieces = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            pieces.append(block.get("text", ""))
                    if pieces:
                        last_text = "".join(pieces)
    except OSError:
        return None
    return last_text


# Phase 3.2 (2026-05-24): voice-keeper cadence capped at max N=3 per
# operator's folie-à-deux framing. If voice-keeper fires too rarely,
# drift can compound between operator + Claude across many turns before
# correction surfaces. Cap ensures evaluation fires at least every 3rd
# turn even when operator opts for less frequent firing.
_VOICE_CADENCE_MAX = 3


def _voice_cadence() -> int:
    """Read ALLOSTAT_VOICE_KEEPER_CADENCE from env, clamped at max=3.

    Returns:
      0  → disabled (operator-explicit opt-out)
      1  → every prompt (default)
      2  → every 2nd prompt
      3  → every 3rd prompt (maximum allowed sparsity)

    Values above 3 are clamped to 3 per the folie-à-deux framing.
    Negative or non-integer values fall back to 1.
    """
    raw = os.environ.get("ALLOSTAT_VOICE_KEEPER_CADENCE", "")
    if not raw:
        return 1
    try:
        v = int(raw)
    except ValueError:
        return 1
    if v < 0:
        return 1
    if v == 0:
        return 0
    return min(v, _VOICE_CADENCE_MAX)


def should_evaluate_this_turn(turn_count: int) -> bool:
    """True when this turn should run voice-keeper evaluation.

    Honors ALLOSTAT_VOICE_KEEPER_CADENCE (clamped to [0, _VOICE_CADENCE_MAX]):
      0  → disabled (always False)
      1  → every turn (default)
      N  → every Nth turn (turn_count % N == 0); N ≤ 3 by clamp

    turn_count is the operator-prompt count for the current session
    (1-indexed; first prompt = 1).
    """
    cadence = _voice_cadence()
    if cadence == 0:
        return False
    if cadence == 1:
        return True
    return turn_count > 0 and (turn_count % cadence == 0)


def _inscribe_voice_violations(
    state_dir: Path,
    violations: list[dict[str, Any]],
    session_id: str | None,
) -> int:
    """Inscribe one silos/voice.jsonl entry per detected violation.

    2026-08-07 — these rows are DETECTIONS, and they now say so. Until this
    date every one of them was written with `success_marker="voice_corrected"`
    while nothing was surfaced to the operator and nothing was corrected; the
    bench VM's 17 unsurfaced violations produced 17 rows all claiming a
    correction had occurred. Anything reading the silo as a record of what the
    operator taught was reading fiction, and getting more confident about it
    over time. The path now calls `record_voice_detection`, and
    `voice_corrected` has exactly one gated way in
    (`silos.voice.record_voice_correction`, which requires evidence the
    correction was surfaced).

    PATCH-195 (2026-05-24) — wrapper-direct voice silo inscribe. Called
    from evaluate_and_record after observation append. Mirrors PATCH-194
    drift inscribe semantic: heuristic auto-inscribe of the detected
    case so future recall fingerprint-matches against accumulated
    history. The violation type and sample stay detection evidence in the local
    fingerprint only. This path emits no resolution and creates no canonical
    binding; canonical voice can enter only through acknowledged correction.

    Args:
        state_dir: operator's `.allostat/` dir (for resolving silo path)
        violations: list of violation dicts from evaluate_voice
        session_id: current session id; "" if unknown

    Returns count of entries inscribed (defense — caller can log on 0).
    Caller is responsible for swallowing any exception so a silo write
    error doesn't break the voice-keeper firing path.
    """
    try:
        from silos.voice import (  # noqa: E402
            VoiceFingerprint,
            record_voice_detection,
        )
        from silo_base import resolve_silo_path  # noqa: E402
    except ImportError:
        return 0
    silo_path = resolve_silo_path(state_dir, "voice")
    inscribed = 0
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        violation_type = str(violation.get("category") or "")
        sample_text = str(
            violation.get("snippet")
            or violation.get("marker")
            or ""
        )
        if not violation_type:
            continue
        fingerprint = VoiceFingerprint(
            violation_type=violation_type,
            sample_text=sample_text,
        )
        try:
            record_voice_detection(
                silo_path,
                fingerprint,
                session_id=session_id or "",
            )
            inscribed += 1
        except OSError:
            continue
    return inscribed


def evaluate_and_record(
    transcript_path: Path,
    state_dir: Path,
    recent_violations: list[dict[str, Any]],
    session_id: str | None = None,
) -> str | None:
    """End-to-end: read prior assistant turn, evaluate voice, record
    violation observation, return nudge text if appropriate.

    Args:
        transcript_path: Claude Code session transcript JSONL.
        state_dir: operator's `<project>/.allostat/` directory (for
            observation append).
        recent_violations: list of recent `voice_violation_detected`
            observations (for cumulative-drift nudge composition).
        session_id: current session id (PATCH-195) — propagated to
            silo entries inscribed for each detected violation so the
            recall surface can show which session produced the case.
            Optional; missing session_id still inscribes (with empty
            session_id field — matches drift inscribe behavior).

    Returns the nudge line to surface via additionalContext, or None if
    no nudge fires this turn. Caller is responsible for emitting.

    Best-effort throughout — any failure returns None silently rather
    than breaking the hook.
    """
    try:
        from local_state import append_observation  # noqa: E402
    except ImportError:
        return None

    text = read_prior_assistant_response(transcript_path)
    if not text:
        return None

    try:
        result = evaluate_voice(text)
    except Exception:
        return None

    violations = result.get("violations") or []
    if violations:
        try:
            append_observation(
                state_dir,
                "voice_violation_detected",
                details={
                    "payload": {
                        "violations": violations[:10],  # cap to avoid jsonl bloat
                        "word_count": result.get("word_count"),
                        "loop_status": result.get("loop_status"),
                    },
                },
            )
        except Exception:
            pass

        # PATCH-195 (2026-05-24) — Phase 3.4c voice silo inscribe.
        # Each detected violation inscribes one entry to silos/voice.jsonl
        # via the existing record_voice_resolution primitive (PATCH-192).
        # Wrapper-direct path — voice-keeper detection is wrapper-local
        # (PATCH-190), so the inscribe is too. No server round-trip.
        #
        # Token construction in lib/silos/voice._to_silo_fp:
        #   [violation_type, sample_text[:60].lower()]
        # Mirrors server-side compute_class_tokens("voice", ...) byte-
        # for-byte. Future encounters of similar voice patterns will
        # fingerprint-match against these inscribed cases on recall.
        try:
            _inscribe_voice_violations(state_dir, violations, session_id)
        except Exception:
            pass

    # Append the current event to recent_violations for composer input
    # (server-side parity: the composer reads recent observation log).
    current_event = {
        "event": "voice_violation_detected",
        "details": {"payload": result},
    }
    combined = list(recent_violations) + [current_event]
    try:
        nudge = compose_voice_nudge(combined)
    except Exception:
        return None
    return nudge
