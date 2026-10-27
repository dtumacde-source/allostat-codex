# Round 10 — full-text matching, redaction layer deleted (2026-07-24)

**Operator decision, advisor-confirmed.** innate-02 (the destructive-command
guard) now matches the **full, un-redacted command text**. The redaction /
neutralization layer — the machinery that tried to prove a destructive-looking
substring was inert *as data* and remove it before matching — has been deleted
from both the wrapper (`lib/innate_rules.py`) and the server mirror
(`pillars/innate_enforcer.py`).

## Why

Audit rounds 7, 8, and 9 were **all the same failure**: "prove this text is
inert, then remove it before matching" got the inertness proof wrong for some
shell or interpreter construct — read-only producer piped into an executor,
heredoc opener inside a quoted string, double-quoted `git commit -m`
substitution, the Perl comment stripper applied to a non-shell body, the
`git diff` read-only proof that accepted a helper-launching `--ext-diff`. Each
round closed the reported instance; the next round found the same disease in a
new construct. Proving "this text cannot execute" requires modeling shell *plus
every interpreter language's* full lexer — an unbounded surface with no bottom.

Deleting the layer makes the entire bypass class **structurally impossible**:
there is no removed-before-matching step left to get wrong. The suppression
layer is gone, and every covered destructive invocation is now pinned directly
on the **raw** command input (see `test_innate02_fulltext_matching.py`).

Stated precisely, because the sloppy version of this claim is wrong: this is
*not* mathematical monotonicity across inputs. The old transform rewrote the
string before matching, so a handful of benign commands matched **only after**
the transform — i.e. redaction manufacturing its own false positives. Raw
matching correctly does not fire on those. Three such cases were found in the
measurement; privacy-safe synthetic equivalents of all three are pinned as
must-stay-quiet controls in `test_innate02_fulltext_matching.py` and
cross-checked on both engines in
`server/tests/test_innate02_fulltext_cross_engine_parity.py`. The reviewer's
independent literal-differential over the rule set found **no** case where a
real destructive invocation matched under the old transform but fails to match
on raw text — that is external corroboration, not an artifact retained here;
what this tree retains and executes are the regression tests named above.

## The cost, measured

Over **32,919 real recorded commands** (the operator's full Claude Code command
history across 15+ projects), deleting redaction newly fires on **152 commands
(0.46%)** — every one of them inside Allostat's own guard-development sessions
(commit messages describing an `rm -rf` fix, `grep` FOR the pattern, test
payloads quoting it). **Zero** of the 5,166 commands in every other project
fired. Each false positive is a fail-closed **confirm-and-proceed** — an extra
"are you sure?", never a crash, never a silent allow. The operator's
guard-dev environment is the maximum-false-positive case that will ever exist;
a normal user sees strictly fewer. Full measurement:
`audits/20260724_task0_redaction_falsepositive_measurement.md` (in the hub
repo).

### Sanitized aggregate (raw commands deliberately not committed)

The raw measurement corpus is **real operator command history (PII: absolute
paths, handles, addresses)** and is NOT committed — it stays in `.tmp`/`audits`
outside the tracked tree. What is preserved here is the distilled aggregate,
which is what justifies the architecture:

| over-confirm shape | count |
|---|---|
| heredoc body (commit/data) | 71 |
| search FOR the pattern (grep/rg/git grep) | 66 |
| `git commit -m` message prose | 11 |
| other (echo/printf sentinels, probe scaffolds) | 4 |
| **total (of 32,919 commands; 0.46%)** | **152** |

By destructive pattern: `rm -rf` 106, `DROP TABLE` 19, `TRUNCATE` 18,
`git push --force` 3, `rd`/`rmdir /s` 2, `Remove-Item -Recurse` 2,
`git checkout --` 1, `DROP DATABASE` 1.

**Reproducing it without shipping anyone's history.**
`dev_tools/measure_innate02_fire_rate.py` re-derives the aggregate on whatever
corpus the runner has, using this tree's own matcher, and emits **counts only —
never command text**. That contract is enforced executably by
`tests/test_measurement_tool_emits_no_command_text.py`, which runs the utility
against a synthetic sentinel corpus and asserts the sentinels never reach
stdout/stderr while the counts prove it really processed them. (The separate
`tests/test_fixture_privacy_scan.py` walks tracked fixture *directories* — it
never executes this tool and does not cover it.) Scope, stated exactly: it reports the
total innate-02 fire rate and its data-context shape distribution. It **cannot**
reproduce the historical with-redaction-vs-without differential that produced
the 152 figure — the redaction layer no longer exists in this tree, which is the
entire point of round 10. The 152 is a point-in-time measurement recorded above;
the tool substantiates the ongoing rate and its shape mix, which is the number
that actually governs cost from here.

A current run (2026-07-24, this machine) reports **31,842 distinct commands,
587 innate-02 fires (1.84%)**, with 1,383 duplicate rows collapsed. Note that
this is the *total* fire rate — it includes genuine destructive invocations,
which should fire — so it bounds the over-confirmation rate from above rather
than isolating it. The shape breakdown is the useful split: `search-for-pattern`,
`git-commit-message`, `heredoc-body` and `read-echo-inspection` are the
mention/data contexts (292 of 587), while `other` (295) is where real
invocations live.

**The historical property is verified continuously, not asserted.**
`tests/test_transform_only_surrogates_replay.py` replays each transform-only
surrogate through the *pre-deletion* implementation (resolved from git history)
and requires `raw=False, old-transformed=True`. It rejects the three earlier
controls, which were quiet under both matchers and therefore proved nothing —
the gap that made the previous claim invalid.

## The preserved shapes + reintegration path

`tests/fixtures/round10_overconfirm_shapes.jsonl` holds one **synthetic**
smallest-possible literal per over-confirm shape (generic paths, reserved
domains) — the **spec for any future precision layer**, kept so reintegration
is cheap without retaining operator history. Deletion is reversible and
forecloses nothing.

The future version, if the real-world false-positive rate ever justifies weeks
of work, is **not this code rebuilt**. It is a real per-language lexer sitting
as an **optional pre-filter in front of** the matcher that suppresses
provably-inert matches — a clean additive layer, not the current tangle woven
back in. The clean full-text matcher this round leaves behind is what makes
that additive boundary possible. The trigger to build it is **data** — a
measured customer false-positive rate — not a hunch. Most likely it never
clears that bar.
