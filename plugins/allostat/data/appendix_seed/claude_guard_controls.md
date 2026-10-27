---
description: How to respond when an Allostat guard pauses an operation. Load when someone asks how to retry or safely resolve a refused action.
topics: [innate, guard, guards, disable, override, overrides, silence, bypass, destructive command, red box, refused, blocked]
confidence_threshold: 0.7
eager_fallback: true
---

# Resolving a guard pause

The remedy is specific to the risk that fired. There is no generic production
switch that safely authorizes unrelated operations.

## Current authorization boundary

The current Claude and Codex hook payloads do not provide an authenticated
operator-input identity or a signing key unavailable to same-user executors.
Local JSON and prompt phrases are not authenticated authority against software
running as the same user. They never override protected writes, permission
changes, or financial actions.

Healthy local hooks do support **cooperative accident prevention** for a closed
set of simple destructive shell requests. After the notice identifies the exact
operation, literal target and consequential options, the operator may send
exactly `allostat:override` as the entire next message. This confirms only the
unchanged pending action in the same project/session/harness, expires in five
minutes and is consumed once after the other guards pass. Any ordinary message
cancels it; competing requests invalidate both. There is no pre-arming.

Supported previews: `rm -rf`, `rm -fr`, `rm -r`, or `rm -f` with one safe literal
relative name; `Remove-Item -LiteralPath NAME -Recurse` with optional `-Force`;
and `git reset --hard` to HEAD in the current project. Composed commands,
private paths, indirect targets, unknown options/tools, and native patch
delete/move operations currently pause without confirmation. An opaque request
ID is not enough to make them safely confirmable. Degraded protection never
offers confirmation and requires repair. This limited preview support is not
universal destructive-operation authorization.

The exact native `apply_patch` tool treats validated add/update content as data,
not shell execution. Delete/move operations still pause, and every file target
retains its independent protected-file and managed-memory checks. Shell tools
and MCP lookalikes do not receive the native-patch exemption.

Use the risk-specific safe alternative: draft without sending, use read-only or
a new file for protected credentials, copy a LEGACY file forward, verify the
canonical folder, or perform the reviewed action through a trusted external client.
A future human-visible host request bridge can extend safe previews; stronger
authentication would require a separate platform capability.

## The twelve guards

Name the specific rule rather than making someone disable a category. If you are
unsure which one fired, the refusal box states the rule id.

| id | what it stops |
|---|---|
| `innate-01` | overwriting secrets, API keys, and credential files |
| `innate-02` | destructive commands pending a supported exact cooperative confirmation; unsupported/degraded cases remain paused |
| `innate-03` | shipping a rollout without archiving what it replaced |
| `innate-04` | editing the wrong copy when several near-identical folders exist |
| `innate-05` | passing session-size checkpoints without a handoff |
| `innate-06` | jumping to production code before brainstorm → plan → execute |
| `innate-07` | creating memory files without sorting and merge checks |
| `innate-08` | deleting aged files instead of moving them to archival staging |
| `innate-09` | retired: messaging is governed by the host and user's instructions, not this memory plugin |
| `innate-10` | OAuth/SSO grants, accepting agreements, payments, API keys, webhooks, account-security changes |
| `innate-11` | crossing a declared workflow decision gate |
| `innate-12` | writing outside a declared canonical workspace |

10 fires on a RECOGNIZED LIST of grant/payment tool shapes — not on
every conceivable one. That boundary is deliberate (operator ruling 2026-08-03):
the shapes MCP tools use to grant access cannot be enumerated in advance, so a
guard claiming to catch all of them would be wrong by an unmeasurable margin
while you planned around the claim. A narrower guard that fires reliably is
worth more than a broad one that misses quietly. Calls that look
permission-shaped but match no recognized shape are recorded, not blocked, so
the gaps are visible and the list grows from real usage. The recognized set and
its edges are documented in the operation inventory audit.

## Do not tell anyone to edit the rule file

Editing a rule's `.yaml` is not a confirmation mechanism.

Every rule is pinned to a content hash. Edit the file — even to improve it —
and the hash no longer matches, so the loader **drops the whole rule**, not
just the line that changed. Someone softening one pattern turns the entire
rule off. For an edited rule the system records the degradation — the next
session start banners it, and the working-set gate writes an
`innate_rule_dropped` record to `.allostat/observations.jsonl` — and
destructive-command coverage falls back to a built-in last-resort pattern set
rather than vanishing. But between the edit and someone reading one of those
signals, the rule as written is not running — and none of it un-drops the
rule. Repair means restoring the file (reinstall, or `/allostat-fix`), not
waiting.

If they want to change what a rule *matches* rather than turn it off, that is a
real change to the shipped rule set, not a local edit — it needs the hash
re-pinned or the rule silently disappears.
