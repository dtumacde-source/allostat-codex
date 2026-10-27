---
project: allostat
description: How Allostat organizes memory — per-project tree structure, sandbox convention, MEMORY.md index, auto-populate triggers. Loaded ALWAYS-ON for Allostat-managed cwds (post-A1 revert 2026-05-24); topic-conditional list kept for non-Allostat cwds via appendix system.
topics: [memory, memory-tree, mcs, continuity, sandbox]
always_on: true
confidence_threshold: 0.7
eager_fallback: false
---

## Memory architecture — auto-populate on session-init and "reread"

The memory architecture lives at `~/.claude/allostat/projects/<project>.md` (one file per project, H2 sections per topic). At session initiation, AND on operator "reread" trigger:

- **File present and no "reread"** → just read `~/.claude/allostat/projects/<project>.md`. Cheap path.
- **File missing** → consolidate from the existing memory tree at the path listed for that project. Write the consolidated file at `~/.claude/allostat/projects/<project>.md` with H2 sections per topic, then read it.
- **"Reread" said** → re-consolidate from the existing memory tree and overwrite the new-path file.

**Consolidation rules:**

- One file per project at `~/.claude/allostat/projects/<project>.md`.
- H2 sections per topic, derived from existing `feedback_*` and `project_*` filenames.
- Locked decisions use the supersession chain pattern (strikethrough → arrow → new value, history preserved).
- Source files in the harness memory tree get archived with `_LEGACY_pre_YYYYMMDD_rollout` suffix per the global rollout rule.

## Sandbox/advisor convention (asymmetric isolation)

Each canonical project file at `~/.claude/allostat/projects/<project>.md` is paired with a confidential sandbox **folder** at `~/.claude/allostat/projects/<project>_sandbox/`. The pairing keeps brainstorming, advisement, and dev exploration separate from canon.

**Folder structure:**

```
~/.claude/allostat/projects/<project>_sandbox/
├── README.md                         ← perms + framing for the whole folder
├── YYYYMMDD_session01_advisor.md     ← one file per advisor session, dated
└── ...
```

**Two-axis isolation (read access asymmetric, write access always operator-permissioned):**

| Operation | Canon (`<project>.md`) | Sandbox folder (`<project>_sandbox/**`) |
|---|---|---|
| Advisor session reads | ✅ allowed | ✅ allowed |
| Non-advisor session reads | ✅ allowed | ❌ DENIED by default |
| Advisor session writes | ⚠ requires per-action operator permission | ⚠ requires per-action operator permission |
| Non-advisor session writes | ⚠ requires per-action operator permission | ❌ DENIED |

## Preferred tree structure for project memory

Project memory is **project-rooted**: it lives in `<project>/memory/` — the project's OWN folder — so the whole project is self-contained and zip-portable. Move the folder to another machine, open Claude with Allostat, and the memory comes with it. Read AND write it there, NOT on the Claude Code C-drive harness path (`~/.claude/projects/<cwd>/memory/`). The Allostat SessionStart hook injects `<project>/memory/MEMORY.md`, and `resolve_memory_root` derives every memory/handoff path from here. (The canon-consolidation file above relocates into the project dir in a later phase; the per-project tree is project-rooted now.)

Organize the tree, not a flat index:

```
MEMORY.md (auto-loaded, kept under 100 lines)
│
├── User identity (1–2 lines)
├── Cross-cutting rules (3–5 lines, the universal ones)
│
├── Projects (1 line per project, top-level only)
│   ├── project_X.md  ──► its own "Related rules" section listing subordinate feedback files
│   └── ...
│
└── Brainstorms log (1 line, points to a single rolling brainstorms.md)
```

**Rules:**

1. `MEMORY.md` is the auto-loaded index. Keep entries under 150 chars each (auto-enforced).
2. Each `project_*.md` owns a `## Related rules` section listing feedback files specific to that project.
3. Cross-cutting rules stay at MEMORY.md top level.
4. Brainstorm sessions consolidate into a single rolling `brainstorms.md`.

### Realized injection model (2026-06-26 — two-layer, grouped)

`MEMORY.md` is grouped under three prefix sections — `## Feedback` (behavioral, always-on), `## Project`, `## Reference` — plus a fallback `## Other`. The SessionStart injection (`memory_reader.build_memory_index_context` → `_render_core_index`) and the regenerate tool (`memory_lifecycle.rebuild_memory_md_index`) BOTH honor this shape:

- **Injection (core view):** the behavioral layer injects in full, with each `feedback_*` entry individually addressable but its hook tail tightened to ≤8 words. `## Project` / `## Reference` / `## Other` inject their entries the same way, capped at the 20 newest per section (header then says `newest N of M`); empty sections and prose placeholders are dropped. An explicit on-disk pointer to the full index is always appended, and a hard char-cap backstop truncates-with-pointer so the injected core never grows unbounded.
  - Those three sections collapsed to a bare `## <Name> (N)` count from 2026-06-26 until 2026-08-07. Measured across 199 questions on a tree Allostat wrote itself, that cost ~90% of the retrieval value already paid for: 7 answers of 199 in normal use against 155 when a read was ordered, with the agent asserting no such memory existed. A count is not a cue — an agent reads on demand only when something suggests a demand. The same leaf re-filed under `## Feedback` was answered correctly and immediately.
- **Regeneration:** `rebuild_memory_md_index` emits the same grouped sections and PRESERVES any hand-curated hook already in `MEMORY.md` (only entries with no curated hook fall back to the frontmatter `description:`), so a rebuild never silently downgrades operator-written hooks or re-flattens the index.

This is lossless on disk: collapsing/tightening changes only what is INJECTED each session; the full entry list and full hooks stay readable in `MEMORY.md` and the leaf frontmatter.

### Auto-sort + auto-merge protocol on new memory file

Whenever a new memory file is created, the same turn must:

1. **Sort:** classify the new file. Project-specific? Cross-cutting? Globally applicable?
2. **Merge check:** scan existing files for >50% conceptual overlap; propose merging rather than creating duplicate.
3. **Update parent:** if project-specific, update relevant `project_*.md`'s "Related rules" section.

### Manual triggers

- **"sort memory"** → audit all memory files, ensure correct tree position.
- **"merge memory"** → scan for overlap, propose merge candidates.
- **"memory audit"** → combined sort + merge + staleness check.

## Per-project override files

In addition to canon (`<project>.md`) and sandbox folder (`<project>_sandbox/`), each project may have:

- `<project>_prose.md` — project-specific voice override if different from global default
- `<project>_preferences.md` — project-specific working-preference overrides

Precedence: project overrides global where explicitly defined; global default applies otherwise.
