"""Memory tree pruning — S2 Block 4 (2026-05-27).

Archive-not-destroy semantics per operator's 5/27 PM directive:

  "If a user tells Claude that something no longer applies, it should be
  pruned then saved in cold storage. No deletion — we are simply telling
  Claude to ignore unless told to recall."

  "Default 90 days for the average user but adjustable via testing."

  "Sections shouldn't be pruned. If superseded, overwrite not prune."
  → Whole-file granularity only.

Two trigger paths:

  Trigger A — age-based: file mtime older than N days (default 90).
  Trigger B — retirement-language: operator says X no longer applies;
    Claude greps memory tree, surfaces candidates with H1 + description,
    operator confirms PER ITEM, only then archive-move runs. The grep +
    surface + confirm protocol is implemented in
    `surface_retirement_candidates` below — disambiguation is regulated,
    never autonomous file-matching.

Cold storage path: `~/.claude/projects/<cwd>/memory/_archived_<YYYYMMDD>/`
preserving subdirectory structure. Claude does NOT auto-read archives at
SessionStart; archives load on demand via `/recall <topic>` or one of
the assertion-of-loss phrases (see `forgetting_trigger.py`).

Log: `~/.claude/projects/<cwd>/memory/_pruning_log.md` — append-only,
single rolling file. Each entry: timestamp / source path / destination
archive path / trigger (age vs operator-language) / file size.

Eligible file classes (only these can be archive candidates):
  feedback_*.md
  project_*.md
  reference_*.md
  Files in subdirectories: action_plans/, ship_reports/, debugging_logs/,
    audit_reports/, behavioral_audits/, code_audits/, benchmarks/,
    tech_specs/

NEVER touched (no-archive zones, even if matched):
  MEMORY.md (the index itself)
  _PURPOSE.md (orientation)
  user_*.md (operator identity + durable preferences)
  Existing _LEGACY*/ directories (the lifecycle_ladder rollback store)
  Existing _archived_*/ directories
  Existing _PURGE*/ and _RETIRED*/ directories (cold storage / retirement)
  _pruning_log.md (the log itself)
  Silos files (deferred to v3.1+)

Canon docs + advisor-sandbox content live OUTSIDE the scanned memory tree
(under ~/.claude/allostat/projects/), so the pruner never walks them;
_is_eligible_path also returns False for any path outside memory_dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_AGE_DAYS = 90
DEFAULT_PER_PASS_CAP = 20

ELIGIBLE_FILENAME_PREFIXES = ("feedback_", "project_", "reference_")
ELIGIBLE_SUBDIRECTORIES = (
    "action_plans",
    "ship_reports",
    "debugging_logs",
    "audit_reports",
    "behavioral_audits",
    "code_audits",
    "benchmarks",
    "tech_specs",
)

# Filenames + directory prefixes that are NEVER eligible regardless of
# other matching. The exact set per v3.2 advisor §3 + operator directive.
NEVER_TOUCHED_FILENAMES = frozenset({
    "MEMORY.md",
    "_PURPOSE.md",
    "_pruning_log.md",
})
NEVER_TOUCHED_FILENAME_PREFIXES = ("user_",)
# NOTE (2026-07-21): the first entry was `"_LEGACY_"` (trailing underscore),
# which never matched the directory lifecycle_ladder actually creates —
# `LEGACY_SUBDIR = "_LEGACY"`, and `"_LEGACY".startswith("_LEGACY_")` is False.
# A top-level `memory/_LEGACY/` survived only because its parent is not in
# ELIGIBLE_SUBDIRECTORIES, but `legacy_supersede` archives into
# `rule_path.parent/_LEGACY/`, so superseding a rule stored under e.g.
# `action_plans/` produced `action_plans/_LEGACY/` — parts[0] eligible, guard
# not matching — and those rollback archives became prune candidates, moved
# into `_archived_<date>/` where the `_LEGACY -> _PURGE` sweep never looks.
# `_PURGE` and `_RETIRED` were absent entirely and protected only by the same
# accident. `startswith("_LEGACY")` still covers any `_LEGACY_*` variant.
NEVER_TOUCHED_DIR_PREFIXES = (
    "_LEGACY",
    "_archived_",
    "_PURGE",
    "_RETIRED",
    "silos",
)

LOG_FILENAME = "_pruning_log.md"
ARCHIVE_DIR_PREFIX = "_archived_"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PruneCandidate:
    """A file eligible for archive based on the trigger criteria."""
    source_path: Path
    relative_path: str  # path relative to memory_dir
    age_days: float
    size_bytes: int
    trigger: str  # "age" | "retirement_language"
    h1_title: str = ""  # first '# Title' line for display
    description: str = ""  # frontmatter description or first prose line


@dataclass
class PruneResult:
    """Outcome of an archive operation on one candidate."""
    candidate: PruneCandidate
    archive_path: Path
    archived_at: str  # ISO timestamp
    succeeded: bool
    error: str = ""


@dataclass
class RestoreResult:
    """Outcome of restoring one archived file."""
    archive_path: Path
    restored_to: Path
    restored_at: str
    succeeded: bool
    error: str = ""


# ---------------------------------------------------------------------------
# File-class predicates
# ---------------------------------------------------------------------------


def _is_eligible_path(memory_dir: Path, candidate: Path) -> bool:
    """Return True iff candidate is in an eligible class AND not in a
    never-touched class."""
    try:
        rel = candidate.relative_to(memory_dir)
    except ValueError:
        return False
    rel_str = str(rel)
    parts = rel.parts

    # Never-touched directory prefixes anywhere in the path
    for part in parts:
        for prefix in NEVER_TOUCHED_DIR_PREFIXES:
            if part.startswith(prefix):
                return False

    name = candidate.name

    # Never-touched filenames
    if name in NEVER_TOUCHED_FILENAMES:
        return False
    for prefix in NEVER_TOUCHED_FILENAME_PREFIXES:
        if name.startswith(prefix):
            return False

    # Eligibility class 1 — top-level filename prefixes
    if len(parts) == 1:
        for prefix in ELIGIBLE_FILENAME_PREFIXES:
            if name.startswith(prefix):
                return True
        return False

    # Eligibility class 2 — files in eligible subdirectories
    top_dir = parts[0]
    if top_dir in ELIGIBLE_SUBDIRECTORIES:
        return True

    return False


# ---------------------------------------------------------------------------
# Candidate scanners
# ---------------------------------------------------------------------------


def scan_age_candidates(
    memory_dir: Path,
    *,
    age_days: int = DEFAULT_AGE_DAYS,
    max_files: int = DEFAULT_PER_PASS_CAP,
    now: float | None = None,
) -> list[PruneCandidate]:
    """Walk memory tree, return files older than age_days that are in
    eligible classes. Capped at max_files per pass."""
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return []
    now_ts = now if now is not None else time.time()
    age_threshold = now_ts - (age_days * 86400.0)

    candidates: list[PruneCandidate] = []
    for path in sorted(memory_dir.rglob("*.md")):
        if not path.is_file():
            continue
        if not _is_eligible_path(memory_dir, path):
            continue
        try:
            stat = path.stat()
        except OSError:  # noqa: PERF203  # Best-effort: stat() failure on one file shouldn't abort the full scan
            continue
        if stat.st_mtime >= age_threshold:
            continue
        # Eligible + old enough
        h1, desc = _extract_metadata(path)
        candidates.append(PruneCandidate(
            source_path=path,
            relative_path=str(path.relative_to(memory_dir)),
            age_days=(now_ts - stat.st_mtime) / 86400.0,
            size_bytes=stat.st_size,
            trigger="age",
            h1_title=h1,
            description=desc,
        ))
        if len(candidates) >= max_files:
            break
    return candidates


def _extract_metadata(path: Path) -> tuple[str, str]:
    """Read first 50 lines; return (h1_title, description). Best-effort."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "")
    lines = text.splitlines()[:50]
    h1 = ""
    desc = ""
    # H1 from first '# ' line
    for line in lines:
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            h1 = s[2:].strip()
            break
    # Description from frontmatter 'description:' field OR first non-empty
    # non-heading line of prose
    in_frontmatter = False
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0 and s == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if s == "---":
                in_frontmatter = False
                continue
            if s.lower().startswith("description:"):
                desc = s.split(":", 1)[1].strip()
                break
        else:
            if s and not s.startswith("#") and not s.startswith("---"):
                desc = s[:120]
                break
    return (h1, desc)


