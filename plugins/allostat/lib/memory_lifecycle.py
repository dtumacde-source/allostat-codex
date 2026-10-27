"""Allostat memory lifecycle orchestrator — PATCH-136 / v0.4.0.

Coordinates the ported v2.3 lifecycle libs (apoptotic_retirement + tier_inference)
into operator-facing operations that slash commands invoke.

Public API (called by slash commands via the CLI entry):
- tend(memory_dir, known_projects) → audit report
- retire(memory_dir, rule_id, reason) → marks a rule retiring
- retain(memory_dir, rule_id) → cancels retirement
- list_retiring(memory_dir) → status of all retiring rules
- finalize_retired(memory_dir) → moves counter-zero rules to _RETIRED/<date>/

SessionStart hook integration:
- tick_at_session_start(memory_dir) → decrements retiring counters once per session,
  finalizes rules whose counter reaches 0

Stop hook integration:
- nothing direct; retirement state machine ticks at session-START not session-END
  (per v2.3 design: ticks reflect "another session has passed without operator
  cancellation, retirement closer")
"""
from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apoptotic_retirement  # noqa: E402
import archipelago_view  # noqa: E402
import auto_merge_proposer  # noqa: E402
import hierarchy_validator  # noqa: E402
import lifecycle_ladder  # noqa: E402
import memory_md_cap  # noqa: E402
import pruning  # noqa: E402
import stale_link_cleanup  # noqa: E402
import tier_inference  # noqa: E402


# ---------- cold-storage exclusion (shared with the pruner) ----------
#
# Audit 2026-07-20: the index helpers below carried a private
# `{"_LEGACY", "_PURGE", "_RETIRED", "_processed"}` EXACT-NAME skip set, which
# matched none of the directories the system actually creates:
#   - the pruner archives into `_archived_<YYYYMMDD>/` (pruning.ARCHIVE_DIR_PREFIX)
#   - session_handoff._archive_reconcile_loser writes `_LEGACY_reconcile/`
#     ("_LEGACY_reconcile" != "_LEGACY")
#   - every session handoff lives in `handoffs/`
# So `--rebuild-index` RESURRECTED deliberately-pruned files as live MEMORY.md
# entries (undoing pruning, whose contract is "stop loading it unless told to
# recall") and appended one `## Other` entry per handoff UUID into the file
# injected at every SessionStart. `--orphans` reported the same files as
# orphans. The skip is now PREFIX-aware and derives from the pruner's own
# NEVER_TOUCHED_DIR_PREFIXES, unioned with the index-only exclusions, so the
# pruner and the tender can never disagree about what is cold storage. The
# literal set is kept alongside the union so this can never narrow below the
# directories named above if the pruner's tuple changes.
_INDEX_SKIP_DIR_PREFIXES: tuple[str, ...] = tuple(sorted(
    {"_LEGACY", "_PURGE", "_RETIRED", "_archived_", "_processed",
     "handoffs", "silos"}
    | set(pruning.NEVER_TOUCHED_DIR_PREFIXES)
))
# Filename prefixes that mark an archived leaf even outside an archive dir
# (lifecycle_ladder's `<name>_LEGACY_pre_<stamp>.md` lands in `_LEGACY/`, but a
# stray copy at the tree root is still archive content, not live memory).
_INDEX_SKIP_FILE_PREFIXES: tuple[str, ...] = (
    "_LEGACY", "_PURGE", "_RETIRED", "_archived_",
)


def _is_cold_storage(memory_dir: Path, path: Path) -> bool:
    """True iff `path` lives in (or is) cold storage / machinery and must not
    appear in the live MEMORY.md index, be reported as an orphan, or be scanned
    for de-indexing.

    Directory parts match by PREFIX (so `_archived_20260721/` and
    `_LEGACY_reconcile/` are both caught); the leaf filename matches only the
    archive prefixes, so an ordinary memory file whose name happens to start
    with "handoffs" or "silos" is still indexed.
    """
    try:
        parts = path.relative_to(memory_dir).parts
    except ValueError:
        parts = path.parts
    if not parts:
        return False
    for part in parts[:-1]:
        if part.startswith(_INDEX_SKIP_DIR_PREFIXES):
            return True
    return parts[-1].startswith(_INDEX_SKIP_FILE_PREFIXES)


# ---------- session-hook integration ----------

def tick_at_session_start(memory_dir: Path) -> dict:
    """Called by SessionStart hook. Decrements retirement counters; finalizes
    rules whose counter reaches 0.

    Returns:
        {
          "ticked": int,       # how many retiring rules were ticked
          "transitioned_to_retired": [rule_ids],
          "finalized": [rule_ids],  # successfully moved to _RETIRED/
        }
    """
    result = {"ticked": 0, "transitioned_to_retired": [], "finalized": []}
    if not memory_dir.exists():
        return result

    transitions = apoptotic_retirement.tick_retiring_rules(memory_dir)
    result["ticked"] = len(transitions)

    for status in transitions:
        if status.state == "retired" and status.rule_path is not None:
            result["transitioned_to_retired"].append(status.rule_id)
            archive_path = apoptotic_retirement.finalize_retirement(status.rule_path)
            if archive_path:
                result["finalized"].append(status.rule_id)

    return result


# ---------- /allostat-tend ----------

def tend(memory_dir: Path, known_projects: list[str] | None = None) -> dict:
    """Manual hygiene pass. Returns audit report for operator review.

    Includes:
      - tier-distribution of memory tree
      - low-confidence / unknown-pattern files (tier integrity issues)
      - retirement status of every rule currently in deprecation window
      - count of files awaiting finalization (state=retired but not yet archived)
    """
    audit = tier_inference.audit_memory_tree(memory_dir, known_projects=known_projects)
    retiring = apoptotic_retirement.list_retiring_rules(memory_dir)

    awaiting_finalization = []
    for rule_path in memory_dir.rglob("*.md") if memory_dir.exists() else []:
        if "_RETIRED" in rule_path.parts:
            continue
        if rule_path.name == "MEMORY.md":
            continue
        try:
            text = rule_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if apoptotic_retirement._extract_frontmatter_field(text, "retirement_state", "active") == "retired":
            awaiting_finalization.append(rule_path.name)

    return {
        "memory_dir": str(memory_dir),
        "tier_audit": audit,
        # Prefixed/unprefixed leaf pairs about one subject, flagged by the
        # SessionStart reconcile. Both stay indexed and both stay on disk;
        # merging them is a human call, which is why they surface here.
        "convention_pairs": read_convention_pairs(memory_dir),
        "retiring_rules": [
            {
                "rule_id": s.rule_id,
                "sessions_remaining": s.sessions_remaining,
                "retrieval_priority_multiplier": s.retrieval_priority_multiplier,
            }
            for s in retiring
        ],
        "awaiting_finalization": awaiting_finalization,
    }


