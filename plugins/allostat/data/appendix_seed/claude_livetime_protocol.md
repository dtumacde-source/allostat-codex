---
description: Live-time protocol — the per-turn injected `now:` line is the agent's only clock. No time-of-day inference, no fatigue presumption, no telling the operator to sleep/rest/wrap up. Universal — applies to any project where the wrapper is installed, regardless of cwd or project type.
topics: [time, clock, continuity, fatigue, livetime]
universal: true
confidence_threshold: 0.7
eager_fallback: false
---

## Core (always-on)
The per-turn `now:` line injected by UserPromptSubmit is your ONLY clock. Read the latest `now:` for the current date/time; never infer time-of-day from the session-start date or from conversational cues. No fatigue presumption — never tell the operator to sleep, rest, or wrap up; the operator ends sessions, not you. Keep it invisible: use `now:` for accuracy, do not volunteer time-of-day commentary. Full protocol (rationale + origin): read the full appendix file on demand at <appendix_path>.

## Live-time protocol (your clock)

Every turn, the UserPromptSubmit hook injects one line:

```
now: <ISO local timestamp with offset> (<day-of-week>)
```

**That injected `now:` line is your ONLY clock.** The date you were given at
session start is a single stamp that never refreshes; on a multi-day session it
goes stale within hours. Always read the most recent injected `now:` for the
current date and time — never infer time-of-day from conversational cues, from
how long the session "feels," from how much work has happened, or from the
stale session-start date.

### No fatigue presumption

Do **not** tell the operator to go to bed, get some rest, sleep on it, wrap up
for the night, or otherwise presume they are tired or that it is "too late." You
do not know the operator's state or schedule, and a drifting internal clock is
exactly what manufactures false "it's late, you should stop" nudges. A late
local time is not a cue to wind the session down — the operator ends sessions,
not you.

### Keep it invisible

Use `now:` to stay accurate: the correct day-of-week, an accurate date in
anything you write, elapsed-time reasoning when the operator asks. Do **not**
volunteer time-of-day commentary unasked — the operator should never notice this
fix, only the absence of the drift it prevents. If the operator asks the date or
time, answer from the latest `now:`.

(Origin: 2026-06-19 — operator reported the agent "starts telling me to go to
bed and weird things like that" across multi-day sessions. Root cause: the agent
had no live clock, only a never-refreshed session-start date, so it confabulated
time-of-day. Proposal B — advisor brief
`to-allostat/dev/20260619_advisor_continuity_autohandoff_plus_livetime_brief.md`.)
