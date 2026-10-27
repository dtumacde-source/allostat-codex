---
name: allostat-handoff-status
description: Show A3 handoff watchdog state for this session — writes count, reminders fired, threshold state, current escalation level. Use to verify the continuity layer is healthy.
---

# /allostat-handoff-status

Visibility into the A3 watchdog (R6 advisor refinement). Surfaces:

- **session_handoff_count** — total substantive handoffs Claude wrote this session
- **session_reminder_count** — times the watchdog queued a reminder
- **session_response_count** — reminders that Claude responded to with a write
- **anti_pattern_count** — junk handoffs caught (file <500 bytes or all sections empty)
- **current_escalation_level** — 0 (silent), 1 (soft), 2 (strong), 3 (stub injection)
- **turns_since_last_write** — turn counter
- **tokens_estimate_since_last_write** — token-delta counter
- **thresholds** — current turn / token thresholds (+ disabled flag)
- **last_handoff_path** — path of the last substantive write
- **reminder_pending** — true if a reminder is queued for the next prompt

## Source

Reads `~/.claude/projects/<cwd>/.allostat/state/handoff_watchdog.json`
client-side. No server round-trip.

## What healthy looks like

- session_handoff_count grows steadily (1 write per ~3-5 turns of work)
- session_reminder_count low or zero
- current_escalation_level stays at 0 most of the time
- anti_pattern_count zero

## What unhealthy looks like

- reminder_count climbing without response_count climbing
- escalation_level stuck at 2 or 3
- anti_pattern_count > 0 (Claude is gaming the watchdog with empty stubs)

If unhealthy: surface this output and ask Claude what's going on. The
watchdog reminders should self-correct; if they don't, something is
wrong with the protocol injection or the file-write path.

## Tuning

- `ALLOSTAT_HANDOFF_WATCHDOG=0` — disable entirely (not recommended)
- `ALLOSTAT_HANDOFF_WATCHDOG_TURNS=N` — turn threshold (default 5)
- `ALLOSTAT_HANDOFF_WATCHDOG_TOKENS=N` — token threshold (default 50000)

Client-side only.
