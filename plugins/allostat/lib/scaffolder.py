"""Allostat install scaffolder — PATCH-A / Tier 1.

First-run scaffolder that ensures Allostat's required structure exists on
customer install. Scaffolder handles:
- Bucket B empty templates (top-level `~/.claude/*.md`) — write if absent
- Tier 5 archipelago.json scaffold — write empty {} if absent
- Tier 4 per-project memory tree — first-cwd-seen detector + MEMORY.md
  + _PURPOSE.md templates
- Tier 3 sandbox folder scaffolding

**Bucket A static content** (memory_architecture, allostat_architecture,
artifact_taxonomy, canon_size_discipline) was injected always-on at
SessionStart pre-Bucket-A-dedup (2026-05-24). Now ships via the appendix
seeder (`appendix_system.py`) with topic frontmatter — loads ONLY when
prompt topics match. Closes the ~17KB-per-session bloat advisor flagged.

Honors load-bearing rules:

1. `feedback_ship_structure_not_content.md` — Bucket B templates ship as
   EMPTY section headers + brief descriptions. Customer fills own.

2. `feedback_migration_never_destroys_existing_files.md` — detects file
   existence BEFORE writing. Never overwrites customer edits.

3. `feedback_no_operator_paths_in_shipped_code.md` — no operator-specific
   content in any template or scaffolded file.

The scaffolder is idempotent via `~/.allostat/.first_run_complete` marker.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from refined_memory_policy import REFINED_TOPICAL_MEMORY_CONTRACT


# Bundle subdirectories (relative to plugin root).
# _STATIC_SUBDIR removed 2026-05-24 — Bucket A files moved to data/appendix_seed/
# with topic frontmatter; load via appendix_system instead of always-on inject.
_TEMPLATES_SUBDIR = ("data", "templates")

# First-run marker location (operator-side state dir).
_FIRST_RUN_MARKER_DIR_PARTS = (".allostat",)
_FIRST_RUN_MARKER_NAME = ".first_run_complete"

# Top-level customer-side directory for Bucket B templates.
_CUSTOMER_DOTCLAUDE_DIR = ".claude"

# Tier 5 — Allostat state scaffold paths (relative to home).
_ARCHIPELAGO_PATH_PARTS = (".claude", ".allostat", "archipelago.json")
_ARCHIPELAGO_DEFAULT_CONTENT = "{}\n"

# Tier 4 — per-project memory tree templates.
_MEMORY_MD_TEMPLATE = """# Memory index — {project_name}

This is the project memory tree for sessions opened from this cwd. Allostat auto-loads MEMORY.md on every session start.

## How to use this file

Each section below holds feedback / project / reference files Claude has learned about your workflow. Lines stay under 150 chars (auto-enforced by Allostat).

Claude: THIS tree — not Claude Code's built-in `~/.claude` memory — is where this project's memory lives. To remember something for future
sessions, write a leaf file in this folder and add a one-line entry for it in the matching section below, in the SAME turn. Allostat injects
this index at every session start, so anything indexed here is found again next session.

Naming is not cosmetic: the `feedback_` / `project_` / `reference_` prefix is what routes a leaf into its section, and the one-line
description is the only cue a future session gets for deciding whether to open it. Keep the prefix you started with for a given subject —
renaming `caroline-profile.md` to `project_caroline.md` mid-project leaves two files where you meant one. If an unindexed leaf does turn up,
Allostat folds it into this index at the next session start and flags any prefixed/unprefixed pair for `/allostat-tend`; that is a safety
net, not the plan.

""" + REFINED_TOPICAL_MEMORY_CONTRACT + """

## Feedback

