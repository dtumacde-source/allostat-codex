---
name: recall
description: Search across Allostat session handoffs + pruning archives by keyword. Returns a ranked list of session pointers (handoffs) plus matching archived files (cold storage). Use to find prior sessions OR retired content where a topic was discussed.
---

# /recall <keywords>

S2 Block 4 (2026-05-27): scope extended beyond handoff `_INDEX.md` to
also include pruning archives + pruning log per advisor §2 design.

Recall is a search command. EXECUTE the searches below against the
active project's memory tree — do not narrate them. The memory tree
root is `<project>/memory/` (project-rooted; resolved the same way the
pruning library resolves it — see `lib/pruning.py`). Run each search
with the Grep tool over that directory.

## Three sources — run all three

### 1. Handoff index (session pointers)

Grep `handoffs/_INDEX.md` files across the memory tree for the keywords:

```
Grep  pattern=<keyword>  path=<project>/memory  glob=**/handoffs/_INDEX.md  -i
```

Each hit is a session pointer. Surface the ranked list, then follow up
with `/loadhandoff <session_id>` to read a specific match. If
`_INDEX.md` is empty/absent, fall back to `/loadhandoff` with no
argument to list recent handoffs directly (that path uses
`lib/handoff_discoverer.py`).

Also grep the per-session detail siblings — these hold the offloaded verbose
overflow (logs, command sequences, long rationale) that the lean handoffs point
to:

```
Grep  pattern=<keyword>  path=<project>/memory  glob=**/handoffs/*.detail.md  -i
```

A hit in a `*.detail.md` means the topic's deep detail lives there; surface the
path and Read it directly (it's the sibling of `<session_id>.md`).

### 2. Pruning archives (cold storage)

Grep the `_archived_*/` directories for the keywords in filename or
content:

```
Grep  pattern=<keyword>  path=<project>/memory  glob=**/_archived_*/**  -i
```

Returns relative paths the operator can `@`-include or Read directly.
To restore a whole archive pass instead of reading one file, use
`/allostat-prune restore <YYYYMMDD>` (runs
`py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run recall-restore <YYYYMMDD>`).

### 3. Pruning log (what was archived, when)

Grep the append-only pruning log for the keywords:

```
Grep  pattern=<keyword>  path=<project>/memory/_pruning_log.md  -i
```

Each entry carries a timestamp + the archive path, so a hit here tells
you which `_archived_<YYYYMMDD>/` pass to look in (or to restore). The
log is written by `lib/pruning.py` on every `execute`.

This is the unambiguous backstop. The forgetting-load trigger (natural-
language phrases like "you're forgetting X", detected by
`pruning.detect_forgetting_trigger`) routes Claude to the same three
search paths automatically.

## Format of _INDEX.md entries

Each line is appended by Claude when a session's focus crystallizes:
```
<timestamp> | <keywords> | <session_id>.md
```

## Matching

Case-insensitive substring match against the keywords (the `-i` flag
above). Multiple keywords: run the grep per keyword and AND the result
sets (a file must appear for every keyword to count as a full match);
surface partial matches separately so the operator can judge.

Cross-project: the memory tree under `~/.claude/projects/<project>/memory/`
(legacy) or `<project>/memory/` (project-rooted) is per-project. To
search other projects, point the Grep `path` at their memory roots.

## Early-session caveat

`_INDEX.md` files don't populate until Claude writes handoffs that
declare keywords. First N sessions post-Fix-Now-install will return
empty results — that's expected, not a bug. After a few sessions of
Claude writing rolling handoffs, the index seeds itself.

If a query returns empty when you expect matches, try `/loadhandoff`
without arguments to list recent handoffs directly (bypasses the index).

## Examples

- `/recall watchdog` → grep all three sources for "watchdog"
- `/recall auth refactor` → AND-match across the three sources
- `/recall debugging install code` → sessions/archives where install code debugging came up

Client-side only. Handoff discovery is backed by
`lib/handoff_discoverer.py`; archive + log search target the tree
`lib/pruning.py` writes to.

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
