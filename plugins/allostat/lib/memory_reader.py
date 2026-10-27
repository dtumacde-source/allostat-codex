"""Allostat memory reader — PATCH-138 / v0.6.0 Slice 5.

Read-only pillar accessor that pulls operator-memory context for the
post-tool-use hook to interpolate INTO pillar nudges CLIENT-SIDE.

Privacy lock (per advisor brief 2026-05-20 §1.2 + ship contract
pillar_post_processing_contract.md):
  - Memory CONTENT is read locally only
  - ZERO snippet ever crosses to server
  - Server returns abstract nudge metadata (template ID + interpolation
    targets); wrapper reads the operator's memory, extracts the snippet
    via this module, interpolates into final nudge text BEFORE rendering
    to operator

Ported from the v2.3 plugin's memory_reader.py into the v2.4 wrapper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from refined_memory_policy import REFINED_TOPICAL_MEMORY_CONTRACT


VOICE_REFERENCE_FILENAMES = (
    "operator_prose.md",
    "operator_voice.md",
    "voice_reference.md",
    "prose.md",
)


@dataclass
class ProjectMemorySnapshot:
    memory_index_text: str = ""
    project_files: list[Path] = field(default_factory=list)
    feedback_files: list[Path] = field(default_factory=list)
    voice_reference_path: Path | None = None
    memory_root: Path | None = None


def _safe_mtime(p: Path) -> float:
    """File mtime, or 0.0 if it can't be stat'd (so a vanished/locked file
    sorts last instead of breaking the sort)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def read_project_memory(memory_root: Path) -> ProjectMemorySnapshot:
    """Return a snapshot of the operator's project memory tree.

    Args:
        memory_root: path to ~/.claude/projects/<sanitized-cwd>/memory/

    Returns:
        ProjectMemorySnapshot with file lists + index text + voice path.
    """
    snap = ProjectMemorySnapshot()
    if not memory_root.is_dir():
        return snap
    snap.memory_root = memory_root

    index = memory_root / "MEMORY.md"
    if index.exists():
        try:
            snap.memory_index_text = index.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            pass

    try:
        # Sort project files most-recent-first: glob order is filesystem-
        # arbitrary (nondeterministic across platforms), but consumers such as
        # nudge_context_from_memory document "the most-recent project_*.md's
        # first locked decision." mtime desc, filename as a stable tiebreak.
        project_files = [
            p for p in memory_root.glob("project_*.md") if p.is_file()
        ]
        project_files.sort(key=lambda p: (-_safe_mtime(p), p.name))
        snap.project_files.extend(project_files)
        for p in memory_root.glob("feedback_*.md"):
            if p.is_file():
                snap.feedback_files.append(p)
    except OSError:
        pass

    snap.voice_reference_path = get_voice_reference_path(memory_root)
    return snap


def get_voice_reference_path(memory_root: Path) -> Path | None:
    if not memory_root.is_dir():
        return None
    for name in VOICE_REFERENCE_FILENAMES:
        candidate = memory_root / name
        if candidate.is_file():
            return candidate
    try:
        for p in memory_root.iterdir():
            if not p.is_file():
                continue
            if p.suffix != ".md":
                continue
            if p.name.endswith("_prose.md") or p.name.endswith("_voice.md"):
                return p
    except OSError:
        pass
    return None


# B1 (2026-06-26) — core-view formatter. Hard cap so the injected memory index
# has a context backstop.
#
# Raised 8000 -> 12000 on 2026-08-07, deliberately. Until then `## Project` and
# `## Reference` collapsed to a bare count, so the cap only ever had to cover
# the behavioral layer. Now every section carries its entries (bounded by
# _SECTION_ENTRY_CAP below), and the cap has to cover them without truncating
# the sections it was just taught to include — truncation runs from the end,
# and Project/Reference sit at the end. The real bound on growth is the
# per-section cap, not this; this is the backstop.
_CORE_INDEX_MAX_CHARS = 12000

# Sections that cap their entry count in the core view. Behavioral rules
# (## Feedback, ## PURPOSE, ## THE LAW, anything not listed here) are always-on
# and stay complete — lazy-loading them reinstates the drift they prevent.
_CAPPED_SECTIONS = ("Project", "Reference", "Other")