# ---------------------------------------------------------------------------
# Retirement-language disambiguation (per advisor §4)
# ---------------------------------------------------------------------------


def surface_retirement_candidates(
    memory_dir: Path,
    keywords: list[str],
    *,
    max_results: int = 10,
) -> list[PruneCandidate]:
    """Grep memory_dir for files matching ANY of the keywords. Returns
    candidate list for operator confirmation. NEVER archives autonomously.

    This is the disambiguation gate per advisor §4 (2026-05-27): when
    operator's retirement language fires, the system surfaces candidates
    + operator confirms per-item BEFORE any move happens. Ambiguous
    matches are returned in full so operator can pick or disambiguate
    further.

    Keyword matching is case-insensitive substring AND filename match.
    A file qualifies if (a) its filename contains any keyword, OR (b) its
    H1 title contains any keyword, OR (c) its description contains any
    keyword.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return []
    if not keywords:
        return []
    keywords_lower = [k.lower() for k in keywords if k]
    if not keywords_lower:
        return []

    candidates: list[PruneCandidate] = []
    seen_paths: set[Path] = set()
    for path in sorted(memory_dir.rglob("*.md")):
        if not path.is_file():
            continue
        if path in seen_paths:
            continue
        if not _is_eligible_path(memory_dir, path):
            continue
        name_lower = path.name.lower()
        h1, desc = _extract_metadata(path)
        h1_lower = h1.lower()
        desc_lower = desc.lower()
        # Body search — first ~2KB of content. Surfaces semantic matches
        # operators expect when they say "the typography rule no longer
        # applies" and the rule is mentioned in body prose, not the
        # frontmatter description.
        body_excerpt_lower = ""
        try:
            body_excerpt_lower = path.read_text(
                encoding="utf-8", errors="replace",
            )[:2048].lower()
        except OSError:  # noqa: PERF203  # Best-effort: read failure → skip body match for this file
            pass
        matched = False
        for kw in keywords_lower:
            if (kw in name_lower or kw in h1_lower or kw in desc_lower
                    or kw in body_excerpt_lower):
                matched = True
                break
        if not matched:
            continue
        try:
            stat = path.stat()
        except OSError:  # noqa: PERF203  # Best-effort: stat() failure shouldn't abort the disambiguation scan
            continue
        seen_paths.add(path)
        now_ts = time.time()
        candidates.append(PruneCandidate(
            source_path=path,
            relative_path=str(path.relative_to(memory_dir)),
            age_days=(now_ts - stat.st_mtime) / 86400.0,
            size_bytes=stat.st_size,
            trigger="retirement_language",
            h1_title=h1,
            description=desc,
        ))
        if len(candidates) >= max_results:
            break
    return candidates


def format_candidate_for_operator(c: PruneCandidate) -> str:
    """Build the operator-facing line for a candidate listing.

    Format: relative_path | "H1 title" — description excerpt (age_days days)
    """
    h1 = c.h1_title or "(no title)"
    desc = (c.description or "")[:80]
    age = f"{c.age_days:.0f}d"
    return f"{c.relative_path} | {h1!r} — {desc} ({age})"


# ---------------------------------------------------------------------------
# Archive operations
# ---------------------------------------------------------------------------


def _today_archive_dir(memory_dir: Path, today: str | None = None) -> Path:
    """Return the path for today's archive subdirectory."""
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return memory_dir / f"{ARCHIVE_DIR_PREFIX}{today}"


