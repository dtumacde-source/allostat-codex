"""Interrupt-override detection — PATCH-141 v1.1.1.

Two-layer detection used by the UserPromptSubmit hook to suppress chrome
injection when the operator is interrupting the assistant. Both layers
are pure (no I/O dependencies beyond the transcript file Layer B opens)
and unit-testable in isolation.

Failure mode this closes
------------------------
UserPromptSubmit hook injects ~400 tokens of chrome (metabolism,
stress-response, surface-directive) on every prompt. When operator's
message is a bare interrupt-override command ("stop", "idle", etc.) or
arrives in a post-interrupt context, the chrome above the operator's
actual words reframes parsing toward literal interpretation and the
assistant misses the interrupt intent. Documented in advisor brief
2026-05-21 §1 — observed live in the prior advisor session four times
in a row.

Detection layers
----------------
Layer A — `looks_like_interrupt_override`
    Leading-word match (PATCH-150) over an explicit vocab (stop, idle,
    wait, halt, pause, cancel, abort, nevermind, never mind, knock it
    off, hold on, hold up): fires when the prompt's FIRST word is in the
    vocab AND the whole prompt is <=50 chars. The length cap keeps verbose
    pivots out. Deliberate trade-off (not anchoring): a short phrase whose
    first word is a vocab word — e.g. "stop the war" — also matches, which
    is accepted because catching real terse operator interrupts outweighs
    the rare false-positive (see test_layer_a_fires_on_leading_interrupt_vocab).

Layer B — `last_assistant_was_interrupted`
    Walks the transcript JSONL. If the most recent assistant turn is
    followed by a `[Request interrupted by user]` synthetic user-role
    text message (Claude Code's harness marker for operator interrupts),
    we're in a post-interrupt context regardless of what the operator's
    follow-up message looks like. Fails CLOSED on read/parse errors —
    we'd rather render chrome on a corrupt-transcript false-negative
    than silently suppress on a transient I/O blip.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


# PATCH-150 (2026-05-22) — leading-word strategy.
#
# Previous: strict bare-vocab anchored regex required the entire prompt to
# be vocab + optional whitespace + optional terminal `.!?`. Failed on common
# operator phrasings: `idle, now!`, `stop now`, `wait please`, `pause for
# a second` — observed firsthand during the v1.2.0 audit when operator
# typed `idle, now!` and chrome still fired.
#
# New: extract the LEADING WORD (alphabetic chars at start of prompt after
# whitespace); if that word is in the interrupt vocab AND total prompt is
# ≤50 chars, treat as interrupt. Preserves false-positive defense via the
# length cap (a 60-char sentence starting with "idle" doesn't match), while
# accepting natural variants like "stop now", "idle, now!", "wait please".
_INTERRUPT_VOCAB_WORDS = frozenset({
    "stop", "idle", "wait", "halt", "pause", "cancel",
    "abort", "nevermind", "hold", "knock",
})
_LEADING_WORD_RE = re.compile(r"^\s*([a-z]+)", re.IGNORECASE)


def looks_like_interrupt_override(prompt: str) -> bool:
    """Layer A: leading-word interrupt-vocab match.

    Returns True iff:
      - `prompt` is ≤50 chars (length cap defends against long sentences
        that happen to start with a vocab word)
      - The first alphabetic word in the prompt (case-insensitive) is in
        the interrupt vocab

    This accepts operator-natural variants the prior anchored-regex missed:
      `idle, now!`, `stop now`, `wait please`, `pause for a moment`,
      `hold on a sec`, `nevermind the previous`, etc.

    Still rejects substring false-positives:
      `the idle threats are silly` — starts with "the", not vocab.
      `stop the war is just a slogan` (>50 chars) — over length cap.

    Multi-word vocab entries like "never mind" / "knock it off" / "hold on"
    / "hold up" reduce to their leading word: "never" → "nevermind" alias
    NOT in vocab (deliberate — leading word "never" alone is ambiguous;
    require "nevermind" as single word). "knock" / "hold" leading-word
    forms are accepted via the single-word vocab.
    """
    if not prompt or len(prompt) > 50:
        return False
    m = _LEADING_WORD_RE.match(prompt)
    if not m:
        return False
    leading = m.group(1).lower()
    # Special-case "never" → only fires if explicitly followed by the WORD
    # "mind" (M15 fixpass 2026-07-01: bare startswith matched "never
    # mindful"/"never minded" — chrome suppressed on non-interrupts).
    if leading == "never":
        rest = prompt[m.end():].strip().lower()
        return bool(re.match(r"mind\b", rest))
    return leading in _INTERRUPT_VOCAB_WORDS


def last_assistant_was_interrupted(transcript_path: Path) -> bool:
    """Layer B: transcript-marker detection.

    Returns True iff the most recent assistant turn in the transcript
    was followed by a `[Request interrupted by user]` synthetic
    user-role text message (Claude Code's harness marker for
    operator-initiated interrupts of in-flight assistant turns).

    The marker sits in the transcript between the last assistant entry
    and any subsequent operator prompt, so its presence in that range
    indicates the current prompt is a post-interrupt directive.

    Returns False (fail-closed) on any read/parse error.
    """
    entries: list[dict] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return False

    # Find index of the most recent assistant turn.
    last_assistant_idx = -1
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx < 0:
        return False

    # Look at all entries AFTER the last assistant turn. If any is a
    # user-role text message whose first text block starts with the
    # interrupt-marker string, the assistant turn was interrupted.
    for msg in entries[last_assistant_idx + 1:]:
        if msg.get("type") != "user":
            continue
        message = msg.get("message", {})
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        # Claude Code stores user content as EITHER a list of blocks OR a plain
        # string. The string shape was silently dropped, missing real interrupts
        # (the sibling reader handoff_autocapture handles both). Handle it here.
        if isinstance(content, str):
            if content.startswith("[Request interrupted by user"):
                return True
            continue
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") != "text":
                continue
            text = c.get("text", "")
            if isinstance(text, str) and text.startswith("[Request interrupted by user"):
                return True
    return False