(Feedback files document corrections you've given Claude. Add entries as Claude learns your preferences.)

## Project

(Project files document state of ongoing work. Add entries as new initiatives start.)

## Reference

(Reference files document external systems, URLs, credentials locations. Add entries as you discover external dependencies.)
"""

_PURPOSE_MD_TEMPLATE = """---
name: purpose-orientation
priority: read at session start, before other memory files
---

# Purpose

This file holds the orienting framework for sessions in this project. Allostat reads it before THE LAW and before STATE.md so the model re-orients to your current purpose on every session start.

## How to use this file

When you give Claude a purpose for this project, write it here. Claude will read it on every session start and work toward it until you declare it fulfilled.

## Current purpose

TODO: describe the current purpose for this project.

## Failure modes from prior sessions

TODO: as sessions surface failure modes you don't want repeated, document them here so future sessions can avoid them.
"""

_SANDBOX_README_TEMPLATE = """# {project_name} sandbox — permission model

This folder is the confidential sandbox for {project_name} — a place for advisor/dev/brainstorm work that isn't yet operating policy. Distinct from canon at `~/.claude/allostat/projects/{project_slug}.md`.

## Access permissions

| Operation | Canon (`{project_slug}.md`) | This sandbox folder |
|---|---|---|
| Advisor-role session reads | allowed | allowed |
| Non-advisor session reads | allowed | DENIED by default |
| Advisor-role session writes | per-action operator permission | per-action operator permission |
| Non-advisor session writes | per-action operator permission | DENIED |

## Per-session naming

Per-session work lives in `YYYYMMDD_session##_advisor.md` files alongside this README. Date-first so files sort chronologically; session number zero-padded for multi-session days.
"""


def is_adoptable_project_root(
    project_root: Path,
    *,
    home: Path | None = None,
    tempdirs: list[str] | None = None,
) -> bool:
    """Conservative adoption gate (2026-08-07, reliable-memory run).

    Registration is QUIET and AUTOMATIC — no interview, no questions — so
    the only protection against planting memory trees where they rot is
    this predicate. It refuses exactly the known-toxic root classes:

    - $HOME itself (a session opened from `~` is not a project),
    - filesystem/drive roots (`C:\\`, `/`),
    - temp trees (OS tempdir + TEMP/TMP/TMPDIR) — scratch dirs vanish,
    - anything under `~/.claude` — the harness namespace; scaffolding there
      recreates the 2026-08-03 orphan-tree class (a parallel memory tree
      the banner later reads as fact).

    Everything else is adoptable, including $HOME subdirectories and
    plain non-git folders: the product promise is that a real project dir
    just works with zero setup. Guards only what provably rots.

    This gates SCAFFOLDING (tree creation), never reading: an existing
    populated tree is always served wherever it lives. `.allostat/` state
    is also not gated — entitlement caching must work even in refused
    roots so regulation still functions there.

    `home`/`tempdirs` are injectable for tests. The env override
    `ALLOSTAT_ADOPTION_TEMPDIR_OVERRIDE` replaces the computed tempdir set
    so subprocess-level tests can exercise both verdicts from inside a
    pytest tmp_path (which IS a real tempdir); it can only ever widen
    adoption of temp scratch — never entitlement, never memory reads.
    """
    import tempfile
    try:
        root = Path(project_root).resolve()
    except (OSError, ValueError):
        return False
    home_dir = Path(home).resolve() if home is not None else Path.home().resolve()
    if root == home_dir:
        return False
    if root.parent == root:
        return False
    claude_home = home_dir / ".claude"
    if root == claude_home or claude_home in root.parents:
        return False
    if tempdirs is None:
        override = os.environ.get("ALLOSTAT_ADOPTION_TEMPDIR_OVERRIDE")
        if override:
            candidates = {override}
        else:
            candidates = {
                tempfile.gettempdir(),
                os.environ.get("TEMP"),
                os.environ.get("TMP"),
                os.environ.get("TMPDIR"),
            }
        tempdirs = [c for c in candidates if c]
    for td in tempdirs:
        try:
            t = Path(td).resolve()
        except (OSError, ValueError):
            continue
        if root == t or t in root.parents:
            return False
    return True


def compute_memory_dir_path(project_root: Path | None = None) -> Path:
    """Return the expected memory tree path for a project, regardless of
    existence. Distinct from memory_lifecycle.resolve_memory_dir (which
    returns None when dir absent — PATCH-D needs the path to scaffold).

    UNIFIED 2026-06-01: delegates to `session_handoff.resolve_memory_root`,
    the single chokepoint, so the WRITER (scaffolder) and the READER
    (resolve_memory_root) agree on where memory lives. Previously this was
    hardcoded to the harness tree (`~/.claude/projects/<sanitized>/memory`)
    while the reader preferred in-project — so the scaffolder seeded C, the
    reader fell back to C, and the in-project folder was never auto-created
    for any project except the one hand-migrated by the operator. Unifying
    here makes new projects scaffold their memory in `<project>/memory/`.
    """
    import os
    if project_root is None:
        project_root = Path(os.getcwd())
    from session_handoff import resolve_memory_root  # noqa: E402
    return resolve_memory_root(Path(project_root).resolve())


def _project_slug_from_root(project_root: Path) -> str:
    """Derive a project slug from the cwd's basename (lowercase, ascii-safe)."""
    name = project_root.name.strip()
    if not name:
        name = "project"
    # ASCII-safe lowercase, dashes for spaces
    safe = "".join(c.lower() if c.isalnum() or c in "_-" else "_" for c in name)
    safe = safe.replace(" ", "_")
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "project"


def scaffold_project_memory_tree(
    project_root: Path,
    home: Path | None = None,
) -> list[ScaffoldAction]:
    """PATCH-D — first-cwd-seen detector.

    Scaffolds ~/.claude/projects/<sanitized-cwd>/memory/ with:
    - MEMORY.md header template
    - _PURPOSE.md empty template

    NEVER overwrites existing files (preserves customer memory per
    feedback_migration_never_destroys_existing_files.md).

    Returns list of actions.
    """
    actions: list[ScaffoldAction] = []
    # Compute path using home override if provided (for testing).
    if home is not None:
        from session_handoff import sanitize_cwd_for_harness  # noqa: E402
        sanitized = sanitize_cwd_for_harness(Path(project_root).resolve())
        memory_dir = home / ".claude" / "projects" / sanitized / "memory"
    else:
        memory_dir = compute_memory_dir_path(project_root)

    project_name = project_root.name.strip() or "project"

    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        actions.append(ScaffoldAction(
            kind="tier4_write_failed",
            target=str(memory_dir),
            detail=f"mkdir failed: {e}",
        ))
        return actions

    # MEMORY.md
    memory_md = memory_dir / "MEMORY.md"
    if memory_md.exists():
        actions.append(ScaffoldAction(
            kind="tier4_skipped_existing",
            target=str(memory_md),
            detail="MEMORY.md preserved",
        ))
    else:
        try:
            memory_md.write_text(
                _MEMORY_MD_TEMPLATE.format(project_name=project_name),
                encoding="utf-8",
            )
            actions.append(ScaffoldAction(
                kind="tier4_created",
                target=str(memory_md),
                detail="MEMORY.md template scaffolded",
            ))
        except OSError as e:
            actions.append(ScaffoldAction(
                kind="tier4_write_failed",
                target=str(memory_md),
                detail=str(e),
            ))

    # _PURPOSE.md
    purpose_md = memory_dir / "_PURPOSE.md"
    if purpose_md.exists():
        actions.append(ScaffoldAction(
            kind="tier4_skipped_existing",
            target=str(purpose_md),
            detail="_PURPOSE.md preserved",
        ))
    else:
        try:
            purpose_md.write_text(_PURPOSE_MD_TEMPLATE, encoding="utf-8")
            actions.append(ScaffoldAction(
                kind="tier4_created",
                target=str(purpose_md),
                detail="_PURPOSE.md template scaffolded",
            ))
        except OSError as e:
            actions.append(ScaffoldAction(
                kind="tier4_write_failed",
                target=str(purpose_md),
                detail=str(e),
            ))

    return actions


def scaffold_project_sandbox(
    project_root: Path,
    home: Path | None = None,
) -> list[ScaffoldAction]:
    """PATCH-D — per-project sandbox folder scaffolder.

    Scaffolds ~/.claude/allostat/projects/<project_slug>_sandbox/ with
    README explaining permission model. NEVER overwrites existing files.

    Returns list of actions.
    """
    actions: list[ScaffoldAction] = []
    h = home or Path.home()
    project_slug = _project_slug_from_root(project_root)
    sandbox_dir = h / ".claude" / "allostat" / "projects" / f"{project_slug}_sandbox"
    try:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        actions.append(ScaffoldAction(
            kind="tier3_write_failed",
            target=str(sandbox_dir),
            detail=f"mkdir failed: {e}",
        ))
        return actions

    sandbox_readme = sandbox_dir / "README.md"
    if sandbox_readme.exists():
        actions.append(ScaffoldAction(
            kind="tier3_skipped_existing",
            target=str(sandbox_readme),
            detail="sandbox README preserved",
        ))
        return actions

    try:
        sandbox_readme.write_text(
            _SANDBOX_README_TEMPLATE.format(
                project_name=project_root.name or "project",
                project_slug=project_slug,
            ),
            encoding="utf-8",
        )
        actions.append(ScaffoldAction(
            kind="tier3_created",
            target=str(sandbox_readme),
            detail=f"sandbox README scaffolded for {project_slug}",
        ))
    except OSError as e:
        actions.append(ScaffoldAction(
            kind="tier3_write_failed",
            target=str(sandbox_readme),
            detail=str(e),
        ))
    return actions


@dataclass
class ScaffoldAction:
    """One scaffolder action: created / skipped / injected. Logged for forensics."""
    kind: str  # "bucket_b_created" | "bucket_b_skipped_existing" | "bucket_a_injected"
    target: str  # file path or static-content name
    detail: str = ""


@dataclass
class ScaffoldResult:
    """Result of a scaffolder run."""
    first_run: bool = False
    bucket_a_content: str = ""  # always "" post Bucket A dedup (2026-05-24); preserved for API compat
    actions: list[ScaffoldAction] = field(default_factory=list)


def get_plugin_root() -> Path:
    """Resolve the plugin's bundle root (where data/static/ + data/templates/ live)."""
    return Path(__file__).resolve().parent.parent


def get_first_run_marker_path(home: Path | None = None) -> Path:
    """Return absolute path to the first-run marker file."""
    h = home or Path.home()
    marker_dir = h
    for part in _FIRST_RUN_MARKER_DIR_PARTS:
        marker_dir = marker_dir / part
    return marker_dir / _FIRST_RUN_MARKER_NAME


def is_first_run(home: Path | None = None) -> bool:
    """True if the first-run marker is absent (customer install or never-run)."""
    return not get_first_run_marker_path(home=home).is_file()


def write_first_run_marker(home: Path | None = None) -> None:
    """Write the first-run marker. Idempotent — overwrites with current
    timestamp on each call. Caller is responsible for invoking only after
    scaffold operations complete successfully."""
    marker_path = get_first_run_marker_path(home=home)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "marker": "first_run_complete",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "scaffolder_version": "PATCH-A v1.4.16",
    }
    marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_bucket_a_content(plugin_root: Path | None = None) -> str:
    """DEPRECATED 2026-05-24: returns "" always.

    Pre-deprecation this function loaded ~17KB of Bucket A static content
    for always-on additionalContext injection at SessionStart. That bloat
    was operator-flagged. Bucket A content moved to data/appendix_seed/
    with topic frontmatter and now loads topic-conditional via the
    appendix system (`appendix_system.py`) — fires only when prompt
    topics match.

    Kept as no-op for API compatibility with existing call sites.
    """
    return ""