def archive_candidate(
    memory_dir: Path,
    candidate: PruneCandidate,
    *,
    today: str | None = None,
) -> PruneResult:
    """Move a single candidate to the dated archive. Preserves subdirectory
    structure under the archive dir. Returns PruneResult with succeeded
    flag + error string on failure.

    Idempotent: if the archive path already exists, the function fails
    cleanly with `error="archive_path_exists"` rather than overwriting.
    """
    memory_dir = Path(memory_dir)
    archive_root = _today_archive_dir(memory_dir, today=today)
    archive_path = archive_root / candidate.relative_path
    archived_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not candidate.source_path.is_file():
        return PruneResult(
            candidate=candidate,
            archive_path=archive_path,
            archived_at=archived_at,
            succeeded=False,
            error="source_missing",
        )

    if archive_path.exists():
        return PruneResult(
            candidate=candidate,
            archive_path=archive_path,
            archived_at=archived_at,
            succeeded=False,
            error="archive_path_exists",
        )

    try:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate.source_path), str(archive_path))
    except OSError as e:
        return PruneResult(
            candidate=candidate,
            archive_path=archive_path,
            archived_at=archived_at,
            succeeded=False,
            error=str(e),
        )

    return PruneResult(
        candidate=candidate,
        archive_path=archive_path,
        archived_at=archived_at,
        succeeded=True,
    )