# Entries per capped section in the core view.
#
# This replaces the collapse-to-a-count that shipped from 2026-06-26 to
# 2026-08-07 (ISSUE-006). Benchmarked on 199 questions against a tree Allostat
# had written itself: with the count, a fresh session answered 7 of 199 and
# volunteered that no such memory existed; the descriptions Allostat had
# already authored and stored — and then deleted on the way to the agent —
# were the missing cue. The saving was inverted too: the block spent ~150
# tokens on boilerplate to save ~40 tokens of payload.
#
# A count is not a cue. An agent reads on demand only when something suggests
# a demand, and "Project (1)" suggests nothing — which is why abstaining was
# the rational move. So entries ship, and the cap bounds the cost instead.
_SECTION_ENTRY_CAP = 20

# Share of the injected core reserved for the cue sections when the whole
# exceeds the cap. The behavioral layer is uncapped by design and on a
# long-lived tree it will fill the budget on its own; without a floor it
# squeezes Project/Reference back down to nothing, which is the defect this
# change exists to remove.
_CAPPED_SECTION_BUDGET_SHARE = 0.4

# The ordered session-start procedure (P3, 2026-08-07).
#
# What the benchmark actually established: a one-line instruction to read the
# memory files outscored the product's own surfacing tenfold (F1 0.52 against
# 0.05). The imperative is the mechanism that works. The catalogue is not —
# handing over the index AND the instruction scored 0.40, WORSE than the
# instruction alone, because a list is read as exhaustive and the model stops
# exploring once it thinks it has seen everything.
#
# So this is a PROCEDURE, not an inventory: bounded, ordered, with a stopping
# condition, and it does not grow with the tree. Two things it deliberately is
# not:
#
#   - It is not "read all your memory files." That was measured: it buys
#     recall by spending calibration (it declined only 36 of 45 adversarial
#     questions against 43 for the gentler arms — it read widely, felt
#     well-informed, and stopped abstaining when it should have) and it cost
#     ~85k context tokens per query against ~26k. Step 4 scopes the read to
#     the leaf nearest the question instead.
#   - It is not a rule wall. It is fixed at these lines. If it starts
#     accumulating clauses, it has become the thing it replaced.
#
# Step 5 is the abstention clause, and it is load-bearing: it exists to claw
# back the calibration the read order costs. It ships only if measurement says
# it does.
_ORIENTATION_PROCEDURE = """HOW TO USE THIS MEMORY — do this once, in order, before answering from it:
1. Read `_PURPOSE.md` and any standing-rule files named below, if present — they set what matters in this project.
2. Read the most recent handoff, if one is present — it says where the last session stopped.
3. What follows is a MAP, not an inventory: each line is a one-line cue, and the leaf file beside it holds the detail.
4. Before saying you don't know, or that nothing here covers it, open the leaf whose description is nearest the question and look.
5. If the memory files genuinely don't contain the answer, say so plainly. Do not fill the gap with a guess.""" + "\n\n" + REFINED_TOPICAL_MEMORY_CONTRACT

# B2 (G6 — keep behavioral rules individually addressable, tightened): each
# behavioral entry keeps its `[title](file)` link but the post-em-dash hook tail
# trims to this many words. The full hook stays in the leaf file frontmatter
# (lossless); the core just carries enough to recognize the rule.
_BEHAVIORAL_HOOK_MAX_WORDS = 8

# Long human titles dominate the always-on layer's size. The leaf FILENAME is
# the canonical addressable id (every rule stays named); the human title in the
# core caps to this many chars so the behavioral layer fits the session-start
# budget. The full title stays in MEMORY.md on disk (lossless).
_BEHAVIORAL_TITLE_MAX_CHARS = 50


def _trim_link_title(head: str, max_chars: int = _BEHAVIORAL_TITLE_MAX_CHARS) -> str:
    """Trim the `[title]` portion of a `- [title](file.md)` head to max_chars,
    leaving the `(file.md)` link target intact so the rule stays addressable."""
    import re
    m = re.match(r"^(\s*[-*+]\s*\**\[)(.*?)(\]\([^)]*\.md\)\**.*)$", head, re.DOTALL)
    if not m:
        return head
    prefix, title, suffix = m.group(1), m.group(2), m.group(3)
    if len(title) <= max_chars:
        return head
    return prefix + title[:max_chars].rstrip() + "…" + suffix


