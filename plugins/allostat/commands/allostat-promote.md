---
name: allostat-promote
description: Review pending pattern-observer proposals and promote approved ones to learned rules. The terminal half of the learning loop — turns detected patterns into persisted defaults under .allostat/rules/learned/.
---

# /allostat-promote

This is the operator-facing terminal half of the pattern-observer learning
loop. The server detects recurring operator behavior (N=4 cross-session,
N=2 in-session) and queues proposals to
`<project>/.allostat/pending_proposals.jsonl`. This command lets the
operator review each queued proposal and approve, reject, or defer it.
Approved proposals are persisted as learned-rule YAML the regulator loads
on subsequent sessions.

EXECUTE the steps below — do not narrate the tooling. The active project's
state dir is resolved from cwd by the libs (they walk up for `.allostat/`,
the same way `lib/local_state.resolve_state_dir` does).

## Step 1 — list the pending proposals

Run the reader to surface what is queued:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-review --list
```

If it prints `No pending proposals.`, tell the operator there is nothing
to review and stop. Otherwise it prints one block per pending proposal:
its short hash, the lane (`N=4` cross-session or `⚡fast` in-session
override), the occurrence count + sessions spanned, the fingerprint
(class / pattern / direction), and the suggested rule wording.

## Step 2 — present each proposal for a decision

For each pending proposal, show the operator the suggested rule and ask
for a decision. Use these option keys (mirrors the pattern-observer
surface):

```
  [a] Approve — persist as a project-scoped learned rule
  [b] Reject  — don't add; drop this proposal
  [d] Defer   — ask again later (leaves it pending-but-snoozed)
  [e] Edit the rule wording before saving
```

If the operator picks **[e] Edit**, take their revised wording and use it
in Step 3 in place of the proposal's `suggested_rule_wording`.

## Step 3 — persist approved proposals

For each **approved** proposal, persist it as a learned rule. Pass the
inner proposal dict (the `proposal` object from the queued record; if the
operator edited the wording, set `suggested_rule_wording` to their text)
as a JSON string:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-write --proposal-json '<inner proposal JSON>'
```

It writes `<project>/.allostat/rules/learned/<subject>-<hash>.yaml` and
prints the path. The write is idempotent — re-running it for the same
proposal is safe (same path, no duplicate). The persisted YAML is in the
same shape the innate-rule loader (`lib/innate_rules._load_rules`)
consumes, with a `tier: learned` marker and a `provenance` block linking
the rule back to its proposal hash.

## Step 4 — mark each proposal resolved

After acting on a proposal, record the decision so it drops out of the
pending queue (and never re-prompts). The decision log is a sidecar
(`proposal_resolutions.json`) — the append-only `pending_proposals.jsonl`
is never rewritten.

For an approved proposal, link the artifact you just wrote:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-review --resolve <hash> --status approved --learned-rule-path "<path from step 3>"
```

For a rejected proposal:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-review --resolve <hash> --status rejected
```

For a deferred proposal:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-review --resolve <hash> --status deferred
```

`<hash>` is the full proposal hash (the `--list` view shows the first 8
chars; use the full hash from the queued record, or the 8-char prefix is
accepted by the reader's lookup).

## Step 5 — confirm

Re-run the count and report the result to the operator:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run promote-review --count
```

Tell the operator how many proposals you promoted to learned rules, how
many you rejected/deferred, and where the new rules live
(`.allostat/rules/learned/`). The learned rules take effect on the next
session when the regulator loads the rule set.

## Notes

- **Client-side only.** Detection happens server-side; review + persistence
  are entirely local. The server never sees the operator's decision or the
  learned-rule content.
- **Nudge surfacing.** The session-start surface counts pending proposals
  via `proposal_review.py --count`; when it is non-zero the operator is
  reminded to run `/allostat-promote`.
- **Idempotent + non-destructive.** Re-running this command is safe:
  already-resolved proposals don't reappear, and re-persisting a learned
  rule is a no-op.

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