def restore_archive_pass(
    memory_dir: Path,
    archive_date: str,
) -> list[RestoreResult]:
    """Restore every file in a dated archive directory back to its original
    location in memory_dir. Files restored at their relative-path roots.

    Idempotent: if a file already exists at the original location, skip
    (don't overwrite the operator's current file).
    """
    memory_dir = Path(memory_dir)
    archive_root = memory_dir / f"{ARCHIVE_DIR_PREFIX}{archive_date}"
    if not archive_root.is_dir():
        return []

    results: list[RestoreResult] = []
    restored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for path in sorted(archive_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(archive_root)
        original = memory_dir / rel
        if original.exists():
            results.append(RestoreResult(
                archive_path=path,
                restored_to=original,
                restored_at=restored_at,
                succeeded=False,
                error="original_exists",
            ))
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(original))
        except OSError as e:
            results.append(RestoreResult(
                archive_path=path,
                restored_to=original,
                restored_at=restored_at,
                succeeded=False,
                error=str(e),
            ))
            continue
        results.append(RestoreResult(
            archive_path=path,
            restored_to=original,
            restored_at=restored_at,
            succeeded=True,
        ))
    return results


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------


def append_log_entry(memory_dir: Path, result: PruneResult) -> None:
    """Append one entry to the rolling _pruning_log.md."""
    memory_dir = Path(memory_dir)
    log_path = memory_dir / LOG_FILENAME
    header = (
        "# Pruning Log\n\n"
        "Append-only record of every archive operation. Restore via "
        "`/allostat tend prune-restore <YYYYMMDD>` or manually via `mv`.\n\n"
        "---\n\n"
    )
    if not log_path.exists():
        log_path.write_text(header, encoding="utf-8")

    rel_source = result.candidate.relative_path
    rel_archive = (
        str(result.archive_path.relative_to(memory_dir))
        if result.archive_path.is_relative_to(memory_dir)
        else str(result.archive_path)
    )
    entry = (
        f"- **{result.archived_at}** "
        f"(trigger: `{result.candidate.trigger}`, "
        f"age: {result.candidate.age_days:.0f}d, "
        f"size: {result.candidate.size_bytes}B)\n"
        f"  - source: `{rel_source}`\n"
        f"  - archive: `{rel_archive}`\n"
        f"  - status: {'archived' if result.succeeded else f'FAILED ({result.error})'}\n"
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry)