def _compress_behavioral_line(line: str, max_hook_words: int = _BEHAVIORAL_HOOK_MAX_WORDS) -> str:
    """B2 — trim the hook tail of a behavioral bullet line to max_hook_words,
    preserving the leading indent and the `[title](file)` link verbatim so the
    rule stays individually addressable. Non-entry lines (not a bullet, or no
    recognizable hook tail) pass through unchanged.

    The hook tail is whatever follows the closing `)` of the `(file)` link, then
    the first separator (em-dash `—` or a spaced hyphen ` - `). Handles both the
    plain `- [t](f) — hook` and bold `- **[t](f)** — hook` / `- **[t](f)** - hook`
    entry forms found in the live index.
    """
    stripped = line.lstrip()
    if not stripped.startswith(("- ", "* ", "+ ")):
        return line
    indent = line[: len(line) - len(stripped)]

    # Locate the LINK close — the `)` that closes `](<file>.md)`. Titles can
    # contain their own parens/em-dashes (e.g. "(2026-05-18 P3 standing rule)"),
    # so we must anchor on the `.md)` link terminator, NOT the first `)`.
    import re
    m = re.search(r"\]\([^)]*\.md\)", stripped)
    if not m:
        return line
    link_close = m.end() - 1  # index of the closing ")"
    head = stripped[: link_close + 1]
    rest = stripped[link_close + 1:]  # e.g. "** — hook" or " — hook" or " - hook"

    # Always trim an over-long human title (filename stays intact → still named).
    head = _trim_link_title(head)

    # Find the first separator in the rest: em-dash, or a spaced hyphen.
    em = rest.find("—")
    hy = rest.find(" - ")
    candidates = [c for c in (em, hy) if c >= 0]
    if not candidates:
        # No hook tail — keep the (possibly title-trimmed) head as the entry.
        return f"{indent}{head}{rest.rstrip()}"
    sep_idx = min(candidates)
    sep_len = 1 if rest[sep_idx] == "—" else 3  # "—" vs " - "
    head_extra = rest[:sep_idx]  # bold-close markers etc. between link and sep
    hook = rest[sep_idx + sep_len:]

    words = hook.strip().split()
    if len(words) > max_hook_words:
        hook_render = " ".join(words[:max_hook_words]).rstrip(".,;: ") + "…"
    else:
        hook_render = hook.strip()
    return f"{indent}{head}{head_extra.rstrip()} — {hook_render}"


def _count_entry_lines(section_body: str) -> int:
    """Count bullet entry lines (`- ...`) in a collapsed section body."""
    return sum(
        1 for ln in section_body.splitlines()
        if ln.lstrip().startswith(("-", "*", "+"))
    )


def _is_entry_line(line: str) -> bool:
    return line.lstrip().startswith(("- ", "* ", "+ "))


def _entry_target_mtime(line: str, memory_root: Path) -> float:
    """mtime of the leaf an entry line links to, for newest-first ordering.

    An entry whose target can't be resolved or stat'd sorts oldest, so a
    broken link is the first thing dropped when a section is over its cap —
    never a live leaf.
    """
    m = re.search(r"\]\(([^)]*\.md)\)", line)
    if not m:
        return 0.0
    target = m.group(1).strip().replace("\\", "/").lstrip("./")
    return _safe_mtime(memory_root / target)


def _render_section_entries(
    body_lines: list[str],
    memory_root: Path,
    cap: int | None,
) -> tuple[list[str], int, int]:
    """Render one section's body: its entry lines, compressed, newest-first-capped.

    Returns (rendered_lines, total_entries, shown_entries). Non-entry lines in
    the body (blank lines, prose placeholders such as `_(none yet)_`) are
    dropped — they are packaging, and packaging is what was crowding out the
    payload.
    """
    entries = [ln for ln in body_lines if _is_entry_line(ln)]
    total = len(entries)
    shown = entries
    if cap is not None and total > cap:
        # Newest `cap` entries, then restored to the index's own order so the
        # section still reads the way the operator wrote it.
        keep = set(
            id(ln)
            for ln in sorted(
                entries, key=lambda ln: _entry_target_mtime(ln, memory_root), reverse=True
            )[:cap]
        )
        shown = [ln for ln in entries if id(ln) in keep]
    return [_compress_behavioral_line(ln) for ln in shown], total, len(shown)