# ---------- /allostat-retire / /allostat-retain ----------

def retire(memory_dir: Path, rule_id: str, reason: str = "operator_directed") -> dict:
    """Mark a rule by id (filename without .md) as retiring."""
    rule_path = _resolve_rule_path(memory_dir, rule_id)
    if rule_path is None:
        return {"ok": False, "error": f"rule not found: {rule_id}"}

    ok = apoptotic_retirement.initiate_retirement(rule_path, reason=reason)
    if not ok:
        return {"ok": False, "error": "could not write retirement frontmatter"}

    return {
        "ok": True,
        "rule_id": rule_id,
        "rule_path": str(rule_path),
        "sessions_until_retired": apoptotic_retirement.DEPRECATION_WINDOW_SESSIONS,
        "cancel_with": f"/allostat-retain {rule_id}",
    }


def retain(memory_dir: Path, rule_id: str) -> dict:
    """Cancel a rule's retirement."""
    rule_path = _resolve_rule_path(memory_dir, rule_id)
    if rule_path is None:
        return {"ok": False, "error": f"rule not found: {rule_id}"}

    ok = apoptotic_retirement.cancel_retirement(rule_path)
    if not ok:
        return {"ok": False, "error": "could not strip retirement frontmatter"}

    return {
        "ok": True,
        "rule_id": rule_id,
        "rule_path": str(rule_path),
        "message": "retirement cancelled — rule remains active",
    }


# ---------- Phase 1 v0.5.0 — Slice 1 new verbs ----------

def bulk_retire(memory_dir: Path, rule_ids: list[str], reason: str = "operator_directed_bulk") -> dict:
    """Retire many rules in one call. Each enters 5-session deprecation window
    simultaneously. Returns per-rule outcome summary.

    Addresses advisor Concern 8 — operator's 8 outstanding feedback_advisor_*
    files can flow through retirement in a single CLI call instead of 8
    individual /allostat-retire commands spread across sessions.
    """
    results = {"ok": [], "failed": []}
    for rule_id in rule_ids:
        r = retire(memory_dir, rule_id, reason=reason)
        if r.get("ok"):
            results["ok"].append(rule_id)
        else:
            results["failed"].append({"rule_id": rule_id, "error": r.get("error")})
    return results


def _normalized_index_relpath(raw: str) -> str | None:
    """Canonical relative identity for an on-disk leaf or Markdown link."""
    value = raw.strip().strip("<>").replace("\\", "/")
    value = value.split("#", 1)[0].split("?", 1)[0]
    normalized = posixpath.normpath(value)
    if (
        normalized in ("", ".")
        or normalized.startswith("../")
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
    ):
        return None
    return normalized


def _memory_relpath(memory_dir: Path, path: Path) -> str | None:
    try:
        return _normalized_index_relpath(path.relative_to(memory_dir).as_posix())
    except ValueError:
        return None


def list_orphans(memory_dir: Path) -> list[str]:
    """Return .md files on disk that are not referenced in MEMORY.md.

    Reads MEMORY.md, parses every `[Title](file.md)` link, walks memory tree
    and returns normalized memory-relative paths for .md files that exist on
    disk but aren't indexed. Relative identity is load-bearing: two nested
    leaves may legitimately share a basename.
    Skips cold storage + machinery (`_is_cold_storage`: _LEGACY*/ _PURGE*/
    _RETIRED*/ _archived_*/ _processed/ handoffs/ silos/) and MEMORY.md itself
    — an archived or handoff file is not an orphan, it is deliberately unindexed.
    """
    import re
    if not memory_dir.exists():
        return []

    index = memory_dir / "MEMORY.md"
    referenced: set[str] = set()
    if index.exists():
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", text):
                rel = _normalized_index_relpath(m.group(1))
                if rel is not None:
                    referenced.add(rel)
        except OSError:
            pass

    orphans = []
    for p in memory_dir.rglob("*.md"):
        if _is_cold_storage(memory_dir, p):
            continue
        if p.name == "MEMORY.md":
            continue
        if p.name == "README.md":
            continue
        rel = _memory_relpath(memory_dir, p)
        if rel is not None and rel not in referenced:
            orphans.append(rel)
    return sorted(orphans)


# ---------- orphan self-healing (2026-08-07) ----------
#
# A written leaf must be reachable. That was agent discipline — write the file,
# remember to add the index line — and discipline lost: a normal 19-session
# ingest produced four leaves and indexed two, having switched naming convention
# partway (`caroline-profile.md` -> `project_caroline.md`) and abandoned the
# originals holding sessions 1-3.
#
# The cost isn't the two missing lines. An index is read as an exhaustive
# inventory, so a partial index actively SUPPRESSES discovery of what it omits:
# the benchmark arm given the partial index scored 0.40 where the arm given no
# index at all scored 0.52, because the second one explored the directory and
# found the orphans while the first trusted the list and stopped. Strictly worse
# than no index.
#
# So reachability becomes mechanical. Every session start, before the index is
# injected, unindexed leaves are appended to their section. Append-only: no
# existing line is rewritten or reordered, no leaf is moved or deleted.

_RECONCILE_DESCRIPTION_CAP = 150
_CONVENTION_PAIRS_RELPATH = ("_processed", "convention_pairs.json")

# Never indexed: the index itself and README prose. Must stay in step with
# `list_orphans`'s own exclusions — a file one of them skips and the other
# reports is a permanent orphan, which would pin the "index may be incomplete"
# warning on forever and teach the reader to ignore it. `_PURPOSE.md` is
# deliberately NOT here: it is a real memory leaf and belongs in the index.
_RECONCILE_SKIP_NAMES = frozenset({"MEMORY.md", "README.md"})

# Frontmatter `type` -> section, for leaves with no filename prefix. Anything
# else (including `user`) lands in Project, per the same default the scaffold
# template teaches.
_TYPE_TO_SECTION = {
    "feedback": "Feedback",
    "project": "Project",
    "reference": "Reference",
}


def _declared_type(text: str) -> str:
    """The leaf's declared `type`, read from frontmatter at ANY indentation.

    The documented schema nests it (`metadata:` / `  type: project`), so a
    column-0-only read — which is what the destructive paths correctly use —
    finds nothing and every typed leaf routes to the default. Scoped to the
    frontmatter block, so a prose mention still can't be mistaken for a
    declaration.
    """
    span = apoptotic_retirement._frontmatter_slice(text)
    if span is None:
        return ""
    m = re.search(r"^[ \t]*type:\s*(.+?)\s*$", text[span[0]:span[1]], re.MULTILINE)
    return m.group(1).strip().strip("'\"").lower() if m else ""


