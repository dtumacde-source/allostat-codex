"""Allostat auto merge proposer — PATCH-139 / v1.0.0 Slice 7.

TF-IDF-style overlap detection across memory tree .md files. Surfaces
high-overlap pairs as merge candidates for operator review via
/allostat-tend --merge-candidates.

Privacy: entirely wrapper-side. Files read locally; TF-IDF computed
locally; merge proposals surface to operator only. Server never sees
content or proposals.

Ported from the v2.3 plugin's auto_merge_proposer.py into the v2.4 wrapper.
Simplified — drops the macrophage-cleanup metaphor docstrings; keeps the
detection algorithm.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


# Tokenize: lowercase words, length >= 3, alphanumeric
_TOKEN_PATTERN = re.compile(r"[a-z0-9]{3,}")

# Skip common English + markdown structural words
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "have", "has",
    "are", "was", "were", "been", "will", "would", "could", "should",
    "what", "when", "where", "how", "why", "which", "but", "not", "all",
    "any", "some", "more", "most", "such", "also", "very", "much",
    "just", "now", "then", "than", "into", "through", "about", "between",
    "without", "during", "after", "before", "above", "below",
    "across", "against", "along", "among", "around", "behind",
    "beneath", "beside", "beyond", "inside", "outside", "over",
    "under", "until", "upon", "within",
    # Markdown structural
    "https", "http", "com", "org", "html",
})

# Pairs with overlap_jaccard >= threshold are surfaced as candidates
OVERLAP_THRESHOLD = 0.35


@dataclass
class MergeCandidate:
    file_a: str
    file_b: str
    overlap_jaccard: float
    top_shared_terms: list[str]
    suggested_merged_name: str


def _tokenize(text: str) -> Counter:
    """Tokenize markdown text. Returns Counter of term frequencies."""
    tokens = []
    for m in _TOKEN_PATTERN.finditer(text.lower()):
        t = m.group(0)
        if t in _STOPWORDS:
            continue
        tokens.append(t)
    return Counter(tokens)


def _compute_tf_idf(corpus: dict[str, Counter]) -> dict[str, dict[str, float]]:
    """Standard TF-IDF over a corpus. Returns per-file dict[term, tfidf_score]."""
    n_docs = len(corpus)
    if n_docs == 0:
        return {}

    # IDF
    doc_freq: Counter = Counter()
    for tf in corpus.values():
        for term in tf:
            doc_freq[term] += 1
    idf = {term: math.log(n_docs / df) for term, df in doc_freq.items()}

    # TF-IDF per file
    result: dict[str, dict[str, float]] = {}
    for fname, tf in corpus.items():
        total = sum(tf.values())
        if total == 0:
            result[fname] = {}
            continue
        result[fname] = {
            term: (count / total) * idf.get(term, 0.0)
            for term, count in tf.items()
        }
    return result


def _top_terms_per_file(tfidf: dict[str, dict[str, float]], k: int = 20) -> dict[str, set[str]]:
    """For each file, return the top-k highest-TFIDF terms (signature)."""
    result: dict[str, set[str]] = {}
    for fname, scores in tfidf.items():
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
        result[fname] = {term for term, _ in top}
    return result


def find_merge_candidates(
    memory_root: Path,
    overlap_threshold: float = OVERLAP_THRESHOLD,
) -> list[MergeCandidate]:
    """Walk memory tree, compute TF-IDF signatures, return pairs whose
    top-term Jaccard overlap exceeds threshold.

    Skips MEMORY.md/README.md, archive subdirs (_LEGACY/_PURGE/_RETIRED), and
    `handoffs/` (see AR-07 below).
    """
    if not memory_root.exists():
        return []

    # Prefix match (deep-audit 2026-07-02): archive folders carry dated
    # suffixes per the rollout rule (e.g. `_LEGACY_pre_20260505_rollout`,
    # `_RETIRED_old`), so an exact-name match let archived duplicates leak into
    # the merge corpus and get proposed for merging. `_processed` stays exact.
    skip_prefixes = ("_LEGACY", "_PURGE", "_RETIRED")
    # AR-07 (2026-07-30): `handoffs` was never excluded, and the operator's
    # queue had accumulated 820 proposals — exactly C(41,2), every possible
    # pairing of 41 session handoffs. They pair with each other because they
    # share the handoff TEMPLATE's boilerplate, not their content.
    #
    # This is a corpus error rather than a threshold error, so raising the
    # threshold would not have been the fix. A handoff is a per-session
    # continuity record: one file per session, keyed on session_id, addressed
    # by that name by `handoff_discoverer` and `handoff_watchdog`. Merging two
    # destroys the key they are addressed by, so no similarity score makes such
    # a proposal correct.
    #
    # Matched on any path part so a relocated or nested memory tree is covered.
    skip_exact = ("_processed", "handoffs")
    corpus: dict[str, Counter] = {}
    for p in memory_root.rglob("*.md"):
        if any(
            part in skip_exact or part.startswith(skip_prefixes)
            for part in p.parts
        ):
            continue
        if p.name in ("MEMORY.md", "README.md"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Strip frontmatter to avoid biasing on metadata fields
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                text = text[end + 5:]
        corpus[p.name] = _tokenize(text)

    tfidf = _compute_tf_idf(corpus)
    signatures = _top_terms_per_file(tfidf, k=20)

    candidates: list[MergeCandidate] = []
    names = sorted(signatures.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            sig_a, sig_b = signatures[a], signatures[b]
            if not sig_a or not sig_b:
                continue
            inter = sig_a & sig_b
            union = sig_a | sig_b
            if not union:
                continue
            jaccard = len(inter) / len(union)
            if jaccard >= overlap_threshold:
                shared = sorted(inter)[:8]
                # Suggest a merged name: common prefix or concatenation
                merged_name = _suggest_merged_name(a, b)
                candidates.append(MergeCandidate(
                    file_a=a,
                    file_b=b,
                    overlap_jaccard=round(jaccard, 3),
                    top_shared_terms=shared,
                    suggested_merged_name=merged_name,
                ))
    # Sort highest-overlap first
    candidates.sort(key=lambda c: -c.overlap_jaccard)
    return candidates


def _suggest_merged_name(a: str, b: str) -> str:
    """Find common filename prefix; fall back to a-merged-b convention."""
    stem_a = a.rsplit(".", 1)[0]
    stem_b = b.rsplit(".", 1)[0]
    # Common prefix
    i = 0
    while i < min(len(stem_a), len(stem_b)) and stem_a[i] == stem_b[i]:
        i += 1
    prefix = stem_a[:i].rstrip("_-")
    if prefix and len(prefix) > 8:
        return f"{prefix}_merged.md"
    return f"{stem_a}_merged_with_{stem_b}.md"


def format_candidates_report(candidates: list[MergeCandidate]) -> str:
    """Operator-facing report for /allostat-tend --merge-candidates."""
    if not candidates:
        return "No merge candidates detected (no pairs with >=35% term-signature overlap)."
    lines = [
        f"=== Auto merge proposer — {len(candidates)} candidate pair(s) ===",
        "",
    ]
    for c in candidates[:15]:
        lines.append(f"  overlap={c.overlap_jaccard:.2f}  {c.file_a} ↔ {c.file_b}")
        lines.append(f"    shared terms: {', '.join(c.top_shared_terms)}")
        lines.append(f"    suggested merged name: {c.suggested_merged_name}")
        lines.append("")
    if len(candidates) > 15:
        lines.append(f"... + {len(candidates) - 15} more pairs.")
    lines.append("Operator decides per-pair: keep separate, merge, or retire one.")
    return "\n".join(lines)