def write_bucket_b_templates(
    home: Path | None = None,
    plugin_root: Path | None = None,
) -> list[ScaffoldAction]:
    """Write Bucket B templates to ~/.claude/ ONLY if file absent.

    NEVER overwrites existing files. Operator/customer edits are sacrosanct
    per `feedback_migration_never_destroys_existing_files.md`.

    Returns list of ScaffoldAction (created or skipped per file).
    """
    h = home or Path.home()
    root = plugin_root or get_plugin_root()
    templates_dir = root
    for part in _TEMPLATES_SUBDIR:
        templates_dir = templates_dir / part
    if not templates_dir.is_dir():
        return []

    customer_claude_dir = h / _CUSTOMER_DOTCLAUDE_DIR
    customer_claude_dir.mkdir(parents=True, exist_ok=True)

    actions: list[ScaffoldAction] = []
    for template_path in sorted(templates_dir.glob("*.md")):
        target_path = customer_claude_dir / template_path.name
        if target_path.exists():
            actions.append(ScaffoldAction(
                kind="bucket_b_skipped_existing",
                target=str(target_path),
                detail="customer edit preserved",
            ))
            continue
        try:
            content = template_path.read_text(encoding="utf-8")
            target_path.write_text(content, encoding="utf-8")
            actions.append(ScaffoldAction(
                kind="bucket_b_created",
                target=str(target_path),
                detail=f"template ({len(content)} bytes)",
            ))
        except OSError as e:
            actions.append(ScaffoldAction(
                kind="bucket_b_skipped_existing",
                target=str(target_path),
                detail=f"write failed: {e}",
            ))
    return actions