def _render_core_index(
    memory_text: str,
    memory_root: Path,
    max_chars: int = _CORE_INDEX_MAX_CHARS,
) -> str:
    """Render the injected core view of MEMORY.md — every section carries cues.

    The behavioral layer (## PURPOSE, ## THE LAW, ## Feedback — anything not in
    `_CAPPED_SECTIONS`) injects complete: those rules are always-on and
    lazy-loading them reinstates the drift they prevent. `## Project`,
    `## Reference` and `## Other` inject their entries too, capped at
    `_SECTION_ENTRY_CAP` newest, with a header saying how many exist and how
    many are shown.

    Until 2026-08-07 those three collapsed to `## <Name> (N) — full list in
    MEMORY.md (read on demand)` from the very first file. Measured consequence
    (199 questions, a tree Allostat wrote itself): 7 answers out of 199, and
    the agent asserting no such memory existed. The identical leaf re-filed
    under `## Feedback` — same content, same tree, same question — was answered
    correctly and immediately. The collapse was the whole difference.

    Empty sections and prose placeholders are dropped from the payload: the
    same measurement found the block preserving ~150 tokens of packaging while
    deleting the ~40 tokens of payload that made retrieval possible.

    Caveat #1 (advisor signoff, NON-NEGOTIABLE): an explicit on-disk pointer line
    is appended — agents don't read files they don't know exist. If the rendered
    core exceeds max_chars, it is truncated WITH the pointer preserved.

    Lossless: nothing is dropped from MEMORY.md on disk; this only changes what
    is INJECTED. The full list stays a Read away at the pointer path.
    """
    on_disk_path = memory_root / "MEMORY.md"
    pointer_line = (
        f"\n> Full index on disk: `{on_disk_path}`. Every line above is a CUE, "
        "not the content — open the leaf whose description is nearest your "
        "question.\n"
    )

    lines = memory_text.splitlines()
    n = len(lines)

    # Collapse the leading PREAMBLE — the orientation prose between the H1 title
    # and the first `## ` section header. It's non-load-bearing narration (tree
    # description, consolidation notes, repo list) that costs hundreds of chars
    # every session; the behavioral RULES live in the sections below. Keep the
    # H1 title (+ any first-line title) and replace the rest of the preamble
    # with a one-line on-disk note. Lossless — full preamble stays in MEMORY.md.
    first_section = next(
        (k for k, ln in enumerate(lines)
         if ln.lstrip().startswith("## ") and not ln.lstrip().startswith("### ")),
        None,
    )
    out: list[str] = []
    # Index into `out` where the capped (cue) sections begin, so the two parts
    # can be budgeted independently if the whole exceeds max_chars. len(out)
    # when no capped section renders at all.
    first_capped_line: int | None = None
    i = 0
    if first_section is not None and first_section > 0:
        # Keep the leading H1 title line(s) only (up to the first blank line).
        kept_preamble: list[str] = []
        for k in range(first_section):
            if lines[k].startswith("# ") and not lines[k].startswith("## "):
                kept_preamble.append(lines[k])
            elif kept_preamble and not lines[k].strip():
                break
        out.extend(kept_preamble)
        out.append(
            "_(orientation preamble omitted from the injected core — read the "
            "full MEMORY.md on disk)_"
        )
        i = first_section

    while i < n:
        line = lines[i]
        stripped = line.strip()
        # Is this a capped top-level section header?
        capped_name = None
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            for name in _CAPPED_SECTIONS:
                # Match "## Project" / "## Project (8)" / "## Reference ..." etc.
                if heading == name or heading.startswith(name + " ") or heading.startswith(name + "("):
                    capped_name = name
                    break
        if capped_name is not None:
            # Gather this section's body up to the next top-level "## " header.
            j = i + 1
            body_lines: list[str] = []
            while j < n:
                nxt = lines[j].lstrip()
                if nxt.startswith("## ") and not nxt.startswith("### "):
                    break
                body_lines.append(lines[j])
                j += 1
            rendered, total, shown = _render_section_entries(
                body_lines, memory_root, _SECTION_ENTRY_CAP
            )
            i = j
            if total == 0:
                # An empty section is a placeholder, and a placeholder is
                # packaging. Drop it rather than spend the payload on it.
                continue
            if first_capped_line is None:
                first_capped_line = len(out)
            if shown < total:
                out.append(
                    f"## {capped_name} — newest {shown} of {total}; the rest "
                    "are in MEMORY.md on disk"
                )
            else:
                out.append(f"## {capped_name}")
            out.append("")
            out.extend(rendered)
            continue
        # Behavioral layer (everything not in a capped section): keep the
        # rule individually addressable but tighten the hook tail (B2 / G6).
        out.append(_compress_behavioral_line(line))
        i += 1

    split_at = len(out) if first_capped_line is None else first_capped_line
    behavioral_text = "\n".join(out[:split_at]).rstrip()
    capped_text = "\n".join(out[split_at:]).rstrip()
    core = "\n".join(filter(None, (behavioral_text, capped_text))) + "\n" + pointer_line
    if len(core) <= max_chars:
        return core

    # Over budget. Truncation used to cut the TAIL, which is where Project and
    # Reference live — so on a tree with a large behavioral layer the cue
    # sections silently vanished, reproducing the exact invisibility the
    # section-collapse fix removed, by a different mechanism. Measured on the
    # operator's own 423-file tree: Reference rendered zero entries.
    #
    # So neither part may be starved by the other's growth. The cue sections
    # get a reserved floor; the behavioral layer takes the rest and is the one
    # that truncates, because a rule cut here is still individually named on
    # disk one Read away, whereas a cue cut here removes the reason to Read at
    # all. Whichever side is trimmed, the trim is ANNOUNCED — a silently
    # shortened index reads as a complete one, and that is the ISSUE-008 trap.
    marker = "\n... [truncated — full index on disk]\n"
    # Room for a marker on BOTH parts: either can be the one that overflows,
    # and budgeting for one lets the cap be exceeded by the other's marker.
    budget = max_chars - len(pointer_line) - (2 * len(marker))
    if budget < 0:
        return core[:max_chars]

    capped_share = min(len(capped_text), int(budget * _CAPPED_SECTION_BUDGET_SHARE))
    behavioral_share = budget - capped_share

    if len(behavioral_text) > behavioral_share:
        behavioral_text = behavioral_text[:behavioral_share].rstrip() + marker
    if len(capped_text) > capped_share:
        capped_text = capped_text[:capped_share].rstrip() + marker
    return "\n".join(filter(None, (behavioral_text, capped_text))) + "\n" + pointer_line


