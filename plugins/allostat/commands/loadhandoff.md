---
name: loadhandoff
description: Load a specific Allostat session handoff into context. Resolves by date (YYYYMMDD) or session_id. Use when you want to pick up where a prior session left off.
---

# /loadhandoff [date|session_id]

Reads a specific Claude-authored rolling handoff from the project-rooted
memory tree (`<project>/memory/handoffs/<session_id>.md`) and surfaces its
content for context.

## Resolution

- **No argument**: list the 10 most-recent handoffs in this project's
  memory tree with timestamps, brief focus summary, and session_id.
  Operator picks one.
- **Date `YYYYMMDD`**: surface the newest handoff modified on that date.
- **Session_id (UUID prefix or full)**: surface the matching file.

## Path

Resolves handoffs the same way `lib/handoff_discoverer.py` does, in order:
1. `$ALLOSTAT_HANDOFF_DIR` (explicit override), if set.
2. **Primary — project-rooted:** `<project>/memory/handoffs/*.md`
   (zip-portable, PATCH-181; this is where handoffs are written).
3. Transition fallback: `~/.claude/projects/<sanitized-cwd>/memory/handoffs/*.md`
   (the legacy harness tree, for installs still mid-migration).
4. Parent-walk + home/Downloads defaults.

The wrapper's auto-rendered `.allostat/audit/` files are NOT surfaced
by this command — those are behavioral observability, not continuity.

The `_LEGACY_pre_fixnow/` subdirectory is searched as a fallback when
no current handoffs exist (transition window after Fix Now ship).

## What gets surfaced

Just the file content. The operator can then ask follow-up questions
referencing the handoff sections (Focus / Decisions / Memory pointers /
Open threads / Blocked / Queued).

## Detail sibling

If a `<session_id>.detail.md` file exists next to the surfaced handoff (same
`handoffs/` folder), the handoff offloaded its verbose overflow there per the
handoff-redesign protocol. Mention that the detail file is available and
**Read it on demand** when the operator wants the full logs / command
sequences / rationale — don't auto-load it (keeping it out of context is the
whole point of the split).

Client-side only. No server round-trip.
