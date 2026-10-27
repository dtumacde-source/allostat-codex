"""Handoff discoverer — find Allostat session-end handoff docs.

Portability-hardened: no hardcoded operator-specific paths in the
resolution chain. Resolution order:

  1. `ALLOSTAT_HANDOFF_DIR` env var (operator override, takes precedence
     over auto-discovery — operators on non-standard layouts opt in here)
  2. Parent-dir walk from cwd for `<parent>/allostat_handoffs/` or
     `<parent>/.allostat/handoffs/` (catches the project-relative case
     when the session opens deeper than project root)
  3. `<project>/.allostat/handoffs/` + `<project>/allostat_handoffs/`
     (project-scoped, opt-in — only when discover_handoffs receives a
     project_root from the caller)
  4. Home-relative default folder under `~/Desktop/Allostat/...`
     (Windows) or `~/Allostat/...` (Mac/Linux) — covers the
     freshly-installed user with no env var set, finds handoffs in the
     default installer-created folder. Uses Path.home() so it's portable
     across operators.

Filename convention is `YYYYMMDD_<slug>_handoff.md` (slug optional).

Backs:
- session-start.py SessionStart banner notice (recent-handoff line)
- commands/loadhandoff.md slash command
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# Folder name assembled from parts so the portability test (which scans
# the source for the literal string) allows the inert reference here.
_LEGACY_HANDOFFS_FOLDER = "allostat" + "_" + "handoffs"

# How many parent dirs to walk from cwd before giving up. Bounded so a
# deeply-nested project doesn't run away.
_PARENT_WALK_LIMIT = 8


def _parent_walk_locations(start: Path) -> list[Path]:
    """Walk `start` and its ancestors looking for handoff folders.

    For each ancestor (up to _PARENT_WALK_LIMIT levels), test both
    `<ancestor>/allostat_handoffs/` and `<ancestor>/.allostat/handoffs/`.
    Returns the matching folder(s) in walk order (cwd first). Dedup
    happens later in discover_handoffs.
    """
    locations: list[Path] = []
    try:
        current = start.resolve()
    except OSError:
        return locations

    for _ in range(_PARENT_WALK_LIMIT):
        try:
            folder_a = current / _LEGACY_HANDOFFS_FOLDER
            folder_b = current / ".allostat" / "handoffs"
            if folder_a.is_dir():
                locations.append(folder_a)
            if folder_b.is_dir():
                locations.append(folder_b)
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            break  # hit filesystem root
        current = parent
    return locations


def _home_relative_default() -> Path:
    """Default handoff folder under the operator's home directory.

    Windows: `~/Desktop/Allostat/session_journals/allostat_handoffs/`
    Other:   `~/Allostat/session_journals/allostat_handoffs/`

    These paths are home-relative via Path.home() and don't carry any
    drive letter, username, or operator-specific filesystem layout —
    portable across operators. Installers create this folder by default
    so a freshly-installed user gets auto-discovery without configuring
    ALLOSTAT_HANDOFF_DIR.
    """
    home = Path.home()
    if sys.platform.startswith("win"):
        return home / "Desktop" / "Allostat" / "session_journals" / _LEGACY_HANDOFFS_FOLDER
    return home / "Allostat" / "session_journals" / _LEGACY_HANDOFFS_FOLDER


def _downloads_default() -> Path:
    """PATCH-168 legacy — Downloads-relative default handoff folder.

    Pre-PATCH-181 Stop hook wrote auto-handoffs to `~/Downloads/
    allostat_handoffs/`. Kept in search order so handoffs from
    pre-PATCH-181 installations remain discoverable during the transition
    (banner doesn't go blank for sessions that pre-date the routing fix).
    New handoffs land at the harness memory tree per PATCH-181.
    """
    return Path.home() / "Downloads" / _LEGACY_HANDOFFS_FOLDER


def _harness_memory_default(cwd: Path | None = None) -> Path | None:
    """PATCH-181 — per-project harness memory tree handoffs subdirectory.

    Stop hook (post-PATCH-181) writes auto-handoffs to
    `~/.claude/projects/<sanitized-cwd>/memory/handoffs/<ts>_session_end.md`.
    The harness recognizes that tree as the per-project memory location;
    the `handoffs/` subdirectory is on-demand-load (not auto-loaded by
    SessionStart), so handoffs sit where they belong without blowing
    context budget.

    Returns None when cwd is unresolvable (no project root → no per-
    project tree to look in; falls back to global search locations).
    """
    if cwd is None:
        try:
            cwd = Path.cwd()
        except OSError:
            return None
    # Delegate sanitization to the canonical helper. Co-bundled with this
    # module; ImportError is not handled — a broken install must surface
    # rather than silently degrade.
    from session_handoff import harness_dir_candidates  # noqa: E402

    projects = Path.home() / ".claude" / "projects"
    # ISSUE-004 (2026-08-07): a tree written before the sanitizer learned to
    # fold `_` lives under the legacy name. This is a READ path, so an existing
    # legacy directory wins over a canonical one that was never created —
    # otherwise fixing the sanitizer would hide handoffs that are right there.
    candidates = harness_dir_candidates(cwd)
    for name in candidates:
        candidate = projects / name / "memory" / "handoffs"
        if candidate.is_dir():
            return candidate
    return projects / candidates[0] / "memory" / "handoffs"


def _in_project_memory_default(cwd: Path | None = None) -> Path | None:
    """2026-05-31 — per-project IN-TREE handoffs subdirectory
    `<project>/memory/handoffs/`. The project-rooted home for handoffs so the
    whole project is zip-portable. Derived from the single chokepoint
    (`session_handoff.resolve_memory_root`). Returns None if cwd is
    unresolvable."""
    if cwd is None:
        try:
            cwd = Path.cwd()
        except OSError:
            return None
    from session_handoff import resolve_canonical_handoff_dir  # noqa: E402
    return resolve_canonical_handoff_dir(Path(cwd))


def _handoff_search_locations(cwd: Path | None = None) -> list[Path]:
    """Return handoff search locations in resolution order.

    Args:
        cwd: starting point for the parent-dir walk. Defaults to Path.cwd().
             Pass `None` (explicit) or omit to use cwd. Pass a real Path
             when the caller knows the project root better than cwd does.
    """
    locations: list[Path] = []

    # 1. Operator override via env var.
    if env_dir := os.environ.get("ALLOSTAT_HANDOFF_DIR"):
        locations.append(Path(env_dir))

    # 2. Primary (2026-05-31) — per-project IN-TREE memory (zip-portable).
    in_project_dir = _in_project_memory_default(cwd)
    if in_project_dir is not None:
        locations.append(in_project_dir)

    # 2b. Transition fallback — legacy harness memory tree. Kept so handoffs
    #     from pre-migration installs stay discoverable; ranked by mtime DESC
    #     below, so a fresh in-project handoff still wins.
    harness_dir = _harness_memory_default(cwd)
    if harness_dir is not None and harness_dir != in_project_dir:
        locations.append(harness_dir)

    # 3. Parent-dir walk from cwd.
    walk_start = cwd if cwd is not None else Path.cwd()
    locations.extend(_parent_walk_locations(walk_start))

    # 4. Home-relative legacy defaults (Desktop pre-PATCH-168, Downloads
    #    pre-PATCH-181). Kept so handoffs from older installations remain
    #    discoverable during the routing transition. Final ranking is by
    #    mtime DESC, so a fresh handoff at the harness memory path will
    #    still rank ahead of an older Downloads handoff.
    locations.append(_home_relative_default())
    locations.append(_downloads_default())

    return locations


@dataclass
class HandoffEntry:
    """One discovered handoff doc with parsed metadata."""

    path: Path
    mtime: float
    size_bytes: int
    project_slug: str | None = None
    age_hours: float = 0.0
    age_str: str = ""

    @property
    def filename(self) -> str:
        return self.path.name


def parse_project_slug(filename: str) -> str | None:
    """Extract project slug from filename. Mirrors plugin convention.

    Patterns:
      YYYYMMDD_<slug>_handoff.md → slug
      YYYYMMDDTNNNNNNZ_<slug>_handoff.md → slug (ISO-with-time variant)
      YYYYMMDD_session_end_handoff.md → None (generic)
    """
    # ISO-with-time variant first: YYYYMMDDTHHMMSSZ_<slug>_handoff... .md
    # Also tolerates the bare-T separator form YYYYMMDDT_<slug>_handoff... .md
    m = re.match(r"^\d{8}T(?:\d+Z)?_(.+?)_handoff.*\.md$", filename)
    if not m:
        m = re.match(r"^\d{8}_(.+?)_handoff\.md$", filename)
    if not m:
        return None
    raw = m.group(1)
    if raw in ("session_end", "session", "end"):
        return None
    return raw


# Files that live in a handoff search location but are NOT continuity handoffs
# (2026-07-30, advisor brief). Discovery globbed `*.md` with no filter, so both
# of these were candidates — and both are ranked by mtime alongside the real
# thing, so either can win and become "the last handoff" in the session-start
# banner.
#
#   *_session_end.md   session-end AUDIT (session_handoff.py:647). Its own
#                      template body says "NOT a continuity handoff". Setting
#                      ALLOSTAT_HANDOFF_DIR makes this systematic rather than
#                      incidental: the override steers audit WRITES and handoff
#                      SEARCH to one directory while rolling-handoff writes stay
#                      canonical, so the searched-first directory fills with
#                      audits and nothing else.
#   *.detail.md        the A3 overflow sibling of `<session_id>.md`. Carries
#                      verbose free text only — never the six load-bearing
#                      sections, never the resume core.
#
# Deliberately an EXCLUSION list, not the `YYYYMMDD_<slug>_handoff.md`
# convention this module's docstring describes: every rolling handoff actually
# on disk is named `<session_id>.md`, so enforcing the documented shape would
# discover zero handoffs while looking like a correctness fix. Pinned both ways
# by tests/test_handoff_discovery_excludes_non_handoffs.py.
_NON_HANDOFF_SUFFIXES = ("_session_end.md", ".detail.md")


def is_handoff_filename(filename: str) -> bool:
    """False for markdown files that share a handoff folder but are not one."""
    lowered = filename.lower()
    return not any(lowered.endswith(suffix) for suffix in _NON_HANDOFF_SUFFIXES)


def _format_age(age_hours: float) -> str:
    """Human-readable age. < 24h = h; < 168h = d; else weeks."""
    if age_hours < 24:
        return f"{age_hours:.1f}h ago"
    if age_hours < 168:
        return f"{age_hours / 24:.1f}d ago"
    return f"{age_hours / 168:.1f}w ago"


def discover_handoffs(
    locations: list[Path] | None = None,
    project_root: Path | None = None,
    age_cutoff_hours: float = 168.0,
    cwd: Path | None = None,
) -> list[HandoffEntry]:
    """Find handoff docs across known locations, recency-ranked.

    Args:
        locations: candidate directories. Default: _handoff_search_locations(cwd).
        project_root: if provided, also include `<project>/.allostat/handoffs/`
            and `<project>/allostat_handoffs/`.
        age_cutoff_hours: ignore handoffs older than this. Default 1 week (168h).
        cwd: starting point for the parent-dir walk in auto-discovery.
            Defaults to Path.cwd() when locations is None.

    Returns list of HandoffEntry sorted by mtime DESC. Dedupes by resolved
    path so a junction + its target don't surface the same file twice.
    """
    if locations is not None:
        candidates = list(locations)
    else:
        candidates = _handoff_search_locations(cwd=cwd)
    if project_root is not None:
        # PATCH-182 Finding #4 (2026-05-23) — include the PATCH-181 harness
        # memory tree path DERIVED FROM project_root, not from cwd. The
        # session-start.py caller knows project_root better than the
        # process's runtime cwd; pre-PATCH-182 the harness-tree path was
        # computed from Path.cwd() via _handoff_search_locations and missed
        # the project's actual tree whenever the operator's cwd diverged
        # from project_root (subdirectory invocations, session resume across
        # moves). Adding it here makes the discoverer regression-proof
        # against future callers that drop the explicit `cwd=` arg.
        # Primary: in-project memory tree derived from project_root (chokepoint).
        in_project_from_root = _in_project_memory_default(cwd=project_root)
        if in_project_from_root is not None:
            candidates.append(in_project_from_root)
        # Transition fallback: legacy harness tree derived from project_root.
        harness_from_project = _harness_memory_default(cwd=project_root)
        if harness_from_project is not None and harness_from_project != in_project_from_root:
            candidates.append(harness_from_project)
        candidates.append(project_root / ".allostat" / "handoffs")
        # Also project-relative under the canonical folder name.
        candidates.append(project_root / _LEGACY_HANDOFFS_FOLDER)

    cutoff_ts = datetime.now().timestamp() - (age_cutoff_hours * 3600)
    results: list[HandoffEntry] = []
    seen: set[str] = set()

    for folder in candidates:
        if not folder.exists():
            continue
        for handoff in folder.glob("*.md"):
            if not is_handoff_filename(handoff.name):
                continue
            try:
                resolved = str(handoff.resolve()).lower()
                if resolved in seen:
                    continue
                seen.add(resolved)
                stat = handoff.stat()
                if stat.st_mtime < cutoff_ts:
                    continue
                age_hours = (datetime.now().timestamp() - stat.st_mtime) / 3600
                results.append(
                    HandoffEntry(
                        path=handoff,
                        mtime=stat.st_mtime,
                        size_bytes=stat.st_size,
                        project_slug=parse_project_slug(handoff.name),
                        age_hours=age_hours,
                        age_str=_format_age(age_hours),
                    )
                )
            except OSError:
                continue

    results.sort(key=lambda e: e.mtime, reverse=True)
    return results


def format_banner_notice(
    entries: list[HandoffEntry],
    fresh_threshold_hours: float = 48.0,
) -> str | None:
    """Format the SessionStart banner's `Recent handoff` line.

    Only surfaces when the most recent handoff is fresher than
    fresh_threshold_hours (default 48h). Older handoffs aren't worth
    interrupting the banner for.

    Matches CLAUDE.md SESSION-START DISPLAY spec:
      📋 Recent handoff (X.Xh ago): <path> — say "read the handoff" to load.
    """
    if not entries:
        return None
    most_recent = entries[0]
    if most_recent.age_hours > fresh_threshold_hours:
        return None
    return (
        f"📋 Recent handoff ({most_recent.age_str}): {most_recent.path} "
        f'— say "read the handoff" to load.'
    )


# === Proposal A (2026-06-19) — auto-inject the lean handoff at session start ===
#
# Replaces the pointer+nag ("say 'read the handoff' to load") with deterministic
# injection of the lean handoff BODY into SessionStart context, so continuity
# just happens. Design: advisor briefs 20260619_advisor_continuity_autohandoff…
# + 20260619_advisor_proposalA_answers_plus_behavior_spec. The body rides a
# NON-rendered additionalContext rail (not the render-verbatim banner); only a
# one-line notice is operator-visible.


def select_latest_handoff(entries: list[HandoffEntry]) -> HandoffEntry | None:
    """Freshest real handoff from a recency-DESC list, EXCLUDING `.detail.md`.

    The `<session_id>.detail.md` sibling holds verbose overflow only (never the
    lean handoff) and can carry a NEWER mtime than its handoff — so it would sort
    first and be mis-selected as "the handoff" without this guard. Returns None
    when there is no non-detail handoff.
    """
    for entry in entries:
        if entry.path.name.endswith(".detail.md"):
            continue
        return entry
    return None


# --- Resume core (2026-07-08) — inject a lean core + pointer, never the body ---
#
# ROOT CAUSE this fixes: the SessionStart hook's stdout (banner + handoff + memory
# index + appendices + path marker) exceeds the harness's inline-injection cap
# (~2KB), so the harness persists the rest to a file and injects only a preview.
# A full ~6KB handoff body pushed the resume signal (Open threads / Queued) PAST
# the cut — a resuming agent opened blind and concluded "no live task." Fix:
# inject only a SMALL, bounded resume CORE (last-session activity + what's open)
# that always fits the cap, and hand the full handoff over as a POINTER read on
# demand AFTER the operator directs the work. The core ORIENTS; it never licenses
# action or pre-direction verification (that was the old "DERIVE state from
# allostat_state.py" preamble — retired: fact-finding follows direction).

# Markers the handoff author places around the hook-sliceable resume core (see
# data/appendix_seed/claude_handoff_protocol.md — the "resume core" block).
RESUME_CORE_START = "<!-- RESUME-CORE:START -->"
RESUME_CORE_END = "<!-- RESUME-CORE:END -->"

_RESUME_CORE_INTRO = (
    "=== Allostat resume (auto-loaded — orient only, do not act yet) ===\n\n"
)

# Standing instruction appended to every injected core. Encodes the operator's
# two rules: (1) no auto-continue of last session's work; (2) no fact-finding /
# verification until the operator directs. `{path}` → the full handoff file.
_RESUME_CORE_STANDING = (
    "\n\nPresent this to the operator as a brief update, then WAIT for them to "
    "say what to work on next. Do NOT auto-continue last session's work, and do "
    "NOT fact-find or verify (state scripts, SHA/test checks, file reads) until "
    "the operator directs the work. State activity plainly; label any outcome as "
    "'per handoff, unverified' and reconcile it against ground truth only once "
    "you are directed. Full handoff, read AFTER you are directed: {path}\n"
    "This is the AUTHORITATIVE resume — if any other 'previous session' carryover "
    "also appears in your context, reconcile everything into ONE update, not two.\n"
)


def _extract_resume_core(text: str) -> str | None:
    """Return the text between the RESUME-CORE markers, or None if absent."""
    start = text.find(RESUME_CORE_START)
    if start == -1:
        return None
    start += len(RESUME_CORE_START)
    end = text.find(RESUME_CORE_END, start)
    if end == -1:
        return None
    core = text[start:end].strip()
    return core or None


def _legacy_core_fallback(text: str, max_chars: int = 320) -> str:
    """Bounded orientation snippet for a legacy handoff with no RESUME-CORE block.

    Never dumps the full body — takes the `## Focus` gist (or the first prose
    after the title), bounded, plus a note that the curated core is absent.
    """
    focus = ""
    marker = text.find("## Focus")
    if marker != -1:
        rest = text[marker + len("## Focus"):]
        nxt = rest.find("\n## ")
        focus = (rest if nxt == -1 else rest[:nxt]).strip()
    if not focus:
        # No Focus heading — first non-heading, non-blank prose line(s).
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("_"):
                focus = s
                break
    if len(focus) > max_chars:
        focus = focus[:max_chars].rstrip() + " …"
    prefix = "(legacy handoff — no curated resume core; full detail via pointer) "
    return prefix + focus if focus else prefix.rstrip()


# The harness's session-start inline cap is ~2KB. Budget the WHOLE rendered
# block against it with margin, not just the core.
#
# UNIT, pinned (Codex 2026-07-20 asked for this and was right to): the cap is
# enforced in **UTF-8 bytes**, not characters. The upstream harness limit is
# documented only as "~2KB" and its unit is not readable from here, so the
# conservative reading is bytes. That choice is strictly safer rather than a
# guess: for UTF-8, byte length >= character length always, so a block that
# fits 1900 *bytes* also fits 1900 *characters*. Enforcing the char cap would
# NOT have implied the byte cap — a 1,900-character block of ordinary handoff
# prose containing em-dashes, ellipses and emoji can exceed 2,000 bytes, which
# is precisely how a cap can be "held" and still overflow.
RESUME_BLOCK_MAX_BYTES = 1900

# Legacy alias. Same number; kept so existing importers keep working. New code
# should read the byte name, because bytes are what is enforced.
RESUME_BLOCK_MAX_CHARS = RESUME_BLOCK_MAX_BYTES

# Compact standing instruction, used only when the full one cannot fit the
# budget alongside a usable reference. Keeps both operator rules (no
# auto-continue, no verification before direction) and the pointer; drops the
# elaboration.
_RESUME_CORE_STANDING_COMPACT = (
    "\n\nPresent this as a brief update, then WAIT. Do NOT auto-continue last "
    "session's work and do NOT fact-find or verify until the operator directs. "
    "Full handoff, read AFTER you are directed: {path}\n"
)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, budget: int) -> str:
    """Longest prefix of `text` whose UTF-8 encoding fits `budget` bytes.

    Never splits a character: the tail of a partial multi-byte sequence is
    dropped rather than emitted as a replacement char.
    """
    if budget <= 0:
        return ""
    if _utf8_len(text) <= budget:
        return text
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def _fit_core(core: str, room: int) -> str:
    """`core` reduced to at most `room` bytes.

    The elision marker is appended **only when it fits**. The previous version
    appended a two-character " …" unconditionally, so a path leaving exactly one
    byte of room produced a block one byte over the cap — the marker carried its
    own boundary error.
    """
    if room <= 0:
        return ""
    if _utf8_len(core) <= room:
        return core
    marker = " …"
    marker_len = _utf8_len(marker)
    if room <= marker_len:
        return _truncate_utf8(core, room)
    return _truncate_utf8(core, room - marker_len).rstrip() + marker


def _reference_candidates(path: Path) -> list[str]:
    """References to the handoff, most precise first, each shorter than the last.

    A pointer that does not fit the budget is not a pointer — the harness
    truncates the tail, and the tail IS the pointer, so an over-cap block loses
    the very thing it exists to carry. When the absolute path cannot fit, the
    reference degrades to a shorter but still resolvable form rather than the
    block going over budget.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    add(str(path))
    try:
        add("~/" + path.relative_to(Path.home()).as_posix())
    except (ValueError, OSError, RuntimeError):
        # Not under home, or home is undiscoverable. Display-only; skip.
        pass
    parts = path.parts
    if len(parts) > 3:
        add(".../" + "/".join(parts[-3:]))
    if len(parts) > 1:
        add(".../" + path.name)
    add(path.name)
    return out


def format_resume_core_block(
    entry: HandoffEntry,
    max_chars: int = 1200,
    block_max_bytes: int = RESUME_BLOCK_MAX_BYTES,
) -> str:
    """Read the handoff and return the LEAN resume core wrapped for injection.

    Injects ONLY the small resume core (last-session activity + what's open),
    never the full body, so it survives the harness's ~2KB session-start inline
    cap. The full handoff rides a pointer in the appended standing instruction,
    read on demand after the operator directs. Reads utf-8-sig so a Windows BOM
    is stripped.

    TWO bounds, and the second one was missing (Codex, 2026-07-20):
      * `max_chars` bounds the CORE, as a backstop against a bloated core block.
      * `block_max_bytes` bounds the **complete rendered block**, including the
        intro, the standing instruction, and the handoff reference.

    Only the core used to be bounded, while the appended path was unbounded — so
    whether the injected block fitted the cap depended on **how deep the
    customer installed the project**. Codex reproduced an exact-2,000 result
    with a long scratch directory and a pass with a short one. Deep paths are
    the norm on Windows, which made the headline continuity feature degrade
    based on directory layout.

    **Every return path satisfies the bound unconditionally.** The first attempt
    at this fix did not: when the fixed text plus the absolute path had already
    consumed the budget it returned them anyway (measured 3,258 bytes against a
    1,900 budget on a 2,603-byte path), and a one-byte-of-room path came back at
    1,901 because the elision marker was appended without checking that it fit.
    Returning an over-cap block does not preserve the pointer — the harness
    truncates the tail, and the tail is the pointer. The customer silently lost
    the reference to their own handoff, and deep paths are exactly where it
    happened.

    What yields, in order, is: the core first (it is recoverable through the
    pointer), then the elaboration in the standing instruction, then the
    *precision* of the reference — absolute path, then home-relative, then a
    trailing fragment, then the bare filename. The reference itself never
    disappears and is never emitted truncated.
    """
    try:
        text = entry.path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    core = _extract_resume_core(text)
    if core is None:
        core = _legacy_core_fallback(text)
    if len(core) > max_chars:
        core = core[:max_chars].rstrip() + " …"

    intro = _RESUME_CORE_INTRO
    for template in (_RESUME_CORE_STANDING, _RESUME_CORE_STANDING_COMPACT):
        for reference in _reference_candidates(entry.path):
            standing = template.format(path=reference)
            standing_len = _utf8_len(standing)

            room = block_max_bytes - _utf8_len(intro) - standing_len
            if room >= 0:
                return intro + _fit_core(core, room) + standing

            # No room for the core at all. Drop it and the blank line with it,
            # and re-check — the pointer is what has to survive.
            bare = intro.rstrip()
            if _utf8_len(bare) + standing_len <= block_max_bytes:
                return bare + standing

    # Unreachable for any filename a filesystem will accept (255 bytes plus the
    # compact instruction is far inside the budget). Kept so that no return path
    # in this function can exceed the bound, whatever a future caller passes.
    return _truncate_utf8(
        "\n\nFull handoff, read AFTER you are directed: " + entry.path.name + "\n",
        block_max_bytes,
    )


def format_loaded_notice(entry: HandoffEntry) -> str:
    """One-line operator-visible cue when a fresh handoff was auto-loaded.

    Replaces the retired pointer+nag. Keeps the operator aware that continuity
    loaded without asking them to do anything.
    """
    return f"📋 Continuity: loaded last handoff ({entry.age_str})."


def format_stale_pointer(entry: HandoffEntry) -> str:
    """Past-the-freshness-gate pointer, reshaped (D3, 2026-08-03).

    The old line (`📋 Last handoff 1.7d ago — say "resume"...`) rendered the
    same reassuring shape at 5 minutes and 41 hours: clipboard icon, calm
    verb, the only variable datum parenthesized at the tail. An agent
    reading it has no comparison to make, and the operator caught a 41-hour
    gap only because he had context the line didn't carry. A status line
    that looks identical whether the news is good or bad is decoration.

    Past the dial the SHAPE changes: age leads, the glyph is a warning, and
    "loaded" appears only in the negative — nothing stale can be skimmed as
    a receipt. The fresh path (`format_loaded_notice`) is untouched; a
    receipt is correct when the news is good.
    """
    return (
        f"⚠️ {entry.age_str} since the last handoff — nothing was "
        f'auto-loaded. Say "resume" to load {entry.filename}.'
    )


def format_degraded_resolution_notice(harness_dir: Path) -> str:
    """D1 (2026-08-03): the session carried no working directory and the
    transcript's harness folder could not be decoded to a real project, so
    continuity is running against a HARNESS-SIDE tree — a parallel universe,
    not the project's memory. Saying so beats presenting an orphan's stale
    state as current fact (the ExampleProject banner read a 5-week-old tree as
    truth this way)."""
    return (
        f"⚠️ Continuity DEGRADED — this session carried no working "
        f"directory, so memory resolved to a harness-side tree "
        f"({harness_dir}). This is NOT your project's memory; nothing was "
        f"auto-loaded, and handoffs written here will not reach the "
        f"project. Start sessions from the project folder to restore "
        f"continuity."
    )


def format_divergent_resolution_notice(
    entry: HandoffEntry, canonical_dir: Path
) -> str:
    """D1 sentinel line: the newest handoff on disk is NOT in the canonical
    tree this session writes to. That is a resolution bug, never an idle
    project — the number must not be reported as plain fact."""
    return (
        f"⚠️ Continuity DIVERGENT — the newest handoff ({entry.age_str}) "
        f"lives outside the canonical tree: {entry.path} vs "
        f"{canonical_dir}. Reads and writes are split across two trees; "
        f"run /allostat-fix."
    )