def scaffold_archipelago(home: Path | None = None) -> ScaffoldAction | None:
    """PATCH-C+E — Tier 5 state scaffold.

    Writes empty `{}` to ~/.claude/.allostat/archipelago.json if absent.
    Without this, /allostat-status --all shows "No archipelago data
    found" until something writes the file. Per
    feedback_migration_never_destroys_existing_files.md: detect existence
    BEFORE writing. Returns None if file already present (preserved).
    """
    h = home or Path.home()
    target = h
    for part in _ARCHIPELAGO_PATH_PARTS:
        target = target / part
    if target.exists():
        return ScaffoldAction(
            kind="tier5_skipped_existing",
            target=str(target),
            detail="archipelago.json present",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_ARCHIPELAGO_DEFAULT_CONTENT, encoding="utf-8")
        return ScaffoldAction(
            kind="tier5_created",
            target=str(target),
            detail="archipelago.json scaffolded as empty {}",
        )
    except OSError as e:
        return ScaffoldAction(
            kind="tier5_write_failed",
            target=str(target),
            detail=str(e),
        )


def run_scaffolder(
    home: Path | None = None,
    plugin_root: Path | None = None,
    force_first_run: bool = False,
) -> ScaffoldResult:
    """Run the full scaffolder pass.

    Behavior:
    1. Detect first-run state (marker absent or force_first_run=True)
    2. Load Bucket A content (always — for additionalContext injection)
    3. On first run: write Bucket B templates (if absent), write marker
    4. On subsequent runs: skip Bucket B writes (gated on first-run state)
    5. Return ScaffoldResult with content + actions

    Caller is expected to:
    - Use result.bucket_a_content for additionalContext injection
    - Log result.actions for forensics (via observation events)
    - Surface result.first_run state if banner needs to note first-install
    """
    result = ScaffoldResult()
    result.first_run = force_first_run or is_first_run(home=home)
    result.bucket_a_content = load_bucket_a_content(plugin_root=plugin_root)

    if result.first_run:
        result.actions = write_bucket_b_templates(
            home=home, plugin_root=plugin_root,
        )
        # PATCH-C+E — Tier 5 archipelago scaffold (only on first run;
        # subsequent runs preserve any existing archipelago state).
        archipelago_action = scaffold_archipelago(home=home)
        if archipelago_action is not None:
            result.actions.append(archipelago_action)
        try:
            write_first_run_marker(home=home)
        except OSError as e:
            result.actions.append(ScaffoldAction(
                kind="marker_write_failed",
                target=str(get_first_run_marker_path(home=home)),
                detail=str(e),
            ))

    return result
