---
name: allostat-prune
description: On-demand memory tree pruning to cold storage. Archive-not-destroy. Three subverbs preview/execute/restore. Two trigger paths age-based + operator retirement-language with disambiguation protocol.
---

# /allostat-prune

S2 Block 4 (2026-05-27): on-demand pruning of stale memory tree files to
cold storage (`_archived_<YYYYMMDD>/` subdirectory). Per operator's
design directive:

> "No deletion. We are simply telling Claude to ignore unless told to
> recall. If a user tells Claude that something no longer applies, it
> should be pruned then saved in cold storage."

The pruning engine lives at `lib/pruning.py`. This command DRIVES it — each
subverb below runs the library directly; do not hand-grep or hand-move files.

## Subverbs

### `/allostat-prune preview` — list candidates, no move

Run:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune preview
```

Scans the memory tree for files older than the age threshold (default
90 days) in eligible classes and prints each candidate with H1 +
description + age. **No files are moved.** Use this to audit what would
happen before invoking `execute`.

Options (append to the command):
- `--age-days N` (default 90): override the age threshold
- `--max-files N` (default 20): cap per-pass candidates

Pass an explicit memory-tree path as the first positional arg if you are
not inside the target project (e.g. `... preview /path/to/memory`);
otherwise the tree is resolved from the active project's cwd.

### `/allostat-prune execute` — archive candidates

Run:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune execute
```

Moves age-trigger candidates to
`<project>/memory/_archived_<YYYYMMDD>/` preserving subdirectory
structure, and logs each move to `<project>/memory/_pruning_log.md`
(append-only). Same `--age-days` / `--max-files` options as `preview`.
Safe because archive-not-destroy + per-pass cap + the `restore` path
below exists. Always run `preview` first and show the operator the
candidate list.

### `/allostat-prune restore <YYYYMMDD>` — reverse a pass

Run:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune restore <YYYYMMDD>
```

Restores every file in `_archived_<YYYYMMDD>/` back to its original
location. Skips any file whose original location already exists (it will
not overwrite current operator content — those show as `skipped
(original_exists)`).

## Retirement-language path

When the operator's text contains retirement-language patterns ("the X
rule no longer applies", "retire that feedback", "this is stale", "not
applicable anymore"), do NOT jump to `execute`. Follow the regulated
disambiguation protocol per advisor §4 (2026-05-27) — this path is
agent-orchestrated because it requires per-item operator confirmation
before any move:

1. **Grep memory tree** for keywords extracted from the operator's text
   (the engine helpers `detect_retirement_language`,
   `extract_keywords_for_disambiguation`, and
   `surface_retirement_candidates` in `lib/pruning.py` define this
   matching; surface candidates by reading the tree).
2. **Surface candidates** with H1 title + description for each.
3. **Wait for operator per-item confirmation** — never autonomous matching.
4. **Only after confirmation** archive the confirmed file(s) via
   `py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune execute` (or move the
   single confirmed file), then point the operator at `restore` for undo.

If no candidate clearly matches, surface the empty result + ask the
operator to clarify rather than guess.

## Eligible vs never-touched

**Eligible for archive:**
- `feedback_*.md` (operator feedback files older than the threshold)
- `project_*.md` (project memory files older than the threshold)
- `reference_*.md` (reference files older than the threshold)
- Files in `action_plans/`, `ship_reports/`, `debugging_logs/`,
  `audit_reports/`, `behavioral_audits/`, `code_audits/`, `benchmarks/`,
  `tech_specs/`

**Never touched (no-archive zones):**
- `MEMORY.md` — the index itself
- `_PURPOSE.md` — orientation
- All `user_*.md` files — operator identity + durable preferences
- Existing `_LEGACY*/` directories (already archived — the lifecycle_ladder
  rollback store, anywhere in the tree)
- Existing `_archived_*/` directories (already in cold storage)
- Existing `_PURGE*/` and `_RETIRED*/` directories (cold storage / retirement)
- `_pruning_log.md` (the log itself)
- Silos files (deferred to v3.1+ — preserve untouched)

Canon docs and advisor-sandbox content live **outside** this memory tree
(under `~/.claude/allostat/projects/…`), so the pruner — which only walks
`<project>/memory/` — never encounters them. `_is_eligible_path` also returns
`False` for any path outside the scanned memory root, so they cannot be
archived even if referenced.

(The never-touched classes listed above are enforced in `lib/pruning.py`'s
`_is_eligible_path` via `NEVER_TOUCHED_FILENAMES` / `NEVER_TOUCHED_FILENAME_PREFIXES`
/ `NEVER_TOUCHED_DIR_PREFIXES`; the CLI cannot archive a never-touched file
even if asked.)

## Recovery

Three paths if Claude needs to recall an archived file:

1. **Implicit via `/recall <topic>`** — scans archives + pruning log
2. **Implicit via "forgetting" trigger language** in operator's prompt
   ("you're forgetting X" / "you don't remember X" / etc.)
3. **Explicit via `/allostat-prune restore <YYYYMMDD>`** — full restore
   of one archive pass (runs `python lib/pruning.py restore`)

Claude does NOT auto-load `_archived_*/` directories at SessionStart.
Context budget stays clean; archives load on demand.

## Examples

- `/allostat-prune preview` → `py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune preview`
- `/allostat-prune preview --age-days 30` → tighter threshold
- `/allostat-prune execute` → archive all age-triggered candidates
- `/allostat-prune restore 20260527` → `py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run prune restore 20260527`

Client-side only. Pruning library backs the operation at `lib/pruning.py`.

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