def _index_completeness_note(memory_root: Path) -> str:
    """One line when the tree still holds leaves the index doesn't name.

    An index is read as an exhaustive inventory, so a partial one actively
    suppresses discovery of whatever it omits — measured worse than no index
    at all (ISSUE-008: the arm handed a partial index scored 0.40 against 0.52
    for the arm handed nothing, and the five questions it missed were exactly
    the facts living in the unindexed files). SessionStart folds orphans in
    before this renders, so a leftover means a leaf that could not be read or
    indexed this session, and the reader is told not to trust the list as
    complete.
    """
    try:
        import memory_lifecycle  # noqa: E402

        if memory_lifecycle.list_orphans(memory_root):
            return (
                "NOTE: this index may be incomplete — unindexed files were "
                "found in the tree and are folded in at each session start. "
                "Treat it as a map, not an inventory.\n"
            )
    except Exception:
        # Best-effort: if the scan can't run, say nothing rather than warn
        # about a condition we did not establish.
        pass
    return ""


def build_memory_index_context(
    memory_root: Path,
    max_chars: int | None = None,
) -> str | None:
    """Labeled additionalContext block for the project MEMORY.md INDEX.

    Injects the CORE VIEW: behavioral rules complete, project/reference entries
    carrying their descriptions (capped, newest-first), plus an explicit on-disk
    pointer. Returns None when there is no MEMORY.md or it is empty. Never
    raises on a missing dir (resolve_memory_dir may point at a not-yet-
    scaffolded folder; read_project_memory returns an empty snapshot).

    The header is deliberately one line of teaching. It used to carry a
    paragraph explaining the tree, the collapse rule, and where leaves live —
    ~150 tokens of packaging in a block whose payload had been cut to ~40.
    """
    snap = read_project_memory(memory_root)
    text = (snap.memory_index_text or "").strip()
    if not text:
        return None
    # A caller with a tighter budget (the Codex adapter caps at 4000) renders
    # TO that budget rather than slicing the finished block: the renderer
    # truncates with the on-disk pointer preserved, where a blind slice would
    # cut from the end — which is where Project/Reference live, reinstating
    # exactly the invisibility this change removes.
    prefix = (
        f"=== Allostat project memory (index) - {memory_root} ===\n"
        f"{_index_completeness_note(memory_root)}"
        f"Your memory for this project lives in `{memory_root}`.\n\n"
        f"{_ORIENTATION_PROCEDURE}\n\n"
    )
    budget = _CORE_INDEX_MAX_CHARS
    if max_chars is not None:
        # Charge the real required prefix before rendering the index. A fixed
        # allowance can silently overrun the caller's cap as policy evolves.
        budget = max_chars - len(prefix)
        if budget < 500:
            return None
    core_view = _render_core_index(text, memory_root, max_chars=budget)
    block = prefix + core_view
    while max_chars is not None and len(block) > max_chars:
        overflow = len(block) - max_chars
        budget -= overflow
        if budget < 500:
            return None
        core_view = _render_core_index(text, memory_root, max_chars=budget)
        block = prefix + core_view
    return block