def read_log(memory_dir: Path) -> str:
    """Read the full pruning log. Returns empty string if absent."""
    log_path = Path(memory_dir) / LOG_FILENAME
    if not log_path.is_file():
        return ""
    try:
        return log_path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Forgetting-load trigger detection (per advisor §2, narrowed set)
# ---------------------------------------------------------------------------


# Narrow assertion-of-loss patterns per advisor 5/27 §2. Question-style
# phrases ("remind me of X", "did we discuss X", "what was the call on
# X", "where did we land on X") are NOT triggers — they over-fire on
# routine conversation.
FORGETTING_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"\byou're\s+forgetting\b",
        r"\byou\s+don'?t\s+remember\b",
        r"\byou\s+used\s+to\s+know\b",
        r"\bhave\s+you\s+forgotten\b",
        r"\byou\s+knew\b.+\bbefore\b",
        r"\bi\s+told\s+you\s+about\b.+\bbefore\b",
    )
)


def detect_forgetting_trigger(prompt: str) -> bool:
    """Return True iff the prompt matches one of the narrow assertion-of-loss
    patterns. False for question-style "did we discuss X" / "remind me of
    X" — those are NOT triggers per the v3.2 plan §2 (advisor 5/27).
    """
    if not prompt or not isinstance(prompt, str):
        return False
    for pattern in FORGETTING_TRIGGER_PATTERNS:
        if pattern.search(prompt):
            return True
    return False


# ---------------------------------------------------------------------------
# Retirement-language trigger detection
# ---------------------------------------------------------------------------


# Phrases that suggest operator is retiring a rule / saying X no longer
# applies. Triggers the disambiguation protocol (grep + surface candidates).
RETIREMENT_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"\bno\s+longer\s+applies\b",
        r"\bwe\s+don'?t\s+do\b.+\banymore\b",
        r"\bretire\s+(that|the)\s+(rule|feedback|memory)\b",
        r"\bthis\s+is\s+stale\b",
        r"\bnot\s+applicable\s+anymore\b",
    )
)


def detect_retirement_language(prompt: str) -> bool:
    """Return True iff the prompt matches retirement-language patterns."""
    if not prompt or not isinstance(prompt, str):
        return False
    for pattern in RETIREMENT_TRIGGER_PATTERNS:
        if pattern.search(prompt):
            return True
    return False


def extract_keywords_for_disambiguation(prompt: str) -> list[str]:
    """Pull content-word tokens from prompt for the disambiguation grep.

    Strips trigger-language stopwords + common short words; returns words
    likely to be the SUBJECT of the operator's retirement statement.
    """
    if not prompt:
        return []
    # Lowercase + split on non-word
    tokens = re.findall(r"[a-z][a-z0-9_-]{2,}", prompt.lower())
    stopwords = frozenset({
        "the", "and", "for", "with", "this", "that", "from", "have", "you",
        "are", "was", "were", "but", "not", "any", "all", "your", "their",
        "rule", "rules", "feedback", "memory", "applies", "anymore", "longer",
        "retire", "retired", "stale", "applicable", "dont", "don", "doesn",
        "isn", "doesnt", "we", "us", "they", "them", "to", "in", "on", "at",
        "of", "is", "be", "by", "as", "an", "no", "yes",
    })
    return [t for t in tokens if t not in stopwords and len(t) >= 3]


