---
description: Rolling Claude-authored handoff protocol — ambient writes every 3-5 turns + metabolism pulses + idle, one file per session overwritten in place. The continuity layer. Universal — applies to any project where the wrapper is installed, regardless of cwd or project type.
topics: [handoff, continuity, mcs, rolling-handoff]
universal: true
confidence_threshold: 0.7
eager_fallback: false
---

## Core (always-on)
You author the rolling session handoff. Cadence: every 3–5 substantive turns + metabolism pulses (150k/300k/500k/700k/750k) + 15-min idle + before a major topic shift + when the watchdog fires. Write to the EXACT path the SessionStart hook surfaces (do not infer from cwd); one file per session, overwrite in place. Six fixed sections, each a load-bearing one-liner — Focus, Decisions, Memory pointers, Open threads, Blocked, Queued — none ever omitted. Keep it lean (≤ ~1500 tokens); offload only verbose free-text overflow to `<session_id>.detail.md` with a breadcrumb (never move a whole section). Verify-before-claim: every built/shipped/done/fixed claim carries a checkable ref (SHA / file:symbol / test). Author a lean RESUME CORE (last-session activity + what's open) between `<!-- RESUME-CORE:START -->` / `<!-- RESUME-CORE:END -->` markers at the top of the handoff — the hook injects ONLY that core at session start (the full body is a pointer, read on demand). On an auto-injected resume: open with a brief 1–3 line update, state activity plainly and label outcomes as unverified, then hand control back — do NOT auto-continue last session's work, and do NOT fact-find or verify (incl. running `scripts/allostat_state.py`, SHA/test checks) until the operator directs the work. Full protocol (examples, detail-sibling rules, index, rationale): read the full appendix file on demand at <appendix_path>.

## Claude rolling handoff protocol (MCS)

You author session handoffs. The wrapper doesn't anymore — its auto-render lives at `<project_root>/.allostat/audit/` for behavioral observability, separate from continuity. The memory tree's `handoffs/` folder belongs to you.

### Write location

The wrapper computes the canonical path for you and surfaces it in your
SessionStart additionalContext as:

```
=== Allostat handoff path (authoritative) ===
  `<absolute/path/to/handoffs/<session_id>.md>`
```

**Write to that exact path.** Do NOT infer the path from `<cwd>` — the cwd
sanitization rules have edge cases and your inference can diverge from the
watchdog's view. Do NOT pattern-match the filename from prior session
references the operator mentions in chat (e.g. an operator-mentioned
`20260525T063000_session_end` is a LEGACY artifact name, not your filename
target). One file per session, keyed on YOUR sessionId from the session-start
payload (`session_id` field), overwrite in place.

**Fallback ONLY if the marker is absent:** use `<your_session_id>.md` (NOT a
timestamp, NOT a prior session's id) under `<project_root>/memory/handoffs/` —
the memory tree that travels with the project. Always prefer the marker over the
fallback when both are available.

**Never write a handoff under `~/.claude/`.** That path is the harness's own
namespace, not a project folder, even when it contains something that looks like
one. On 2026-08-04 this fallback named it and handoffs for a live project landed
there; the operator moved them by hand. If you cannot determine the project root,
say so and write nothing rather than writing into the harness tree — a stated
gap is recoverable, a handoff nobody can find is not.

### Cadence (no operator signal required)

Write or refresh the rolling handoff:

- Every 3–5 turns of substantive work
- At metabolism volume pulses: 150k / 300k / 500k / 700k / 750k tokens
- On idle: if 15 min pass with no operator prompt, write before responding to the next one
- Before a major topic shift, even if turn count hasn't hit 3
- When the watchdog reminder fires (you've drifted past threshold — write now)

Do NOT wait for operator to say "end session." That was the old cumbersome pattern. Ambient = you decide.

### Sections (fixed structure)

```markdown
# <session_id>

_Rolling Claude-authored handoff. Last update: <ISO timestamp UTC>._

<!-- RESUME-CORE:START -->
**Worked on:** 2–4 tight one-line bullets — what THIS session actually did.
**Left hanging:** 1–3 items — what's open / gated / queued for next session.
<!-- RESUME-CORE:END -->

## Focus
What this session is about. One paragraph or 2-3 bullets.

## Decisions
Locked calls made this session. Numbered list. Include "why" briefly.

## Memory pointers
Files / paths future-you should read to resume. Bullet list with one-line "why this one."

## Open threads
Work in progress. Each entry: what's open, what blocks closure, where to pick up.

## Blocked
Items waiting on operator decision / external state. Each entry: what's blocked, on whom/what.

## Queued
Stuff that came up but isn't being done this session. Brief list.
```

Tight prose per section. No filler. If a section has nothing, write `(none)` rather than deleting the heading — fixed structure helps future-you scan fast.

**The RESUME-CORE block is load-bearing — it is the ONLY thing the session-start hook injects.** The hook slices the text between the `<!-- RESUME-CORE:START -->` / `<!-- RESUME-CORE:END -->` markers, wraps it with a standing "orient, then await direction — do not auto-continue or fact-find until directed" instruction plus a pointer to the full handoff, and injects just that (~a few hundred tokens, so it always survives the harness's ~2KB session-start inline cap). Everything below the markers — the six sections — is read on demand via the pointer, AFTER the operator directs the work. So keep the core genuinely lean (Worked on / Left hanging, tight bullets) and never rely on any section below it reaching the next session automatically. If the block is missing (a legacy handoff), the hook falls back to a bounded `## Focus` gist + the pointer — never the full body.

### Length cap + the detail sibling (handoff-redesign 2026-06-14, A1 reframe)

Keep the rolling handoff **lean — target ≤ ~1500 tokens** (rough proxy:
~6000 characters; the watchdog soft-nudges only past ~2× that). The lean
handoff answers **"what next"**; the sibling file answers **"how/why."**

**The hard rule — this is the continuity guarantee, not a style choice:**

> The six fixed sections — **Focus, Decisions, Memory pointers, Open threads,
> Blocked, Queued** — ALWAYS stay in the lean handoff, each as a one-liner.
> **None of them ever moves to the sibling.** Only free-text *verbose overflow*
> offloads: command logs, full verification dumps, long rationale beyond the
> one-line "why," status dumps.

A section keeps its **load-bearing one-liner**; only its verbose *tail* moves.
The decision stays; the essay behind it moves. The pointer stays; the full
file-by-file analysis moves. Example, in `## Decisions`:

```
3. Re-stage `latest`, not cebd680 — why: it's a superset, avoids a 2nd
   promote cycle. Full diff analysis → `<session_id>.detail.md`.
```

**Offloading a whole fixed section is a protocol VIOLATION, not a judgment
call.** There is no per-item "is this audit?" decision to get wrong — sections
never move, only un-sectioned verbose text does. Why this matters: the same
agent writes the handoff and later reads it to resume, and the watchdog can see
only the lean handoff's bytes (never the sibling), so a gutted section would be
**silently unrecoverable**. Keeping all six sections makes the worst case
LOUD — leftover detail bloats the lean file and trips the size nudge — instead
of silent.

**The sibling file is session-scoped and flat:** write overflow to
`<session_id>.detail.md` in the **same** `handoffs/` folder as the handoff
(one detail file per session, overwrite in place, same as the handoff). No
subfolder, no registry — it's just a companion file. It shares its handoff's
lifecycle exactly (same folder, same name-stem): whatever removes a handoff
catches the sibling too, and `/allostat-prune` leaves both untouched (the
`handoffs/` folder is outside its scope). No separate lifecycle to manage.

**Always leave a breadcrumb.** When you offload, the section's one-liner ends
with a pointer so future-you (and the watchdog) know the sibling exists:

```
... → `<session_id>.detail.md`.
```

Only create the sibling when you actually have verbose overflow. A short
session needs no detail file — the lean handoff stands alone.

### Index update

On the FIRST write of a session AND when the session's `## Focus` materially shifts, append one line to `_INDEX.md` in the same folder:

```
<ISO timestamp UTC> | <3-6 keywords describing focus> | <session_id>.md
```

Keywords are your editorial pick based on what the session is actually about. `/recall <keyword>` will scan this index.

### Source material

You already have everything in your rolling context. If you need to recover detail from earlier in a long session, your transcript is at `~/.claude/projects/<cwd>/<session_uuid>.jsonl` — Read it directly.

### Examples

**Example 1 — early session, light work:**
```markdown
# 7a3f9c-12

_Rolling Claude-authored handoff. Last update: 2026-05-24T14:22:08Z._

## Focus
Reviewing advisor's consolidated memory brief; sequencing Part A items.

## Decisions
1. A1 cwd scope: Allostat-managed only (token cost outweighs value on non-Allostat cwds).
2. A3 cascade prevention: escalation, not hard cooldown.

## Memory pointers
- `to-allostat/20260524_advisor_brief_CONSOLIDATED_memory_fix_now_plus_plan.md` — the brief
- `from-allostat/20260524_dev_action_plan_response_to_consolidated_memory_brief.md` — my plan

## Open threads
- Action plan filed; waiting on advisor review + operator gate.

## Blocked
(none)

## Queued
- innate-02 precision fix (out of scope for Fix Now)
```

**Example 2 — mid-session, heavy work:**
```markdown
# e3677c-4b

_Rolling Claude-authored handoff. Last update: 2026-05-24T17:45:31Z._

## Focus
Implementing Fix Now Part A — A1 + A2 shipped, A3 in progress.

## Decisions
1. Locked all 3 open operator decisions per autonomous-run authorization.
2. A2 redirect path: `.allostat/audit/` (preserves observability).

## Memory pointers
- `<wrapper_repo>/lib/session_handoff.py` — A2 path redirect lives here
- `<wrapper_repo>/data/appendix_seed/claude_handoff_protocol.md` — this protocol

## Open threads
- A3 watchdog escalation logic — at level 2 of 3 implementation
- A4 dependency audit pending

## Blocked
(none)

## Queued
- A5 benchmark queries (need to mine from session transcripts)
- A6 slash commands
```

### Verify-before-claim (no bare-prose code claims)

A continuity record may NOT assert code-state in bare prose. Every
**"built / shipped / done / fixed"** claim MUST carry a **checkable reference**
— a commit SHA, a `file.py:symbol`, or a test name — or it cannot make the claim.

- Not: `atomic done-marker BUILT`
- But: `atomic done-marker — session_handoff.py:write_done_marker @ <sha>, test test_marker_atomic`

On resume, treat every handoff claim as a **claim to verify** — spot-check the
cited ref exists before relying on it. An unreferenced "done" is a TODO, not a
fact. For deploy / repo / version questions, never report from a handoff or any
prose file — DERIVE from ground truth (`scripts/allostat_state.py`).

(Origin: 2026-06-19 — handoff `8b9c3fd4` asserted two safety prereqs were
"BUILT"; neither existed, and the next session reasoned from the false claim and
burned an hour.)

### Session-start resume behavior (auto-loaded handoff)

When the wrapper auto-loads a fresh handoff body into your SessionStart context
(Proposal A — continuity just happens, with no manual load step), do NOT recite
it back and do NOT ask the operator to confirm it. Open the session with a brief
**update** — "here's where we left off" — in 1–3 lines: what we were working on +
where to pick up. Not the six sections; not a status banner.

**Orient, then hand control back — never self-initiate.** A session-start update
ORIENTS; it does not authorize you to act. Do NOT self-initiate non-trivial work
off a resume — no auto-investigate, auto-resume, or auto-continue — even when the
handoff names a dead run, an open decision, or an in-flight task. (Datapoint: a
resume off a two-word prompt self-escalated into a 57k-token investigation of a
dead run nobody asked about.)

- **Actionable item present** (killed run, open decision, in-flight task) → the
  update NAMES it and PROMPTS, then STOPS: *"last run died uncommitted on X —
  want me to investigate?"* Wait for the answer before doing the work.
- **Clean resume (nothing actionable)** → the brief update IS the handback. Do
  NOT manufacture a question — a forced every-session prompt re-breaks the
  silence rule and grates (especially on a journaling persona). Prompt only on a
  real fork; orient and wait otherwise.

**The load-bearing rule — activity vs. outcome:**

- **Activity** ("we were working on X", "Y was left open") → state it plainly.
  It's orientation; it carries no onus.
- **Outcome** ("X shipped / merged / deployed / live / done") → **label, never
  recite as fact.** In the opening update, present every outcome as "per the
  handoff, unverified" — do NOT run `scripts/allostat_state.py`, SHA/test checks,
  or any fact-finding to confirm it yet. Reconciliation against ground truth
  (`scripts/allostat_state.py` for the cheaply-checkable git / deploy /
  working-tree state) happens ONLY once the operator directs the work, scoped to
  that work — fact-finding follows direction, it does not precede it.

**You carry the onus, not the operator** — for everything checkable. Only the
underivable narrative is handed over, and it is labeled as such. An update that
dumps every claim on the operator to verify is NOT following verify-before-claim.

Persona-adaptive tone, identical mechanism: a journaling operator gets "welcome
back — last time you wrote about…"; a dev gets "where we were: …". The
activity/outcome rule does not change with the voice.

**Stale handoff (past the inline freshness dial) → no update**, just the calm
one-line pointer the wrapper surfaces. Don't auto-recite an abandoned thread.

(Why this is the half that matters: the injection makes continuity *present*; this
behavior makes it *trustworthy*. Without it, a mandated session-start update is an
automated recitation of possibly-stale claims — the phantom, every session.
Origin: advisor 20260619_advisor_proposalA_answers_plus_behavior_spec.)

### What NOT to do

- Don't append; overwrite. One file per session, always current.
- Don't write an empty file just to satisfy the watchdog (anti-pattern detection will catch it and re-fire stronger).
- Don't skip writes hoping operator won't notice — the visibility surface (`/allostat-handoff-status`) shows the operator how often you're writing.
- Don't put operator-content inside sections that would expose to server-side analysis — handoffs stay client-side; this is meta about the session, not the session's content body.
