---
name: allostat-forget
description: Retract something from memory — purges every store Allostat can reach and reports, per store, what it removed and what it could not. Preview by default.
---

# /allostat-forget

Remove a fact from Allostat's memory and say honestly where it went.

## Why this exists

"Forget X" used to delete the memory file, report success, and leave the same
text sitting verbatim in the observation log and the audit files. That is not a
deletion. `Custody and Constraint` (2026-07-31) requires the record be held in
storage you "can read in full, can amend, and can destroy" — so a command that
claims destruction has to deliver it, or say precisely where it fell short.

## Usage

```
/allostat-forget <text>              # preview: shows what WOULD be removed, changes nothing
/allostat-forget <text> --execute    # actually remove it
/allostat-forget <text> --regex      # treat <text> as a regular expression
```

Preview is the default deliberately. A deletion is not reversible and you are
entitled to see its blast radius first.

## Required execution path

This command DRIVES the bundled engine. Parse the operator's arguments, preserve
the pattern as one argument, and run exactly this entry point; do not hand-grep,
hand-edit, or summarize what should have happened:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run forget <pattern> [--regex] [--execute]
```

The engine requests the server purge decision for every present local silo,
applies the returned client-state instructions, then independently scans and
purges all registered local stores. If the server purge bridge is unavailable,
the local work still runs but the command exits non-zero and refuses to say the
retraction is complete.

## What it reaches

Every store is listed in the report, whether or not it held anything:

- the memory tree, including cold storage and `_processed/` derivatives
- the `MEMORY.md` index and the harness-side mirror of it
- session handoffs
- the observation log, nudge history, pending surfaces, server-instructed writes
- local silos and canonical resolutions
- audit files
- rotated `.jsonl.gz` archives of any of the above

Each store reports what was removed, rewritten, or deleted — and every store is
re-read afterwards. A store that still matches is reported as **STILL PRESENT**,
not as done.

## What it does NOT reach, and says so every time

- **Claude Code and Codex session transcripts.** They belong to their harnesses,
  not to Allostat, and your 10-year retention rule governs them. Purging either
  transcript store is a deliberate harness decision, never a side effect of
  this command.

## Accountability

Every execution appends a line to `.allostat/retraction_log.jsonl` recording
when it ran, a random non-reversible retraction identifier, and per-store counts
— never the pattern or removed content. A deletion nobody can account for is
its own custody problem.

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