def compose_trigger_nudge(prompt: str | None) -> str:
    """W11 (pillar-wiring 2026-06-05) — autonomic memory-lifecycle routing.

    The retirement/forgetting trigger detectors above existed but nothing called
    them, so the "automatic routing" the /allostat-prune + /recall docs claim was
    never autonomic. Wired into user-prompt-submit.py, this turns a detected
    trigger into a routing nudge surfaced to the operator:

      - retirement language ("retire that rule", "no longer applies") →
        `/allostat-prune` to review candidates. DISAMBIGUATION ONLY — nothing is
        deleted without the operator's confirmation (the prune skill's two-path
        protocol). Surfaces the extracted subject keywords for the grep.
      - forgetting language ("you're forgetting X", "you don't remember") →
        `/recall` to search handoffs + pruning archives before re-deriving.

    Returns "" when neither fires (routine + question-style prompts don't trigger
    — the detectors are deliberately narrow to avoid over-firing). Retirement
    takes precedence over forgetting when both somehow match.
    """
    if not prompt or not isinstance(prompt, str):
        return ""
    if detect_retirement_language(prompt):
        kws = extract_keywords_for_disambiguation(prompt)
        subject = ", ".join(kws[:5]) if kws else "the topic you named"
        return (
            "[allostat / recall-silos] retirement language detected — "
            f"candidates for: {subject}. Run `/allostat-prune` to review what to "
            "retire (disambiguation first — nothing is deleted without your ok)."
        )
    if detect_forgetting_trigger(prompt):
        kws = extract_keywords_for_disambiguation(prompt)
        subject = " ".join(kws[:5]) if kws else "that"
        return (
            "[allostat / recall-silos] you indicated prior context was lost — "
            f"`/recall {subject}` searches handoffs + pruning archives before "
            "re-deriving it."
        )
    return ""


# ---------------------------------------------------------------------------
# CLI entry — thin argparse dispatch over the engine fns above.
# ---------------------------------------------------------------------------
#
# Before this block, the engine (scan_age_candidates / archive_candidate /
# restore_archive_pass) was fully implemented but had NO __main__, so
# `commands/allostat-prune.md` could only NARRATE manual grep/move steps.
# This dispatch lets the slash command run `python lib/pruning.py <verb>`
# for real. It mirrors record_migration._cli's pattern: resolve the active
# project's memory tree from cwd when no positional is given. The engine
# fns are NOT modified — this is a pure wiring layer.


