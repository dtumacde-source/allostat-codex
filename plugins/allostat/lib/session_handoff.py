"""Session AUDIT writer with fingerprint dedup + per-project routing.

A2 REDIRECT (2026-05-24): writes aggregate-signal audit files to the
per-project audit tree (`~/.claude/projects/<sanitized-cwd>/.allostat/
audit/<ts>_session_end.md`). PRE-A2 these files wrote to
`~/.claude/projects/<sanitized-cwd>/memory/handoffs/` and squatted the
canonical handoff path. The canonical path now belongs to Claude-authored
rolling handoffs per the A3 protocol; the wrapper's auto-rendered
aggregate signal lives at the audit path for behavioral observability.

Function names retain "handoff" in their public API for backward compat
with callers (Stop hook, signal-phrase forced-write path), but the
written content is the wrapper's auto-rendered session_debrief output
(files touched, operator messages, caught/shaped/quiet) — not a
continuity handoff.

Fingerprint dedup on `(session_id, n_observations_at_prior_handoff)` so
a fresh audit file lands only when meaningful new work has accumulated.

Called from the Stop hook ABOVE the `_is_repeat_stop` debounce gate so the
write fires on every Stop boundary; the dedup keys decide whether to write.

See dev_patch_log.md (PATCH-181 origin, PATCH-FIXNOW-A2 redirect) for
design rationale.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
import sys
from pathlib import Path

from local_state import utc_iso


def _is_reparse_dir(p: Path) -> bool:
    """True if p is a symlink OR a Windows directory junction (reparse point).

    Path.is_symlink() returns False for a junction (reparse tag
    IO_REPARSE_TAG_MOUNT_POINT), so gating the walk on is_symlink() alone would
    still descend through a junction — contradicting _iter_files_safe's "never
    follow junctions" contract (ultraswarm). Junctions carry
    FILE_ATTRIBUTE_REPARSE_POINT, which lstat() exposes on Windows.
    """
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    try:
        attrs = getattr(p.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


# A2 (2026-05-24): audit path replaces handoffs path for auto-rendered files.
_AUDIT_SUBDIR_PARTS = (".allostat", "audit")
_HARNESS_HANDOFFS_SUBDIR = "handoffs"  # preserved for legacy fallback (pre-A2 envs)
_HANDOFF_STATE_FILENAME = "last_handoff_session.json"

# Workstream C (R3): marker identifying a regenerated harness MEMORY.md pointer.
_HARNESS_POINTER_MARKER = "<!-- allostat:project-rooted-memory-pointer -->"


#: Characters the Claude Code harness folds to "-" when naming a project
#: directory under `~/.claude/projects/`.
#:
#: `_` was missing until 2026-08-07 (ISSUE-004). The harness maps it; Allostat
#: did not, so ANY underscore in a cwd made the two disagree and every path
#: derived from the sanitized name pointed at a directory that does not exist:
#:
#:   /home/example/project_under_home  ->  Allostat: -home-example-project_under_home
#:                                   harness:  -home-example-project-under-home
#:
#: Two consumers derive from that name — `_harness_memory_root` (the fallback
#: tree for un-migrated projects, which then read blank where memory existed)
#: and the A2 audit path, whose files piled up in a phantom directory.
#: Underscored folder names are ordinary (`my_project`), so this was not exotic.
_HARNESS_SANITIZE_CHARS: tuple[str, ...] = (":", "\\", "/", " ", "_")


def sanitize_cwd_for_harness(cwd: Path | str) -> str:
    """Convert a cwd path into the harness's sanitized form.

    Example: `C:\\Users\\<user>\\<project>` becomes
    `C--Users---user---project-` (colons, backslashes, forward slashes,
    spaces and underscores all become dashes). The Claude Code harness uses
    this sanitization when computing the per-project memory tree path
    under `~/.claude/projects/`.
    """
    raw = str(cwd)
    for ch in _HARNESS_SANITIZE_CHARS:
        raw = raw.replace(ch, "-")
    return raw


def legacy_sanitize_cwd_for_harness(cwd: Path | str) -> str:
    """The pre-2026-08-07 form, which left `_` intact.

    Read-time alias only. Directories were really written under this name
    before the fix — audit trees and legacy fallback memory — and correcting
    the sanitizer must not orphan them. Callers that RESOLVE a harness path
    should try the canonical name first and fall back to this one; callers
    that CREATE one must always use the canonical form.
    """
    raw = str(cwd)
    for ch in (":", "\\", "/", " "):
        raw = raw.replace(ch, "-")
    return raw


def harness_dir_candidates(cwd: Path | str) -> list[str]:
    """Sanitized directory names to try when RESOLVING a harness path, in
    priority order: the canonical name first, then the legacy underscore form
    if it differs. Deduplicated, so the common case is a single candidate."""
    canonical = sanitize_cwd_for_harness(cwd)
    legacy = legacy_sanitize_cwd_for_harness(cwd)
    return [canonical] if canonical == legacy else [canonical, legacy]


def _harness_memory_root(project_root: Path) -> Path:
    """The legacy harness-rooted memory tree:
    `~/.claude/projects/<sanitized-cwd>/memory`. Kept as the FALLBACK target
    for cwds that have not migrated to in-project memory.

    Resolves through `harness_dir_candidates`, so a tree written under the
    pre-2026-08-07 underscore form is still found. An EXISTING legacy tree
    wins over a canonical path that does not exist yet — otherwise correcting
    the sanitizer would read blank for exactly the projects that already had
    memory there. When neither exists, the canonical name is returned, so
    anything created from here is created correctly.
    """
    projects = Path.home() / ".claude" / "projects"
    candidates = harness_dir_candidates(project_root)
    for name in candidates:
        candidate = projects / name / "memory"
        if candidate.is_dir():
            return candidate
    return projects / candidates[0] / "memory"


def _iter_files_safe(root: Path, suffix: str | None = None, max_depth: int = 25):
    """Yield files under `root` WITHOUT descending into symlinked/junction dirs.

    pathlib.rglob FOLLOWS directory symlinks/junctions on py3.11/3.12, so a
    junction loop inside a memory tree would hang session start. This manual
    walk skips any symlinked directory and caps recursion depth. Best-effort:
    unreadable directories are skipped rather than raising.
    """
    stack = [(Path(root), 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            # Best-effort: skip an unreadable directory, keep walking the rest.
            continue
        for e in entries:
            try:
                if e.is_dir():
                    if _is_reparse_dir(e) or depth >= max_depth:
                        continue  # never follow junctions/symlinks; cap depth
                    stack.append((e, depth + 1))
                elif suffix is None or e.name.endswith(suffix):
                    yield e
            except OSError:
                # Best-effort: a single bad entry doesn't abort the walk.
                continue


def _is_harness_path(p: Path) -> bool:
    """True if `p` is itself inside the harness tree `~/.claude/projects/`.

    Callers (e.g. hooks/_base.resolve_project_root_from_payload) can fall back
    to `transcript.parent` — which IS a `~/.claude/projects/<encoded>/` dir —
    when no clean cwd/.allostat is available. Such a path is NOT a real
    opened-from project folder; treating it as one would root memory at
    `<harness>/memory`, re-creating the harness-tree coupling this design
    removes. Detect it so resolve_memory_root handles it correctly.
    """
    try:
        projects_root = (Path.home() / ".claude" / "projects").resolve()
        rp = Path(p).resolve()
    except OSError:
        return False
    return rp == projects_root or projects_root in rp.parents


#: A harness tree directory name is produced by `sanitize_cwd_for_harness`,
#: which turns `:` `\` `/` and spaces into `-`. Every such name therefore starts
#: with a drive-letter pair (`C--Users-…`) on Windows or a leading dash
#: (`-home-…`) on POSIX. A direct child of `~/.claude/projects/` that does NOT
#: match this shape was not generated by the harness — it is a folder somebody
#: created, i.e. a project opened inside Claude's own namespace.
_SANITIZED_TREE_NAME = re.compile(r"^([A-Za-z]--|-)")


def is_project_opened_inside_harness_namespace(project_root: Path) -> bool:
    """True for a REAL project folder living under `~/.claude/projects/`.

    Distinguishes the two things `_is_harness_path` lumps together: a harness
    bookkeeping tree (`C--work-ExampleProject`, generated) from a directory a human made
    and opened as a project (`ExampleProject`, `Creative Studio`). Only the second is a
    problem, and only the second should be surfaced — warning on every legacy
    harness tree would be noise, since un-migrated projects legitimately read
    from one.
    """
    if not _is_harness_path(project_root):
        return False
    try:
        projects_root = (Path.home() / ".claude" / "projects").resolve()
        rp = Path(project_root).resolve()
    except OSError:
        return False
    if rp == projects_root:
        return False
    # Only judge the segment directly under `projects/`; anything deeper is
    # inside some tree already.
    try:
        first = rp.relative_to(projects_root).parts[0]
    except (ValueError, IndexError):
        return False
    return not _SANITIZED_TREE_NAME.match(first)


def harness_namespace_project_warning(project_root: Path) -> str:
    """Operator-facing warning when a project is rooted inside `~/.claude/`.

    Returns "" when there is nothing to say, so callers can inject
    unconditionally.

    Why a warning and not a redirect (2026-08-04): nothing here can know where
    the real project lives. When the operator hit this, `~/.claude/projects/
    ExampleProject` held a five-week-old copy while the live project was at
    `C:\\work\\ExampleProject` — a mapping only its owner knew. Silently picking either one is
    how two divergent trees for one project happen. Detection worked in this
    class of bug before; the reporting path is what was missing.
    """
    if not is_project_opened_inside_harness_namespace(project_root):
        return ""
    root = Path(project_root)
    return (
        "⚠ This project is opened INSIDE Claude's own directory "
        f"(`{root}`).\n"
        "That path is the harness's namespace, not a project folder, so this "
        "session's memory and handoffs are being written somewhere that does "
        "not travel with the project and that a session opened from the real "
        "project folder will never see.\n"
        "If this project also exists elsewhere (e.g. under `C:\\dev\\`), the "
        "two are diverging right now. Move the project out of "
        "`~/.claude/projects/` and reopen it from its real location."
    )


def _tree_has_memory(memory_dir: Path) -> bool:
    """True if `memory_dir` is a directory holding at least one NON-EMPTY
    `.md` file (searched recursively).

    A whitespace-only file counts as empty — the Tier-4 scaffolder can leave
    blank `MEMORY.md`/`_PURPOSE.md` templates behind, and a folder move can
    leave an empty `memory/` dir. `resolve_memory_root` uses this so such an
    empty tree never SHADOWS a populated one (the complete-failure trap).

    Best-effort: any OSError is treated as "no memory" so the caller falls
    back rather than raising inside the resolution chokepoint.
    """
    try:
        if not memory_dir.is_dir():
            return False
        for md in _iter_files_safe(memory_dir, ".md"):
            try:
                if md.is_file() and md.read_text(
                    encoding="utf-8-sig", errors="replace"
                ).strip():
                    return True
            except OSError:
                # Best-effort: an unreadable file simply doesn't count as
                # memory; keep scanning the rest of the tree.
                continue
        return False
    except OSError:
        return False


def resolve_memory_root(project_root: Path) -> Path:
    """SINGLE CHOKEPOINT for where a project's memory tree lives (2026-05-31).

    Project-rooted by design: memory belongs in `<project>/memory/` so the
    whole project is self-contained and zip-portable (operator directive
    2026-05-31). This mirrors `.allostat/` state, which already roots in the
    project folder.

    Resolution (in-project with safe fallback):
      0. If `project_root` is ITSELF a harness path (`~/.claude/projects/<enc>/`,
         from a caller's transcript-parent fallback) → it already IS the harness
         project dir; memory is `<that>/memory`. Do NOT misread it as a clean
         in-project root. (v1.4.43 fix — closes the cross-layer contradiction
         where an older caller handed a harness path and memory rooted back
         under ~/.claude, defeating the project-rooted goal.)
      1. Else if `<project>/memory/` is POPULATED → use it (migrated project).
      2. Else if the legacy harness tree is POPULATED → use it (un-migrated
         project, OR a folder move that left an empty in-project dir behind).
      3. Else (brand-new project) → default to in-project `<project>/memory/`.

    SAFETY fail-safe (2026-06-19): steps 1/2 test for a POPULATED tree, not a
    merely-existing directory. An empty `<project>/memory/` (left by a folder
    move or a bare scaffold) must NEVER shadow a populated harness tree — doing
    so would open a session with zero memory, the one path to a complete
    failure of Allostat. When neither tree is populated, the in-project path is
    returned and an empty result is CORRECT (a genuinely new project).

    Every memory/handoff path in the wrapper derives from THIS function, so the
    root can never fragment into divergent sanitized-cwd trees again.
    """
    project_root = Path(project_root)
    # 0. A path inside `~/.claude/projects/`. TWO different things land here and
    #    they need different answers (2026-08-04):
    #
    #    (a) A SANITIZED TREE DIR (`C--work-ExampleProject`) handed in by an older
    #        caller's transcript-parent fallback. That IS the harness project
    #        dir; its `memory` subdir is the legacy tree and un-migrated
    #        projects still read from it. Unchanged behaviour.
    #
    #    (b) A GENUINE cwd that merely sits under the harness namespace
    #        (`~/.claude/projects/ExampleProject`, `.../Creative Studio`) — someone
    #        opened a project inside Claude's own directory. Until today this
    #        took branch (a) and rooted memory AND every handoff at
    #        `<harness>/…/memory`, because every path in the wrapper derives
    #        from this function. The operator hit it on 2026-08-04: a live
    #        project's handoffs were written into the harness tree and he moved
    #        them by hand. His rule — handoffs live in the project folder, and
    #        nothing under `~/.claude/` is a project folder.
    #
    #    Case (b) cannot be resolved silently: nothing here can know that the
    #    real project is at `C:\work\ExampleProject`, and guessing is what produced two
    #    divergent trees for one project. It is surfaced instead — see
    #    `harness_namespace_project_warning()`, which session-start renders.
    if _is_harness_path(project_root):
        return project_root / "memory"
    in_project = project_root / "memory"
    # 1. Prefer in-project — but only when it actually holds memory, so an
    #    empty dir can never shadow real content elsewhere.
    if _tree_has_memory(in_project):
        return in_project
    # 2. Fall back to a POPULATED legacy harness tree (un-migrated project, or
    #    a move that left an empty in-project dir). Never serve blank when the
    #    real memory still exists.
    legacy = _harness_memory_root(project_root)
    if _tree_has_memory(legacy):
        return legacy
    # 3. Nothing populated anywhere → in-project (new project; blank is correct).
    return in_project


_LAST_UPDATE_RE = re.compile(
    r"Last update:\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)"
)
_DATED_NAME_RE = re.compile(r"^\d{8}[_T]")


def _last_update_stamp(text: str) -> str | None:
    """A handoff's in-file 'Last update: <ISO>' stamp — survives copy/zip, unlike
    filesystem mtime. None if absent/unparseable. ISO form sorts lexicographically."""
    m = _LAST_UPDATE_RE.search(text)
    return m.group(1) if m else None


def _stamp_dt(stamp: str) -> datetime | None:
    """Parse a 'Last update' stamp into a tz-aware datetime so handoffs are
    compared by TIME, not by string sort. The regex accepts both 'T' and space
    separators and an optional 'Z'; string-sorting those forms is wrong (a space
    0x20 sorts BEFORE 'T' 0x54), so a newer space-form stamp could lose. Returns
    None if unparseable, so the caller falls back to mtime."""
    t = stamp.strip().replace(" ", "T", 1)
    if t.endswith("Z"):
        t = t[:-1]
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _reconcile_winner(harness_file: Path, inproject_file: Path, rel: Path) -> str:
    """Arbitrate a DIVERGED harness/in-project pair → 'harness' | 'inproject'.

    R3 arbiter (advisor + allocouncil 2026-06-18):
      - empty/whitespace NEVER clobbers non-empty (the blank-scaffold trap);
      - handoffs: newest by in-file 'Last update' stamp (mtime fallback);
      - date-prefixed files: add-only — in-project kept (never reconciled);
      - everything else: newest mtime wins (C-wins-on-conflict, same machine).
    """
    try:
        h_text = harness_file.read_text(encoding="utf-8-sig", errors="replace")
        i_text = inproject_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return "inproject"  # unreadable → keep what we have
    if h_text == i_text:
        return "inproject"  # identical → no-op
    h_blank, i_blank = (not h_text.strip()), (not i_text.strip())
    if h_blank and not i_blank:
        return "inproject"
    if i_blank and not h_blank:
        return "harness"
    # Fix #2 (+ #5): the operator-canonical files — MEMORY.md (the index) and
    # _PURPOSE.md (the purpose) — are authoritative in the project. A harness
    # copy (a stale pre-flip version, a newer-mtime AUTO-WRITE, or a freshly
    # SCAFFOLDED template) must NEVER clobber them. This generalizes the
    # blank-scaffold trap: the empty-guard above only catches *whitespace*-empty
    # files, but the Tier-4 scaffolder writes NON-empty templates ("TODO: ...")
    # whose newer mtime would otherwise win mtime-newest-wins and overwrite real
    # content (observed clobbering Allostat's real _PURPOSE.md with the scaffold).
    # The empty-guard above still lets a real harness copy populate an EMPTY
    # project one (first migration). These two are the only scaffolded files.
    if rel.name in ("MEMORY.md", "_PURPOSE.md"):
        return "inproject"
    parts = rel.parts
    if parts and parts[0] == _HARNESS_HANDOFFS_SUBDIR and rel.suffix == ".md":
        h_stamp, i_stamp = _last_update_stamp(h_text), _last_update_stamp(i_text)
        if h_stamp and i_stamp:
            # Compare as DATETIMES, not strings — mixed 'T'/space/Z forms sort
            # wrong as strings (reconcile-1). Tie -> harness (C-wins, same machine).
            h_dt, i_dt = _stamp_dt(h_stamp), _stamp_dt(i_stamp)
            if h_dt and i_dt:
                return "inproject" if i_dt > h_dt else "harness"
            # one/both unparseable -> distrust the stamp, fall through to mtime
        elif h_stamp and not i_stamp:
            # one parseable stamp beats an absent/reset one — stronger than mtime.
            return "harness"
        elif i_stamp and not h_stamp:
            return "inproject"
        # neither / unparseable -> fall through to mtime
    elif _DATED_NAME_RE.match(rel.name):
        return "inproject"  # add-only
    try:
        h_m, i_m = harness_file.stat().st_mtime, inproject_file.stat().st_mtime
    except OSError:
        return "inproject"
    return "inproject" if i_m > h_m else "harness"  # Fix #3: tie -> C-wins


def _archive_reconcile_loser(in_project: Path, rel: Path, current: Path) -> None:
    """Before overwriting an in-project file, archive its current bytes under
    `_LEGACY_reconcile/<rel>` (never destroy). Non-colliding."""
    import shutil

    archive = in_project / "_LEGACY_reconcile" / rel
    archive.parent.mkdir(parents=True, exist_ok=True)
    candidate, n = archive, 0
    while candidate.exists():
        n += 1
        candidate = archive.with_name(f"{archive.name}.{n}")
    shutil.copy2(current, candidate)


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy src->dst atomically: copy to a temp sibling then os.replace, so an
    interrupted copy can never leave a truncated live file (reconcile-3)."""
    import shutil
    tmp = dst.with_name(dst.name + ".allostat-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


_HARNESS_OWNER_MARKER = ".allostat_harness_owner"


def migrate_harness_memory_to_in_project(project_root: Path) -> dict:
    """Self-healing migration: if a legacy harness memory tree exists for this
    project but the in-project `<project>/memory/` does not (or is partial),
    COPY the harness tree into the project folder so the project becomes
    self-contained / zip-portable.

    Why this exists (2026-06-01): the writer (scaffolder) historically created
    ONLY the harness tree while the reader (resolve_memory_root) preferred
    in-project — so the in-project branch could never win because nothing ever
    populated it. Every non-hand-migrated project stayed stranded on the
    harness tree. This converts the one-time legacy
    case into a self-healing migration that runs at session start.

    Discipline (feedback_migration_never_destroys_existing_files):
      - COPY, never move. The harness tree is left intact as a backup.
      - RECONCILE newest-wins (R3, 2026-06-18): a DIVERGED file is resolved by
        the arbiter (`_reconcile_winner`) — handoffs by in-file 'Last update'
        stamp, date-prefixed files add-only, else newest mtime; an empty file
        never clobbers a non-empty one. When the harness wins, the overwritten
        in-project copy is archived to `_LEGACY_reconcile/` first (never
        destroyed). Pre-R3 this was copy-missing-only, which stranded
        harness-newer EDITS in-project and leaked stale reads.
      - A `.migrated_from_harness` marker is written in-project for provenance.
      - Best-effort: never raises; returns a result dict for observation.

    Returns a dict: {migrated: bool, copied: int, skipped_existing: int,
    skipped_reason: str|None, source: str|None, dest: str|None}.
    """
    import shutil

    result = {
        "migrated": False, "copied": 0, "skipped_existing": 0, "reconciled": 0,
        "errors": 0, "error_paths": [],
        "skipped_reason": None, "source": None, "dest": None,
    }
    project_root = Path(project_root)
    # Never migrate a harness path onto itself.
    if _is_harness_path(project_root):
        result["skipped_reason"] = "project_root_is_harness_path"
        return result

    harness = _harness_memory_root(project_root)
    in_project = project_root / "memory"
    result["source"] = str(harness)
    result["dest"] = str(in_project)

    if not harness.is_dir():
        result["skipped_reason"] = "no_harness_tree"
        return result

    # Provenance guard (resolver-3): sanitize_cwd_for_harness can map two
    # distinct cwds (e.g. 'my app' vs 'my-app') to the SAME harness tree. Refuse
    # to migrate a harness tree a DIFFERENT project already claimed, so one
    # project's memory can never be copied into another's folder. The owner
    # marker lives in the (non-portable) harness tree, not the shipped project.
    owner = str(project_root.resolve())
    prov = harness / _HARNESS_OWNER_MARKER
    try:
        recorded = prov.read_text(encoding="utf-8-sig").strip() if prov.is_file() else ""
    except OSError:
        recorded = ""
    if recorded and recorded != owner:
        result["skipped_reason"] = "harness_provenance_mismatch"
        return result

    try:
        in_project.mkdir(parents=True, exist_ok=True)
        if not recorded:
            try:
                prov.write_text(owner + "\n", encoding="utf-8")
            except OSError:
                # Best-effort: failing to stamp ownership must not block migration.
                pass
        for src in _iter_files_safe(harness):
            try:
                rel = src.relative_to(harness)
                # Never carry our own provenance marker into the project tree.
                if rel.name == _HARNESS_OWNER_MARKER:
                    continue
                # Confidential carve-out: never materialize *_sandbox/ content
                # into the zip-portable project tree.
                if any("_sandbox" in part for part in rel.parts):
                    result["skipped_existing"] += 1
                    continue
                # Never carry a regenerated harness pointer (Workstream C output)
                # back into the project tree — it is a pointer, not memory.
                if rel == Path("MEMORY.md") and _is_harness_pointer_file(src):
                    result["skipped_existing"] += 1
                    continue
                dst = in_project / rel
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_copy(src, dst)
                    result["copied"] += 1
                    continue
                # dst exists → RECONCILE (R3).
                if _reconcile_winner(src, dst, rel) == "harness":
                    _archive_reconcile_loser(in_project, rel, dst)  # never destroy
                    _atomic_copy(src, dst)
                    result["reconciled"] += 1
                else:
                    result["skipped_existing"] += 1
            except OSError as e:
                # reconcile-3: one bad file (lock, disk full, long path) must NOT
                # strand the rest of the migration. Count it and keep going.
                result["errors"] += 1
                result["error_paths"].append(str(src))
                continue
        # Provenance marker (only when we actually moved content in).
        if result["copied"] > 0 or result["reconciled"] > 0:
            marker = in_project / ".migrated_from_harness"
            if not marker.exists():
                marker.write_text(
                    # privacy-2: no absolute source path — this marker travels in
                    # the zip-portable project tree, so it must not leak the
                    # operator's home/username/machine layout.
                    "Migrated from the Claude Code harness memory tree on first "
                    "project-rooted session. Harness copy left intact as backup.\n",
                    encoding="utf-8",
                )
        result["migrated"] = (result["copied"] + result["reconciled"]) > 0
    except OSError as e:
        result["skipped_reason"] = f"oserror:{e}"
    return result


def _is_harness_pointer_file(p: Path) -> bool:
    """True if `p` is a regenerated harness MEMORY.md pointer, not real memory."""
    try:
        return _HARNESS_POINTER_MARKER in p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _harness_pointer_text(in_project: Path) -> str:
    """The thin pointer written at the harness MEMORY.md once memory is
    project-rooted. Thin by design — the real index is injected from the project
    tree, so this stays small (no double-load) and just orients the agent."""
    return (
        f"{_HARNESS_POINTER_MARKER}\n"
        "# Memory moved — this project is project-rooted\n\n"
        f"This project's memory now lives in `{in_project}` and travels with the "
        "project (zip-portable). Allostat injects the real index from there at "
        "session start.\n\n"
        "**Do not read or write memory here.** This harness file is a regenerated "
        "pointer, not a memory store; any edit made here is reconciled into the "
        "project tree at the next session start. Edit memory in the project folder "
        "instead.\n"
    )


def regenerate_harness_pointer(project_root: Path) -> dict:
    """Workstream C (R3): keep the harness `MEMORY.md` as a thin POINTER to the
    project-rooted tree — regenerated each session, a LIVE mirror, NOT a tombstone
    (tombstone is deferred per the allocouncil). The Claude Code harness always
    auto-loads `~/.claude/projects/<cwd>/memory/MEMORY.md`; once memory is
    project-rooted we want that path to surface a current pointer (cheap, no
    double-load of the index) instead of a stale full copy, and we never treat it
    as an independent write target — which is what sidesteps the drain-war.

    Acts ONLY when memory is genuinely project-rooted (`resolve_memory_root` points
    in-project AND a non-empty project MEMORY.md exists). The original harness
    MEMORY.md is archived ONCE (`MEMORY.md.PRE_POINTER`) before the first overwrite
    — never destroyed. Idempotent + best-effort: never raises.
    """
    result = {"regenerated": False, "skipped_reason": None}
    project_root = Path(project_root)
    if _is_harness_path(project_root):
        result["skipped_reason"] = "project_root_is_harness_path"
        return result
    try:
        in_project = project_root / "memory"
        project_index = in_project / "MEMORY.md"
        if resolve_memory_root(project_root) != in_project or not project_index.is_file():
            result["skipped_reason"] = "not_project_rooted"
            return result
        if not project_index.read_text(encoding="utf-8-sig", errors="replace").strip():
            result["skipped_reason"] = "empty_project_index"
            return result
        harness = _harness_memory_root(project_root)
        if not harness.is_dir():
            result["skipped_reason"] = "no_harness_tree"
            return result
        wrote, skip = _write_pointer_into(harness, in_project)
        if wrote:
            result["regenerated"] = True
        else:
            result["skipped_reason"] = skip
    except OSError as e:
        result["skipped_reason"] = f"oserror:{e}"
    return result


def _write_pointer_into(
    harness_memory_dir: Path, in_project: Path
) -> tuple[bool, str | None]:
    """Write/refresh the do-not-read-here pointer MEMORY.md inside ONE
    harness-side memory dir. Extracted verbatim from
    `regenerate_harness_pointer` (D1, 2026-08-03) so the orphan reconciler
    below shares the exact archival semantics: the original real index is
    archived ONCE (`MEMORY.md.PRE_POINTER`) before the first overwrite —
    never destroyed. Returns (wrote, skipped_reason)."""
    harness_index = harness_memory_dir / "MEMORY.md"
    pointer = _harness_pointer_text(in_project)
    if harness_index.is_file():
        existing = harness_index.read_text(encoding="utf-8", errors="replace")
        if existing == pointer:
            return False, "already_pointer"
        # Archive the ORIGINAL real index once, before the first overwrite.
        if _HARNESS_POINTER_MARKER not in existing:
            backup = harness_memory_dir / "MEMORY.md.PRE_POINTER"
            if not backup.exists():
                import shutil
                shutil.copy2(harness_index, backup)
    harness_index.write_text(pointer, encoding="utf-8")
    return True, None


def orphan_harness_candidates(project_root: Path) -> list[Path]:
    """Every `~/.claude/projects/<name>/memory` a stray encoding of THIS
    project could have created (D1, 2026-08-03). Three observed shapes:

      1. sanitized cwd            — `C--work-ExampleProject` (the standard tree;
                                    the migration already tends it)
      2. bare project name        — `ExampleProject` (a caller handed a NAME or
                                    the harness filed a no-cwd session there)
      3. double-encoded harness   — `C--Users-<user>--claude-projects-ExampleProject`
                                    (something sanitized the harness path
                                    itself)

    Returns existing candidate memory dirs, standard encoding excluded (the
    ordinary migration owns it). Read-only; the reconciler decides writes.
    """
    project_root = Path(project_root)
    projects = Path.home() / ".claude" / "projects"
    standard = sanitize_cwd_for_harness(project_root)
    names = {
        project_root.name,                                   # bare
        sanitize_cwd_for_harness(projects / standard),       # double-encoded
    }
    out: list[Path] = []
    for name in sorted(names):
        if not name or name == standard:
            continue
        candidate = projects / name / "memory"
        try:
            if candidate.is_dir():
                out.append(candidate)
        except OSError:
            # Best-effort: an unstatable candidate simply isn't an orphan we
            # can tend; enumeration must never break session start.
            continue
    return out


def reconcile_orphan_harness_trees(project_root: Path) -> dict:
    """Stamp the do-not-read-here pointer into every populated ORPHAN
    harness tree for this project (D1, 2026-08-03).

    The ExampleProject incident: a no-cwd session resolved memory into the bare
    `projects/ExampleProject` tree; the banner then read that orphan and presented
    a 5-week-old handoff as current state while the live project tree was
    11 minutes old. The standard-encoding tree already carries the pointer
    (`regenerate_harness_pointer`); the stray encodings carried nothing.

    Same gate as the ordinary pointer: acts only when memory is genuinely
    project-rooted with a non-empty index. Content is never moved or
    deleted — the pointer marks, the archival keeps. Best-effort per tree;
    never raises.
    """
    result: dict = {"stamped": [], "skipped": []}
    project_root = Path(project_root)
    if _is_harness_path(project_root):
        result["skipped"].append(("*", "project_root_is_harness_path"))
        return result
    in_project = project_root / "memory"
    try:
        index_ok = (
            resolve_memory_root(project_root) == in_project
            and (in_project / "MEMORY.md").is_file()
            and (in_project / "MEMORY.md")
            .read_text(encoding="utf-8-sig", errors="replace").strip() != ""
        )
    except OSError:
        index_ok = False
    if not index_ok:
        result["skipped"].append(("*", "not_project_rooted"))
        return result
    for candidate in orphan_harness_candidates(project_root):
        try:
            if not _tree_has_memory(candidate):
                result["skipped"].append((str(candidate), "empty"))
                continue
            wrote, skip = _write_pointer_into(candidate, in_project)
            if wrote:
                result["stamped"].append(str(candidate))
            else:
                result["skipped"].append((str(candidate), skip))
        except OSError as e:
            result["skipped"].append((str(candidate), f"oserror:{e}"))
    return result


def resolve_audit_dir(project_root: Path) -> Path:
    """A2 (2026-05-24): return the per-project AUDIT subdirectory.

    The auto-rendered aggregate-signal file lives at `.allostat/audit/`, which
    already roots in the project folder (mirrors `resolve_state_dir`). Pre-A2
    it squatted `memory/handoffs/`; post-A2 that canonical path belongs to
    Claude-authored handoffs.
    """
    return Path(project_root) / _AUDIT_SUBDIR_PARTS[0] / _AUDIT_SUBDIR_PARTS[1]


def resolve_canonical_handoff_dir(project_root: Path) -> Path:
    """A2 (2026-05-24): return the per-project CANONICAL handoff subdirectory
    under `memory/handoffs/`. Used by handoff_discoverer (banner line) and the
    A3 watchdog to locate Claude-authored handoffs.

    2026-05-31: now derives from `resolve_memory_root` (project-rooted with
    legacy fallback) so handoffs live in `<project>/memory/handoffs/` — the
    same tree as the rest of project memory, zip-portable with the project.
    """
    return resolve_memory_root(project_root) / _HARNESS_HANDOFFS_SUBDIR


def _read_handoff_state(state_dir: Path) -> dict:
    """Read the per-project handoff fingerprint state. Returns {} on missing/error."""
    from local_state import read_state_json
    return read_state_json(
        state_dir / "state" / _HANDOFF_STATE_FILENAME, default={},
    )


def _write_handoff_state(state_dir: Path, state: dict) -> None:
    """Persist handoff fingerprint state. Best-effort — never raises."""
    from local_state import write_state_json
    write_state_json(state_dir / "state" / _HANDOFF_STATE_FILENAME, state)


def _count_observations(state_dir: Path) -> int:
    """Count lines in observations.jsonl as a proxy for observation index.

    Used as the fingerprint component for dedup: if count grew since the
    prior handoff for this session, new work happened → write a fresh
    handoff. If count is unchanged, no new work → skip.
    """
    from local_state import count_observations
    return count_observations(state_dir)


def write_session_handoff_if_needed(
    state_dir: Path,
    project_root: Path,
    session_id: str | None,
    *,
    force: bool = False,
) -> Path | None:
    """Write a session-end handoff to the per-project harness memory tree
    IF the fingerprint indicates new work has accumulated since prior
    handoff for this session.

    Fingerprint: (session_id, n_observations_at_prior_handoff). When the
    incoming n_observations > prior_n_observations OR session_id differs,
    new work happened → write. Otherwise skip (return None).

    Args:
        state_dir: per-project Allostat state directory.
        project_root: project root for resolving the harness memory dir.
        session_id: distinct session identifier. None → no-op (matches
            PATCH-172 side-session suppression — keeps "(no debrief)"
            handoffs from accumulating).
        force: bypass fingerprint check. Used by the operator-signal-phrase
            path where intent is explicit ("end session please" → operator
            wants a fresh handoff regardless of dedup).

    Returns the written Path on success, None on skip/error.
    """
    if session_id is None:
        return None

    current_obs_count = _count_observations(state_dir)

    if not force:
        state = _read_handoff_state(state_dir)
        prior_session = state.get("session_id")
        prior_obs_count = state.get("n_observations_at_write", 0)
        if (
            prior_session == session_id
            and isinstance(prior_obs_count, (int, float))
            and current_obs_count <= prior_obs_count
        ):
            return None

    # PATCH-181: route to per-project harness memory tree by default.
    # Operator override via ALLOSTAT_HANDOFF_DIR still honored — power
    # users / non-standard layouts opt in there.
    #
    # M1 audit fix (2026-05-25, S1): calls `resolve_audit_dir` directly
    # (the canonical name). The deprecated `resolve_harness_handoff_dir`
    # back-compat shim was retired pre-release (zero remaining consumers).
    env_override = os.environ.get("ALLOSTAT_HANDOFF_DIR")
    if env_override:
        handoff_dir = Path(env_override)
    else:
        handoff_dir = resolve_audit_dir(project_root)

    try:
        handoff_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    ts = utc_iso().replace(":", "").replace("-", "")[:15]
    handoff_path = handoff_dir / f"{ts}_session_end.md"

    # Build body via session_debrief if available.
    body = "(no debrief content)"
    try:
        # session_debrief lives under lib/; add to sys.path if needed.
        lib_dir = Path(__file__).resolve().parent
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
        from session_debrief import build_session_end_debrief  # noqa: E402
        body = build_session_end_debrief(state_dir) or "(no debrief content)"
    except Exception as e:
        body = f"(debrief build failed: {e})"

    handoff_content = (
        f"# Session-end audit (auto-generated)\n\n"
        f"_Behavioral observability artifact — wrapper-rendered aggregate signal._\n"
        f"_NOT a continuity handoff. Claude-authored rolling handoffs live in the_\n"
        f"_project's own memory tree, at `<project_root>/memory/handoffs/<session_id>.md`_\n"
        f"_per the A3 protocol — never under `~/.claude/`._\n\n"
        f"Generated: {utc_iso()}\n"
        f"Session id: {session_id}\n"
        f"Observations at write: {current_obs_count}\n"
        f"Trigger: {'operator signal phrase (forced)' if force else 'Stop hook (fingerprint dedup)'}\n\n"
        f"{body}\n"
    )

    try:
        handoff_path.write_text(handoff_content, encoding="utf-8")
    except OSError:
        return None

    _write_handoff_state(state_dir, {
        "session_id": session_id,
        "written_at": utc_iso(),
        "n_observations_at_write": current_obs_count,
        "path": str(handoff_path),
        "forced": force,
    })

    return handoff_path
