"""Session-end matcher with article-normalization + negation filter.

PORTED FROM α.5 (v2.3 plugin's `lib/canon_appendix_loader.py`). Wrapper
plugin MUST inherit article-normalization from day one per the η
acceptance bullet — the bug the α.5 patch fixed must not regress on
the v2.4 wrapper track.

Lockstep with v2.3 plugin's lib/canon_appendix_loader.py:
  _ARTICLES_RE     — article tokens stripped between word boundaries
  _NEGATION_TOKENS — 30-char-upstream-window check for negation prefix
  _normalize_haystack — three-pass: lowercase → apostrophe-strip → article-strip
                                    → whitespace-collapse
  _match_topics     — find substring + check negation-prefix

The wrapper uses this matcher to detect session-end signals in operator
prompts. When matched, the wrapper fires the dusk-phase Stop hook
sweep (volume_control_evaluate + pattern_observer_evaluate).
"""
from __future__ import annotations

import re
from typing import Sequence


# Article tokens stripped between word boundaries.
# Locked in lockstep with plugin/lib/canon_appendix_loader.py:_ARTICLES_RE.
_ARTICLES_RE = re.compile(r"\b(?:the|this|that|these|those|my|your|our|a|an)\b")

# Negation tokens. 30-char-upstream-window check rejects matches preceded
# by negation. Locked in lockstep with plugin's _NEGATION_TOKENS.
_NEGATION_TOKENS: tuple[str, ...] = (
    "dont", "do not", "didnt", "did not", "wont", "will not",
    "cant", "cannot", "shouldnt", "should not",
    "wouldnt", "would not", "isnt", "is not", "arent", "are not",
    "not yet", "never",
)


# Canonical session-end topic words. The matcher checks for these as
# substrings of the normalized haystack. New phrasings should be added
# as topic entries — locked in lockstep with the plugin seed file
# data/appendix_seed/session_end_memory_sweep.md::topics array.
SESSION_END_TOPICS: tuple[str, ...] = (
    "session-end",
    "end-session",
    "end-of-session",
    "session-over",
    "were-done",
    "good-to-go",
    "wrap-up-session",
    "stopping-here",
    "lets-call-it",
)


def normalize_haystack(message: str) -> str:
    """Three-pass normalization: lowercase → strip apostrophes → strip
    articles → collapse whitespace.

    Lockstep with plugin/lib/canon_appendix_loader.py::_normalize_haystack.
    """
    if not message:
        return ""
    no_apos = message.lower().replace("'", "")
    no_articles = _ARTICLES_RE.sub(" ", no_apos)
    return re.sub(r"\s+", " ", no_articles).strip()


def _is_negated(haystack: str, match_start: int) -> bool:
    """30-char-upstream-window negation check.

    Lockstep with plugin's _is_negated.
    """
    prefix = haystack[max(0, match_start - 30):match_start]
    return any(token in prefix for token in _NEGATION_TOKENS)


def match_session_end_topics(
    message: str,
    topics: Sequence[str] = SESSION_END_TOPICS,
) -> list[str]:
    """Return the subset of topics that appear in `message` after
    normalization + negation filter.

    Lockstep with plugin/lib/canon_appendix_loader.py::_match_topics.
    """
    if not message:
        return []
    haystack = normalize_haystack(message)
    matches: list[str] = []
    for topic in topics:
        if not topic:
            continue
        normalized = topic.lower().replace("_", " ").replace("-", " ")
        candidates = {topic.lower(), normalized}
        for c in candidates:
            idx = haystack.find(c)
            if idx == -1:
                continue
            if _is_negated(haystack, idx):
                continue
            matches.append(topic)
            break
    return matches


def is_session_end_signal(message: str) -> bool:
    """Convenience: True iff any session-end topic matches.

    Used by the UserPromptSubmit hook to decide whether the Stop-hook
    end-of-session sweep should be primed.
    """
    return bool(match_session_end_topics(message))