def _reconcile_section_for(path: Path, text: str) -> str:
    """Section for an orphan leaf: filename prefix first, then frontmatter
    `type`, else Project."""
    section = _section_for_filename(path.name)
    if section != "Other":
        return section
    return _TYPE_TO_SECTION.get(_declared_type(text), "Project")


def _stem_tokens(fname: str) -> list[str]:
    """Filename reduced to comparable tokens, with any section prefix removed.

    `project_caroline.md` and `caroline-profile.md` both start at `caroline`,
    which is what makes them recognizable as one subject split across a
    convention change.
    """
    stem = fname[:-3] if fname.lower().endswith(".md") else fname
    lower = stem.lower()
    for prefix in ("feedback_", "project_", "reference_"):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    return [t for t in re.split(r"[-_\s]+", lower) if t]


def _find_convention_pairs(memory_dir: Path, live_names: list[str]) -> list[dict]:
    """Prefixed/unprefixed leaf pairs about the same subject.

    A pair is two live leaves that differ in whether they carry a section
    prefix, where one's token list starts the other's AND the two differ by at
    most one token. Both stay indexed and both stay on disk — this only flags
    them for `/allostat-tend`, where merging is human-gated.

    The one-token bound is not arbitrary. Run against a real 423-file tree,
    a bare prefix relation flagged `project_allostat.md` with
    `allostat-mcp-installer-fix-brief.md` and `feedback_purpose_over_expediency.md`
    with `_PURPOSE.md` — a shared first word is not a shared subject, and a
    tend report full of coincidences is a tend report nobody reads. A real
    convention change adds a qualifier: `project_caroline.md` /
    `caroline-profile.md`, one token apart.

    Machinery files (leading `_`) are excluded: they are not subject leaves and
    can never be the other half of a rename.
    """
    prefixed = [
        n for n in live_names if _section_for_filename(Path(n).name) != "Other"
    ]
    bare = [
        n for n in live_names
        if _section_for_filename(Path(n).name) == "Other"
        and not Path(n).name.startswith("_")
    ]
    pairs: list[dict] = []
    for p in prefixed:
        p_tokens = _stem_tokens(Path(p).name)
        if not p_tokens:
            continue
        for b in bare:
            b_tokens = _stem_tokens(Path(b).name)
            if not b_tokens:
                continue
            shorter, longer = sorted((p_tokens, b_tokens), key=len)
            if len(longer) - len(shorter) > 1:
                continue
            if longer[: len(shorter)] == shorter:
                pairs.append({
                    "prefixed": p,
                    "unprefixed": b,
                    "shared_stem": " ".join(shorter),
                    "reason": "naming_convention_change",
                })
    return pairs