# ---------- locked decisions extractor ----------

_LOCKED_HEADINGS = (
    "locked decisions", "locked", "settled decisions",
    "decisions locked", "lock", "do not change",
)
_INLINE_LOCK_RE = re.compile(
    r"\([^)]*?(?:locked|do not change|do not edit)[^)]*?\)",
    re.IGNORECASE,
)


def extract_locked_decisions(file_path: Path) -> list[str]:
    """Find bullet items under 'Locked decisions' headings, plus inline
    lines with (locked YYYY-MM-DD) markers. Returns deduped list, order
    preserved.
    """
    if not file_path.is_file():
        return []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    decisions: list[str] = []
    in_locked_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            in_locked_section = any(h in heading for h in _LOCKED_HEADINGS)
            continue
        if in_locked_section and stripped.startswith(("-", "*", "+")):
            item = stripped[1:].strip()
            if item:
                decisions.append(item)
            continue
        if _INLINE_LOCK_RE.search(stripped) and stripped:
            decisions.append(stripped)

    seen: set = set()
    out: list[str] = []
    for d in decisions:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------- pillar context surface ----------

_DECISION_AWARE_PILLARS = ("volume-control", "stress-response", "confidence-gate",
                            "volume_control", "stress_response", "confidence_gate")
_VOICE_AWARE_PILLARS = ("voice-keeper", "voice_keeper")


def nudge_context_from_memory(
    memory_root: Path,
    pillar: str,
    max_chars: int = 180,
) -> str | None:
    """Return a one-line context fragment to interpolate into a pillar's
    nudge. None when nothing relevant lives in memory.

    Voice-aware pillars get a voice-reference hint.
    Decision-aware pillars get the most-recent project_*.md's first locked
    decision.

    Bounded by max_chars (180 per v2.3 default).
    """
    snap = read_project_memory(memory_root)
    if snap.memory_root is None:
        return None

    if pillar in _VOICE_AWARE_PILLARS:
        if snap.voice_reference_path is not None:
            hint = f"voice reference: {snap.voice_reference_path.name} — match cadence + register"
            return hint[:max_chars]
        return None

    if pillar in _DECISION_AWARE_PILLARS:
        for project_file in snap.project_files:
            decisions = extract_locked_decisions(project_file)
            if decisions:
                snippet = decisions[0]
                hint = f"locked: {snippet}"
                return hint[:max_chars]
        return None

    return None


# ---------- pillar-post-processing interpolation ----------

def interpolate_nudge(
    nudge_text: str,
    memory_root: Path,
    pillar: str,
) -> str:
    """Take server-returned nudge text and interpolate operator-memory
    context client-side. Looks for the marker `{{memory_context}}` and
    substitutes the memory snippet.

    If no marker, returns nudge_text unchanged (server didn't request
    interpolation for this nudge).

    Per advisor §1.2: this happens WRAPPER-SIDE. Server never sees the
    interpolation result; operator's memory content never crosses to server.
    """
    marker = "{{memory_context}}"
    if marker not in nudge_text:
        return nudge_text
    snippet = nudge_context_from_memory(memory_root, pillar) or ""
    return nudge_text.replace(marker, snippet)