def _resolve_memory_dir_from_cwd() -> Path | None:
    """Resolve the active project's memory tree from cwd.

    Walks up for the `.allostat`-rooted project (the same root local-state
    uses), then asks the canonical memory-root chokepoint where that
    project's memory tree lives. Returns None when no project root is
    found so the CLI can print a usable error.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from local_state import resolve_project_root  # noqa: E402
    from session_handoff import resolve_memory_root  # noqa: E402

    project_root = resolve_project_root() or Path.cwd()
    return resolve_memory_root(project_root)


def _print_candidates(candidates: list[PruneCandidate], *, header: str) -> None:
    """Print an operator-facing candidate listing."""
    print(header)
    if not candidates:
        print("  (no candidates)")
        return
    for c in candidates:
        print(f"  {format_candidate_for_operator(c)}")


def _cli(argv: list[str] | None = None) -> int:
    """python "$ALLOSTAT_PLUGIN_DIR/lib/pruning.py" <verb> [memory_dir] [opts]

    Subverbs (each drives an existing engine fn):
      preview [memory_dir] [--age-days N] [--max-files N]
          -> scan_age_candidates; lists candidates, MOVES NOTHING.
      execute [memory_dir] [--age-days N] [--max-files N]
          -> scan_age_candidates + archive_candidate + append_log_entry;
             moves age-triggered candidates to _archived_<YYYYMMDD>/.
      restore <YYYYMMDD> [memory_dir]
          -> restore_archive_pass; reverses one archive pass.

    memory_dir is optional; when omitted it is resolved from the active
    project's cwd (the .allostat-rooted project's memory tree).
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="pruning")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_preview = sub.add_parser("preview", help="list age candidates; no move")
    p_preview.add_argument("memory_dir", nargs="?", default=None)
    p_preview.add_argument("--age-days", type=int, default=DEFAULT_AGE_DAYS)
    p_preview.add_argument("--max-files", type=int, default=DEFAULT_PER_PASS_CAP)

    p_execute = sub.add_parser("execute", help="archive age candidates")
    p_execute.add_argument("memory_dir", nargs="?", default=None)
    p_execute.add_argument("--age-days", type=int, default=DEFAULT_AGE_DAYS)
    p_execute.add_argument("--max-files", type=int, default=DEFAULT_PER_PASS_CAP)

    p_restore = sub.add_parser("restore", help="restore one archive pass")
    p_restore.add_argument("archive_date", help="YYYYMMDD of the pass to restore")
    p_restore.add_argument("memory_dir", nargs="?", default=None)

    args = parser.parse_args(argv)

    # Resolve the memory tree: explicit positional wins; else resolve from cwd.
    memory_dir_arg = getattr(args, "memory_dir", None)
    if memory_dir_arg:
        memory_dir = Path(memory_dir_arg)
    else:
        memory_dir = _resolve_memory_dir_from_cwd()
    if memory_dir is None:
        print(
            "ERROR: could not resolve memory dir from cwd; pass it as a "
            "positional arg (python lib/pruning.py <verb> <memory_dir>)",
            file=sys.stderr,
        )
        return 2
    if not memory_dir.is_dir():
        print(f"ERROR: memory dir not found: {memory_dir}", file=sys.stderr)
        return 2

    if args.verb == "preview":
        candidates = scan_age_candidates(
            memory_dir, age_days=args.age_days, max_files=args.max_files,
        )
        _print_candidates(
            candidates,
            header=(
                f"=== prune preview ({len(candidates)} candidate(s), "
                f">{args.age_days}d) — {memory_dir} ==="
            ),
        )
        print("\nNo files moved. Run `execute` to archive these.")
        return 0

    if args.verb == "execute":
        candidates = scan_age_candidates(
            memory_dir, age_days=args.age_days, max_files=args.max_files,
        )
        if not candidates:
            print(f"No age candidates (>{args.age_days}d) in {memory_dir}.")
            return 0
        archived = 0
        failed = 0
        for candidate in candidates:
            result = archive_candidate(memory_dir, candidate)
            append_log_entry(memory_dir, result)
            if result.succeeded:
                archived += 1
                print(
                    f"  archived  {candidate.relative_path} -> "
                    f"{result.archive_path.relative_to(memory_dir)}"
                )
            else:
                failed += 1
                print(
                    f"  FAILED    {candidate.relative_path} ({result.error})",
                    file=sys.stderr,
                )
        print(
            f"\nArchived {archived} file(s)"
            + (f", {failed} failed" if failed else "")
            + f". Log: {LOG_FILENAME}. Restore with "
            f"`python lib/pruning.py restore <YYYYMMDD>`."
        )
        return 0 if failed == 0 else 1

    if args.verb == "restore":
        results = restore_archive_pass(memory_dir, args.archive_date)
        if not results:
            print(
                f"No archive pass at _archived_{args.archive_date}/ in "
                f"{memory_dir}."
            )
            return 0
        restored = sum(1 for r in results if r.succeeded)
        skipped = [r for r in results if not r.succeeded]
        for r in results:
            if r.succeeded:
                print(f"  restored  {r.restored_to.relative_to(memory_dir)}")
            else:
                print(
                    f"  skipped   {r.restored_to.relative_to(memory_dir)} "
                    f"({r.error})"
                )
        print(
            f"\nRestored {restored} file(s)"
            + (f", {len(skipped)} skipped" if skipped else "")
            + "."
        )
        return 0

    # argparse(required=True) guarantees a known verb; defensive only.
    parser.error(f"unknown verb: {args.verb}")  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    import sys

    sys.exit(_cli())