def reconcile_orphans(memory_dir: Path) -> dict:
    """Append an index line for every live leaf MEMORY.md doesn't name.

    Runs at SessionStart before the index is injected, so what reaches the
    agent names everything on disk. Append-only and non-destructive:

      - existing index lines are never rewritten, reordered, or removed
      - leaf files are never moved, renamed, or deleted (archiving is
        /allostat-prune's job and stays human-gated)
      - cold storage, handoffs, silos, `_processed/`, dotfiles and empty files
        are not leaves and are not indexed
      - a missing section header is appended before its first entry; existing
        section bodies take the new line at their end

    Returns {"scanned", "indexed", "failed", "sections", "pairs", "ok"}.
    Best-effort throughout: a tree that can't be read returns a report saying
    so rather than raising into the hook.
    """
    report = {"scanned": 0, "indexed": [], "failed": [], "sections": {},
              "pairs": [], "ok": False}
    if not memory_dir.is_dir():
        return report

    index = memory_dir / "MEMORY.md"
    orphan_paths = set(list_orphans(memory_dir))
    report["scanned"] = len(orphan_paths)

    live_names: list[str] = []
    for p in memory_dir.rglob("*.md"):
        if _is_cold_storage(memory_dir, p) or p.name in _RECONCILE_SKIP_NAMES:
            continue
        if not p.name.startswith("."):
            rel = _memory_relpath(memory_dir, p)
            if rel is not None:
                live_names.append(rel)
    report["pairs"] = _find_convention_pairs(memory_dir, sorted(set(live_names)))

    if not orphan_paths:
        report["ok"] = True
        _write_convention_pairs(memory_dir, report["pairs"])
        return report

    # (section, entry_line) for each orphan we can actually describe.
    additions: list[tuple[str, str]] = []
    for p in sorted(memory_dir.rglob("*.md")):
        rel = _memory_relpath(memory_dir, p)
        if rel is None or rel not in orphan_paths:
            continue
        if p.name in _RECONCILE_SKIP_NAMES or p.name.startswith("."):
            continue
        if _is_cold_storage(memory_dir, p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            report["failed"].append(rel)
            continue
        if not text.strip():
            # An empty file is not yet a memory. Skipped, not failed — it
            # gets indexed on the session after it has content.
            continue
        description = _extract_description(text)
        if len(description) > _RECONCILE_DESCRIPTION_CAP:
            description = description[:_RECONCILE_DESCRIPTION_CAP].rstrip() + "..."
        title = p.name[:-3].replace("_", " ").replace("-", " ").title()
        additions.append((
            _reconcile_section_for(p, text),
            f"- [{title}]({rel}) — {description}",
        ))

    if not additions:
        report["ok"] = True
        _write_convention_pairs(memory_dir, report["pairs"])
        return report

    try:
        original = index.read_text(encoding="utf-8", errors="replace") if index.exists() else ""
    except OSError as e:
        report["failed"].append(f"MEMORY.md: {e}")
        return report

    lines = original.splitlines()
    for section, entry in additions:
        lines = _append_entry_to_section(lines, section, entry)
        report["indexed"].append(entry)
        report["sections"][section] = report["sections"].get(section, 0) + 1

    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    try:
        index.write_text(new_text, encoding="utf-8")
        report["ok"] = True
    except OSError as e:
        report["failed"].append(f"MEMORY.md: {e}")
    _write_convention_pairs(memory_dir, report["pairs"])
    return report


def _append_entry_to_section(lines: list[str], section: str, entry: str) -> list[str]:
    """Insert `entry` at the END of `section`'s body, creating the section if
    absent. Existing lines keep their order and their text."""
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("## ") or stripped.startswith("### "):
            continue
        heading = stripped[3:].strip()
        if heading == section or heading.startswith(section + " ") or heading.startswith(section + "("):
            header_idx = i
            break

    if header_idx is None:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.append(f"## {section}")
        out.append("")
        out.append(entry)
        return out

    # Last non-blank line before the next `## ` header (or EOF).
    end = len(lines)
    for j in range(header_idx + 1, len(lines)):
        nxt = lines[j].lstrip()
        if nxt.startswith("## ") and not nxt.startswith("### "):
            end = j
            break
    insert_at = end
    while insert_at > header_idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    return lines[:insert_at] + [entry] + lines[insert_at:]


def _write_convention_pairs(memory_dir: Path, pairs: list[dict]) -> None:
    """Persist convention-change pairs for `/allostat-tend` to surface.

    Supersede semantics, and deliberately NOT the merge queue: that file is
    overwritten wholesale by the Stop-hook detector, which would erase these.
    """
    import json as _json
    target = memory_dir.joinpath(*_CONVENTION_PAIRS_RELPATH)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _json.dumps({"count": len(pairs), "pairs": pairs}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: these pairs are a HINT for /allostat-tend, not a record
        # anything depends on. Both files stay indexed and both stay on disk
        # either way, so a failed write loses a suggestion, not a memory — and
        # the next session start recomputes it from scratch.
        pass


def read_convention_pairs(memory_dir: Path) -> list[dict]:
    """Read the flagged convention-change pairs. [] on missing/corrupt file."""
    import json as _json
    target = memory_dir.joinpath(*_CONVENTION_PAIRS_RELPATH)
    if not target.is_file():
        return []
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pairs = data.get("pairs") if isinstance(data, dict) else None
    return pairs if isinstance(pairs, list) else []


def reorder_memory_md(memory_dir: Path, by: str = "age") -> dict:
    """Rewrite MEMORY.md with entries sorted oldest→newest (by="age")
    or grouped by tier (by="tier").

    Preserves the file's pre-list header (markdown above the first index
    entry). Only the list section is reordered.
    """
    import re
    if not memory_dir.exists():
        return {"ok": False, "error": "memory_dir not found"}
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        return {"ok": False, "error": "MEMORY.md not found"}

    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}

    lines = text.splitlines()
    entry_pattern = re.compile(r"^- \[[^\]]+\]\(([^)]+\.md)\)")
    section_pattern = re.compile(r"^#{1,6}\s")

    # H-14: reordering must be SECTION-AWARE. Track which `##`-heading section
    # each entry lives under and sort ONLY within that section — never move an
    # entry across a heading boundary. (The prior global sort + positional
    # rewrite could relocate an always-on feedback_* entry into the collapsed
    # Project/Reference body, silently dropping it from every-session injection.)
    entries: list[tuple[int, str, str, int]] = []  # (line_idx, line, target_file, section_id)
    section_id = 0  # 0 = pre-heading preamble; increments at each heading line
    for i, line in enumerate(lines):
        if section_pattern.match(line):
            section_id += 1
            continue
        m = entry_pattern.match(line)
        if m:
            entries.append((i, line, m.group(1).split("/")[-1], section_id))

    if not entries:
        return {"ok": False, "error": "no entries found in MEMORY.md"}

    if by == "age":
        def sort_key(entry):
            _, _, fname, _ = entry
            target = memory_dir / fname
            try:
                return target.stat().st_mtime
            except OSError:
                # INTENT (fixpass 2026-07-01): a missing/unstatable target
                # sorts FIRST but its index entry is preserved — replacement
                # below is positional over the original lines, so no entry is
                # ever lost. Do NOT "skip" failed entries here: shortening
                # sorted_entries truncates the zip below and duplicates lines.
                return 0.0
    elif by == "tier":
        def sort_key(entry):
            _, _, fname, _ = entry
            cls = tier_inference.infer_tier(fname)
            tier_order = {"innate": 0, "user": 1, "umbrella": 2, "project": 3,
                          "reference": 4, "outside-matrix": 5}
            return (tier_order.get(cls.tier, 99), fname.lower())
    else:
        return {"ok": False, "error": f"unknown sort key: {by}"}

    # Sort within each section independently, then rewrite each entry line in
    # its ORIGINAL slot with the section-locally sorted text. Because every
    # entry's slot is filled only from entries of the SAME section, no entry can
    # cross a `##` boundary.
    new_lines = list(lines)
    for sid in dict.fromkeys(e[3] for e in entries):  # unique section ids, in order
        section_entries = [e for e in entries if e[3] == sid]
        sorted_texts = [e[1] for e in sorted(section_entries, key=sort_key)]
        for (orig_idx, _, _, _), new_text in zip(section_entries, sorted_texts):
            new_lines[orig_idx] = new_text

    new_content = "\n".join(new_lines)
    if text.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    backup = index.with_suffix(".md.PRE_REORDER")
    try:
        backup.write_text(text, encoding="utf-8")
        index.write_text(new_content, encoding="utf-8")
        return {"ok": True, "sorted_by": by, "entries_reordered": len(entries),
                "backup": str(backup)}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# B3 (2026-06-26) — section grouping for the regenerated index. Mirrors the
# realized two-layer injection model (memory_reader._render_core_index):
# behavioral (feedback_*) always-on, project_* + reference_* collapsible.
_REBUILD_SECTION_ORDER = ("Feedback", "Project", "Reference", "Other")


def _section_for_filename(fname: str) -> str:
    """Map a leaf filename to its MEMORY.md section by prefix (B3). Falls back
    to ## Other for anything that isn't feedback_/project_/reference_."""
    lower = fname.lower()
    if lower.startswith("feedback_"):
        return "Feedback"
    if lower.startswith("project_"):
        return "Project"
    if lower.startswith("reference_"):
        return "Reference"
    return "Other"


def _normalize_link_target(raw: str) -> str:
    """Normalize a markdown link target to a memory_dir-relative POSIX path:
    backslashes → `/`, leading `./` stripped."""
    norm = raw.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def _parse_curated_hooks(index_text: str) -> dict[str, str]:
    """Extract hand-curated hooks from an existing MEMORY.md, keyed by the
    NORMALIZED link target. B3 anti-regression: a hook the operator wrote must
    survive a rebuild — only entries with no curated hook fall back to the
    frontmatter `description:`.

    Returns {relative_posix_path: hook_text}. A line like
        `- [Title](feedback_x.md) — operator-written hook`
    yields {"feedback_x.md": "operator-written hook"}, and
        `- [Launch plan](action_plans/project_launch.md) — the T-48h sequence`
    yields {"action_plans/project_launch.md": "the T-48h sequence"}.
    Entries with no hook (no em-dash tail) are skipped so they fall back to the
    frontmatter.

    Audit 2026-07-20: the key used to be the RAW target while the lookup used a
    bare basename, so every subdirectory entry missed the map and the operator's
    hook was silently downgraded to the auto-extracted frontmatter description.
    `_lookup_curated_hook` now matches on the relative path first and falls back
    to an UNAMBIGUOUS basename match (which keeps flat legacy indexes working).
    """
    import re
    hooks: dict[str, str] = {}
    # Match `](<filename>.md)` then capture an em-dash or hyphen-led hook tail.
    line_re = re.compile(r"\]\(([^)]+\.md)\)\s*(?:—|-)\s*(.+?)\s*$")
    for line in index_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*", "+")):
            continue
        m = line_re.search(stripped)
        if not m:
            continue
        fname, hook = _normalize_link_target(m.group(1)), m.group(2).strip()
        if fname and hook:
            hooks[fname] = hook
    return hooks


def _lookup_curated_hook(
    curated_hooks: dict[str, str], rel_path: str,
) -> str | None:
    """Find the operator's hook for `rel_path` (memory_dir-relative POSIX).

    Exact relative-path match wins. Otherwise fall back to a basename match,
    but ONLY when exactly one indexed entry has that basename — an ambiguous
    basename must not paste one file's hook onto a different file.
    """
    hook = curated_hooks.get(rel_path)
    if hook is not None:
        return hook
    base = rel_path.rsplit("/", 1)[-1]
    matches = [v for k, v in curated_hooks.items() if k.rsplit("/", 1)[-1] == base]
    if len(matches) == 1:
        return matches[0]
    return None


def _unique_backup_path(base: Path) -> Path:
    """Return `<base>_<utc-stamp>` — with a `-<n>` disambiguator appended if a
    file of that name already exists. A backup must never overwrite a backup.
    """
    from local_state import utc_iso  # noqa: E402
    stamp = utc_iso().replace(":", "").replace("-", "")
    candidate = base.with_name(f"{base.name}_{stamp}")
    counter = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{stamp}-{counter}")
        counter += 1
    return candidate


def rebuild_memory_md_index(memory_dir: Path) -> dict:
    """Regenerate MEMORY.md from disk, GROUPED under ## Feedback / ## Project /
    ## Reference (fallback ## Other) per the realized two-layer injection model
    (B3, 2026-06-26 — supersedes the prior FLAT list which contradicted the
    two-level spec in memory_architecture.md).

    Hook source (B3 anti-regression): PRESERVE any hand-curated hook already in
    the current MEMORY.md (keyed by the leaf filename); only fall back to the
    frontmatter `description:` (or first prose line) for entries with NO curated
    hook. This keeps an operator-written hook from silently downgrading to an
    auto-extracted one on every rebuild.

    Skips cold storage + machinery via `_is_cold_storage` — archived files
    (`_archived_*/`, `_LEGACY*/`, `_PURGE*/`, `_RETIRED*/`) must NOT be
    resurrected as live index entries, and `handoffs/` must not be indexed at
    all (one `## Other` line per session UUID would crowd out real memory in the
    SessionStart injection).

    Link targets are emitted memory_dir-RELATIVE, so an entry under
    `action_plans/` stays resolvable instead of being flattened to a root path
    that does not exist.
    """
    if not memory_dir.exists():
        return {"ok": False, "error": "memory_dir not found"}

    index = memory_dir / "MEMORY.md"
    # Read curated hooks from the existing index BEFORE we overwrite it.
    curated_hooks: dict[str, str] = {}
    if index.exists():
        try:
            curated_hooks = _parse_curated_hooks(
                index.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            curated_hooks = {}

    # section -> list[(relative_link_target, leaf_filename, hook)]
    grouped: dict[str, list[tuple[str, str, str]]] = {s: [] for s in _REBUILD_SECTION_ORDER}
    total = 0

    for p in sorted(memory_dir.rglob("*.md")):
        if _is_cold_storage(memory_dir, p):
            continue
        if p.name in ("MEMORY.md", "README.md"):
            continue
        try:
            rel = p.relative_to(memory_dir).as_posix()
        except ValueError:
            continue
        # Curated hook wins; otherwise fall back to the frontmatter description.
        hook = _lookup_curated_hook(curated_hooks, rel)
        if hook is None:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Best-effort: an unreadable leaf is simply skipped from the
                # regenerated index (it stays on disk; nothing is destroyed).
                continue
            hook = _extract_description(text)
            # Cap auto-extracted descriptions per the MEMORY.md entry cap rule.
            # (Curated hooks are left as the operator wrote them.)
            max_desc = max(30, memory_md_cap.ENTRY_CAP_CHARS - len(p.name) - 30)
            if len(hook) > max_desc:
                hook = hook[:max_desc].rstrip() + "..."
        grouped[_section_for_filename(p.name)].append((rel, p.name, hook))
        total += 1

    title_lines = ["# Memory index — auto-generated by Allostat v1.0.0",
                   "",
                   f"Regenerated {total} entries from disk via `/allostat-tend --rebuild-index`.",
                   "Grouped by section; hooks preserve any hand-curated text in the prior "
                   "index, else fall back to frontmatter `description:`.",
                   ""]
    body_lines: list[str] = []
    for section in _REBUILD_SECTION_ORDER:
        section_entries = grouped[section]
        if not section_entries:
            continue
        body_lines.append("")
        body_lines.append(f"## {section}")
        for rel, fname, hook in section_entries:
            title = fname[:-3].replace("_", " ").title()
            # Link target is memory_dir-RELATIVE (audit 2026-07-20): emitting
            # only `p.name` rewrote `action_plans/project_launch.md` as a
            # root-level `project_launch.md` that does not exist.
            body_lines.append(f"- [{title}]({rel}) — {hook}")
    new_text = "\n".join(title_lines + body_lines) + "\n"

    # Timestamped backup (audit 2026-07-20): a single fixed `.PRE_REBUILD` name
    # was overwritten on every run, so a second rebuild destroyed the only copy
    # of the operator's original index prose. Matches the PRE_DEINDEX convention
    # in detect_and_deindex_retired below, plus collision avoidance — the stamp
    # has 1-second resolution, and "timestamped" that still collides is still a
    # backup eating a backup.
    backup = _unique_backup_path(index.with_name("MEMORY.md.PRE_REBUILD"))
    try:
        index_existed = index.exists()
        if index_existed:
            backup.write_text(index.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        index.write_text(new_text, encoding="utf-8")
        return {"ok": True, "entries": total,
                "backup": str(backup) if index_existed else None}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def _extract_description(text: str) -> str:
    """Pull a one-line description for the MEMORY.md index entry.

    Preference:
      1. YAML frontmatter `description:` field
      2. First H1 heading text
      3. First non-blank prose line after frontmatter
      4. Filename-derived fallback
    """
    import re
    if text.startswith("---\n"):
        second = text.find("\n---\n", 4)
        if second > 0:
            fm = text[4:second]
            m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
            if m:
                return m.group(1).strip().strip("'\"")
            body = text[second + 5:]
        else:
            body = text
    else:
        body = text

    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# "):
            return s[2:].strip()
        return s[:200]
    return "(no description available)"


# ---------- F1 — Memory Consolidation Enforcer ----------
#
# Closes the "detection exists but nothing enforces it" gap in two tiers.
#
# TIER 1 (detect_and_deindex_retired): AUTO de-index of self-declared RETIRED
#   files — safe because it's operator-self-declared (`status: RETIRED` in the
#   file's own frontmatter) and only touches the MEMORY.md index line, never
#   the file on disk (archive-not-destroy). Distinct from the
#   apoptotic_retirement `retirement_state` deprecation-window machine.
#
# TIER 2 (run_merge_detection_and_queue / read_pending_merge_count): PROPOSE
#   inferred-between-files merges, with detection DECOUPLED from execution. The
#   heavy TF-IDF detection runs at Stop and persists to a queue file; the cheap
#   reader runs at SessionStart and surfaces a count. The actual merge stays
#   operator-gated inside /allostat-tend — Tier 2 only ever PROPOSES.

_MERGE_QUEUE_RELPATH = ("_processed", "merge_queue.json")


def _extract_frontmatter_field(text: str, field_name: str, default: str = "") -> str:
    """Read a YAML frontmatter field, scoped to the leading `---` block ONLY
    (a prose mention like "discusses a status: RETIRED file" can never be
    mistaken for a declaration).

    Delegates to apoptotic_retirement._extract_frontmatter_field — the two
    modules carried identical copies until the 2026-07-01 fixpass dedup; the
    frontmatter-scoping data-safety guard now lives in ONE place.
    """
    return apoptotic_retirement._extract_frontmatter_field(text, field_name, default)


def detect_and_deindex_retired(memory_dir: Path) -> list[str]:
    """TIER 1 — de-index self-declared RETIRED memory files.

    Find memory .md files whose frontmatter declares `status: RETIRED` and
    which still have a line in MEMORY.md; remove ONLY that line from MEMORY.md.
    The file itself stays on disk untouched (archive-not-destroy).

    Robust to MEMORY.md referencing a file by bare name OR relative path:
    the line's `(...)` target is reduced to its basename before comparing.

    Returns the list of de-indexed filenames (bare names, e.g.
    "feedback_retired.md"). A RETIRED file already absent from the index is a
    no-op and is not reported.
    """
    import re
    if not memory_dir.exists():
        return []

    index = memory_dir / "MEMORY.md"
    if not index.is_file():
        return []

    # Collect the bare names of RETIRED-declared files. Skip cold storage +
    # machinery (same shared predicate the other index helpers use).
    retired_names: set[str] = set()
    for p in memory_dir.rglob("*.md"):
        if _is_cold_storage(memory_dir, p):
            continue
        if p.name in ("MEMORY.md", "README.md"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _extract_frontmatter_field(text, "status", "").upper() == "RETIRED":
            retired_names.add(p.name)

    if not retired_names:
        return []

    try:
        index_text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # A line is a candidate for removal when it links to a retired file. Match
    # any markdown link target ending in .md and reduce to its basename so both
    # `(feedback_retired.md)` and `(./sub/feedback_retired.md)` compare equal.
    link_re = re.compile(r"\(([^)]+\.md)\)")
    kept_lines: list[str] = []
    deindexed: set[str] = set()
    had_trailing_newline = index_text.endswith("\n")
    for line in index_text.splitlines():
        targets = link_re.findall(line)
        basenames = {t.replace("\\", "/").rsplit("/", 1)[-1] for t in targets}
        hit = basenames & retired_names
        if hit:
            deindexed |= hit
            continue  # drop this index line
        kept_lines.append(line)

    if not deindexed:
        return []

    new_text = "\n".join(kept_lines)
    if had_trailing_newline and not new_text.endswith("\n"):
        new_text += "\n"
    # Crash-safety (deep-audit 2026-07-02): this runs on every Stop and
    # overwrites the operator's MEMORY.md index. Back it up (matching the
    # reorder/rebuild `.PRE_*` convention in this module) and write atomically
    # so an interrupted write can't truncate the index.
    try:
        from local_state import atomic_write_text, utc_iso  # noqa: E402
        stamp = utc_iso().replace(":", "").replace("-", "")
        try:
            index.with_name(f"MEMORY.md.PRE_DEINDEX_{stamp}").write_text(
                index_text, encoding="utf-8"
            )
        except OSError:
            pass  # backup best-effort; atomic write below still protects
        atomic_write_text(index, new_text)
    except Exception:
        # Never let de-index crash the Stop hook; index stays as-is on failure.
        return []

    return sorted(deindexed)


def run_merge_detection_and_queue(memory_dir: Path) -> dict:
    """TIER 2 (detection half) — run find_merge_candidates and persist the
    result to `<memory_dir>/_processed/merge_queue.json`.

    Idempotent: a re-run SUPERSEDES the queue (full overwrite), never appends —
    so repeated Stop-hook fires can't pile up duplicate candidates. A simple
    `generation` counter (read from the prior file, incremented) marks
    freshness without depending on wall-clock time for any logic decision.

    Returns {"count": int, "generation": int}. No-op-safe when the memory tree
    is absent (returns count 0). The queue file lives under _processed/, which
    every memory scanner already skips, so it never becomes an orphan or a
    merge-detection input.
    """
    if not memory_dir.exists():
        return {"count": 0, "generation": 0}

    candidates = auto_merge_proposer.find_merge_candidates(memory_dir)
    serialized = [
        {
            "file_a": c.file_a,
            "file_b": c.file_b,
            "overlap_jaccard": c.overlap_jaccard,
            "top_shared_terms": c.top_shared_terms,
            "suggested_merged_name": c.suggested_merged_name,
        }
        for c in candidates
    ]

    queue_file = memory_dir.joinpath(*_MERGE_QUEUE_RELPATH)

    # Generation counter — read prior, increment. Pure supersede semantics.
    prior_generation = 0
    if queue_file.is_file():
        try:
            import json as _json
            prior = _json.loads(queue_file.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                prior_generation = int(prior.get("generation", 0) or 0)
        except (OSError, ValueError):
            prior_generation = 0
    generation = prior_generation + 1

    payload = {
        "count": len(serialized),
        "generation": generation,
        "candidates": serialized,
    }
    import json as _json
    try:
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        # Best-effort: a failed write must not break the Stop hook.
        return {"count": len(serialized), "generation": generation}

    return {"count": len(serialized), "generation": generation}


def read_pending_merge_count(memory_dir: Path) -> int:
    """TIER 2 (cheap reader) — return the pending merge-proposal count from the
    queue file WITHOUT re-running detection.

    Fast by design: this is what SessionStart calls (heavy detection at
    SessionStart is forbidden by the F1 brief — sessions get abandoned and the
    Stop-hook detection may never fire, so the persisted queue is what lets the
    NEXT session still surface the proposal). Returns 0 on missing/corrupt file
    or any read error; never raises.
    """
    queue_file = memory_dir.joinpath(*_MERGE_QUEUE_RELPATH)
    if not queue_file.is_file():
        return 0
    try:
        import json as _json
        data = _json.loads(queue_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def legacy_supersede(memory_dir: Path, rule_id: str) -> dict:
    """Operator-explicit LEGACY supersession. Moves rule .md to _LEGACY/
    with v2.3 naming convention.
    """
    rule_path = _resolve_rule_path(memory_dir, rule_id)
    if rule_path is None:
        return {"ok": False, "error": f"rule not found: {rule_id}"}
    action = lifecycle_ladder.legacy_supersede(rule_path)
    return {
        "ok": action.success,
        "rule_id": rule_id,
        "source": action.source_path,
        "target": action.target_path,
        "error": action.error,
    }


def run_lifecycle_sweep(memory_dir: Path) -> dict:
    """Stop-hook-triggered LEGACY→_PURGE (cold storage) archival sweep.
    Move-only, best-effort — archive is terminal, nothing is ever deleted."""
    return lifecycle_ladder.run_full_sweep(memory_dir)


def list_retiring(memory_dir: Path) -> list[dict]:
    return [
        {
            "rule_id": s.rule_id,
            "sessions_remaining": s.sessions_remaining,
            "retrieval_priority_multiplier": s.retrieval_priority_multiplier,
            "rule_path": str(s.rule_path) if s.rule_path else None,
        }
        for s in apoptotic_retirement.list_retiring_rules(memory_dir)
    ]


def finalize_retired(memory_dir: Path) -> list[str]:
    """Move all retirement_state=retired files to _RETIRED/<YYYYMMDD>/. Returns
    list of rule_ids successfully finalized.
    """
    finalized = []
    if not memory_dir.exists():
        return finalized
    for rule_path in memory_dir.rglob("*.md"):
        if "_RETIRED" in rule_path.parts:
            continue
        try:
            text = rule_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if apoptotic_retirement._extract_frontmatter_field(text, "retirement_state", "active") != "retired":
            continue
        archived = apoptotic_retirement.finalize_retirement(rule_path)
        if archived:
            finalized.append(rule_path.stem)
    return finalized


# ---------- helpers ----------

def _is_contained(path: Path, root: Path) -> bool:
    """True iff `path`, after full symlink/`..` resolution, is inside `root`.

    H-15 containment guard: _resolve_rule_path feeds the DESTRUCTIVE
    retire/retain/legacy operations, so a rule_id carrying `../` must never
    escape the memory tree. Compare fully-resolved paths so `../` and symlinks
    can't smuggle the target outside `root`.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


def _resolve_rule_path(memory_dir: Path, rule_id: str) -> Path | None:
    """rule_id is the filename stem (e.g., 'feedback_no_jargon'). Find the .md file.

    H-15: the resolved path MUST stay inside memory_dir. A rule_id containing
    `../` (or any traversal) that would land outside the memory tree is rejected
    (returns None) — this guards the destructive retire/retain/legacy callers.
    """
    if not memory_dir.exists():
        return None
    # Try direct path first — but never accept one that escapes the tree.
    candidate = memory_dir / f"{rule_id}.md"
    if candidate.exists() and _is_contained(candidate, memory_dir):
        return candidate
    # Walk to find (handles subdirectories). Apply the same containment guard to
    # the rglob fallback result.
    for path in memory_dir.rglob(f"{rule_id}.md"):
        if "_RETIRED" in path.parts:
            continue
        if not _is_contained(path, memory_dir):
            continue
        return path
    return None


# ---------- operator-facing formatters ----------

def format_tend_report(report: dict) -> str:
    audit = report["tier_audit"]
    lines = [
        f"=== Allostat memory tree audit — {report['memory_dir']} ===",
        f"",
        f"Total .md files: {audit['total']}",
        f"By tier:",
    ]
    for tier, count in sorted(audit["by_tier"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {tier:18s} {count:>4d}")

    if audit["low_confidence"]:
        lines.append(f"")
        lines.append(f"Low-confidence classifications ({len(audit['low_confidence'])}):")
        for fname, flags in audit["low_confidence"][:20]:
            lines.append(f"  {fname}  flags={flags}")
        if len(audit["low_confidence"]) > 20:
            lines.append(f"  ... + {len(audit['low_confidence']) - 20} more")

    retiring = report.get("retiring_rules", [])
    if retiring:
        lines.append(f"")
        lines.append(f"Rules in deprecation window ({len(retiring)}):")
        for r in retiring:
            lines.append(
                f"  {r['rule_id']:50s} sessions_remaining={r['sessions_remaining']}  "
                f"priority={r['retrieval_priority_multiplier']:.2f}"
            )

    pairs = report.get("convention_pairs", [])
    if pairs:
        lines.append(f"")
        lines.append(
            f"Naming-convention pairs ({len(pairs)}) — one subject, two files, "
            f"both indexed. Merge is your call:"
        )
        for p in pairs[:20]:
            lines.append(f"  {p.get('prefixed', '?')}  <->  {p.get('unprefixed', '?')}")
        if len(pairs) > 20:
            lines.append(f"  ... + {len(pairs) - 20} more")

    awaiting = report.get("awaiting_finalization", [])
    if awaiting:
        lines.append(f"")
        lines.append(f"Awaiting finalization (counter=0, ready to move to _RETIRED/):")
        for f in awaiting:
            lines.append(f"  {f}")

    return "\n".join(lines)


# ---------- CLI entry ----------

def _cli():
    """python -m memory_lifecycle <verb> [args].

    Verbs:
      tend [memory_dir]
      retire <rule_id> [memory_dir] [--reason=<r>]
      retain <rule_id> [memory_dir]
      list [memory_dir]
      finalize [memory_dir]
      tick [memory_dir]    (for session-start; usually not called directly)
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="memory_lifecycle")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_tend = sub.add_parser("tend")
    p_tend.add_argument("memory_dir", nargs="?", default=None)
    p_tend.add_argument("--projects", default="")
    p_tend.add_argument("--json", action="store_true")

    p_retire = sub.add_parser("retire")
    p_retire.add_argument("rule_id")
    p_retire.add_argument("memory_dir", nargs="?", default=None)
    p_retire.add_argument("--reason", default="operator_directed")

    p_retain = sub.add_parser("retain")
    p_retain.add_argument("rule_id")
    p_retain.add_argument("memory_dir", nargs="?", default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("memory_dir", nargs="?", default=None)

    p_fin = sub.add_parser("finalize")
    p_fin.add_argument("memory_dir", nargs="?", default=None)

    p_tick = sub.add_parser("tick")
    p_tick.add_argument("memory_dir", nargs="?", default=None)

    p_bulk = sub.add_parser("bulk-retire")
    p_bulk.add_argument("rule_ids", help="comma-separated rule ids")
    p_bulk.add_argument("memory_dir", nargs="?", default=None)
    p_bulk.add_argument("--reason", default="operator_directed_bulk")

    p_orphans = sub.add_parser("orphans")
    p_orphans.add_argument("memory_dir", nargs="?", default=None)

    p_reorder = sub.add_parser("reorder")
    p_reorder.add_argument("by", choices=["age", "tier"])
    p_reorder.add_argument("memory_dir", nargs="?", default=None)

    p_legacy = sub.add_parser("legacy")
    p_legacy.add_argument("rule_id")
    p_legacy.add_argument("memory_dir", nargs="?", default=None)

    p_sweep = sub.add_parser("sweep")
    p_sweep.add_argument("memory_dir", nargs="?", default=None)

    p_rebuild = sub.add_parser("rebuild-index")
    p_rebuild.add_argument("memory_dir", nargs="?", default=None)

    p_merge = sub.add_parser("merge-candidates")
    p_merge.add_argument("memory_dir", nargs="?", default=None)

    p_audit = sub.add_parser("audit-tiers")
    p_audit.add_argument("memory_dir", nargs="?", default=None)
    p_audit.add_argument("--projects", default="")

    sub.add_parser("archipelago")

    sub.add_parser("check-symlinks")

    args = parser.parse_args()
    # Some verbs don't need a memory_dir (archipelago, check-symlinks
    # operate on different state surfaces).
    _MEMDIR_OPTIONAL = {"archipelago", "check-symlinks"}
    if args.verb in _MEMDIR_OPTIONAL:
        memory_dir = None
    else:
        memory_dir_arg = getattr(args, "memory_dir", None)
        memory_dir = Path(memory_dir_arg) if memory_dir_arg else _default_memory_dir()
        if memory_dir is None:
            print("error: could not resolve memory dir; pass as positional arg", file=sys.stderr)
            sys.exit(2)

    if args.verb == "tend":
        projects = [p.strip() for p in args.projects.split(",") if p.strip()] or None
        report = tend(memory_dir, known_projects=projects)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(format_tend_report(report))
    elif args.verb == "retire":
        result = retire(memory_dir, args.rule_id, reason=args.reason)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    elif args.verb == "retain":
        result = retain(memory_dir, args.rule_id)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    elif args.verb == "list":
        retiring = list_retiring(memory_dir)
        print(json.dumps(retiring, indent=2))
    elif args.verb == "finalize":
        finalized = finalize_retired(memory_dir)
        print(json.dumps({"finalized": finalized}, indent=2))
    elif args.verb == "tick":
        result = tick_at_session_start(memory_dir)
        print(json.dumps(result, indent=2))
    elif args.verb == "bulk-retire":
        ids = [x.strip() for x in args.rule_ids.split(",") if x.strip()]
        result = bulk_retire(memory_dir, ids, reason=args.reason)
        print(json.dumps(result, indent=2))
        sys.exit(0 if not result.get("failed") else 1)
    elif args.verb == "orphans":
        result = list_orphans(memory_dir)
        print(json.dumps({"orphans": result, "count": len(result)}, indent=2))
    elif args.verb == "reorder":
        result = reorder_memory_md(memory_dir, by=args.by)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    elif args.verb == "legacy":
        result = legacy_supersede(memory_dir, args.rule_id)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    elif args.verb == "sweep":
        result = run_lifecycle_sweep(memory_dir)
        # Convert dataclass list values to dicts for JSON serialization
        serializable = {
            k: [
                {"source": a.source_path, "target": a.target_path, "error": a.error}
                if hasattr(a, "source_path") else a
                for a in v
            ]
            for k, v in result.items()
        }
        print(json.dumps(serializable, indent=2))
    elif args.verb == "rebuild-index":
        result = rebuild_memory_md_index(memory_dir)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)
    elif args.verb == "merge-candidates":
        cands = auto_merge_proposer.find_merge_candidates(memory_dir)
        print(auto_merge_proposer.format_candidates_report(cands))
    elif args.verb == "audit-tiers":
        projects = [p.strip() for p in args.projects.split(",") if p.strip()] or None
        report = hierarchy_validator.audit_tier_integrity(memory_dir, known_projects=projects)
        print(hierarchy_validator.format_audit_report(report))
        sys.exit(0 if report.get("audit_passed") else 1)
    elif args.verb == "archipelago":
        print(archipelago_view.render_archipelago_view())
    elif args.verb == "check-symlinks":
        roots = stale_link_cleanup.default_scan_roots()
        stale = stale_link_cleanup.scan_for_stale_links(roots)
        print(stale_link_cleanup.format_stale_links_report(stale))


def resolve_memory_dir(project_root: Path | None = None) -> Path | None:
    """Resolve Claude Code's auto-loaded memory tree path for a given project.

    Claude Code stores per-project memory at
    ~/.claude/projects/<sanitized-cwd>/memory/ where <sanitized-cwd> is the
    cwd path with ':' → '-', '\\' → '-', '/' → '-', and ' ' → '-'.

    PATCH-183.2 (2026-05-23): delegate sanitization to
    `session_handoff.sanitize_cwd_for_harness` (the canonical PATCH-181
    helper). Pre-fix this function STRIPPED the colon (rather than
    replacing it) AND did not handle whitespace in the cwd, so any cwd
    containing a space (drive + folder-with-space pattern) computed a
    sanitized form that didn't match the harness's actual path shape.
    Manifested as the handoff_consolidation CLI failing with "could
    not resolve project_root / state_dir / mem_dir from cwd" — even
    though state_dir resolution succeeded, this function returned None.

    Args:
        project_root: project's working directory. If None, uses os.getcwd().

    Returns:
        Path to the memory dir if it exists, otherwise None.
    """
    import os
    if project_root is None:
        project_root = Path(os.getcwd())
    cwd = Path(project_root).resolve()
    # 2026-05-31: delegate to the single memory-root chokepoint (project-rooted
    # with legacy harness fallback). Co-bundled; ImportError is not handled — a
    # broken install must surface, not silently degrade.
    from session_handoff import resolve_memory_root  # noqa: E402
    candidate = resolve_memory_root(cwd)
    if candidate.exists():
        return candidate
    return None


def _default_memory_dir() -> Path | None:
    """Backwards-compat alias for resolve_memory_dir(None)."""
    return resolve_memory_dir(None)


if __name__ == "__main__":
    _cli()
