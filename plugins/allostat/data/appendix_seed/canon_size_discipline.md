---
project: allostat
description: Project canon files at ~/.claude/allostat/projects/<project>.md must stay tight. Cap-and-archive pattern. Decision supersession chains preserved, not deleted. Loaded ALWAYS-ON for Allostat-managed cwds (post-A1 revert 2026-05-24).
topics: [canon, size, appendix-split, supersession]
always_on: true
confidence_threshold: 0.7
eager_fallback: false
---

## Canon size discipline

Project canon files at `~/.claude/allostat/projects/<project>.md` consolidate the project's locked decisions, current state, and core architecture. They have a tendency to grow unbounded as operator iterates. Discipline keeps them useful.

### The cap

**Soft cap: 15 KB per canon file.** When a canon file exceeds 15 KB, the next session that touches it should run a consolidation pass:

1. Identify sections that have grown verbose (often "## Locked decisions" with many supersession chains)
2. Tighten language without losing the supersession chain (strikethrough → arrow → new-value pattern preserves history)
3. Move any extensive narrative to the project's appendix folder (`<project>_appendix/`)
4. Verify size after consolidation

### Why the cap matters

Canon files load into Claude's context every session via `@`-include or direct read. A 50 KB canon file burns context budget even when most of the content isn't relevant to the current task. Tight canon = better context budget = better Claude performance for the same project work.

### Supersession chain pattern

When a decision changes, do NOT delete the old decision. Use the supersession chain pattern:

```markdown
### Pricing — $25/month single tier flat

> Operator directive 2026-07-20: *"the price has been locked at $25/month for
> a long time now"* — $24 is retired.

- ~~$19 universal single tier~~ (locked 2026-05-05)
- → ~~$19 Basic / $24 Pro provisional two-tier~~ (locked 2026-05-06)
- → ~~$24/month single tier flat~~ (locked 2026-05-07)
- → **$25/month single tier flat, no paywalls** (LOCKED, current)
```

The chain above is a live example, not a hypothetical: this file previously
ended at "$24 … LOCKED, current" while the Stripe price had been $25.00/mo for
weeks. Because this seed ships to customers, a stale example about our own
product is a stale *fact* on their machine. Illustrations of a pattern still
have to be true.

Future reads can trace the decision history. Tight, but complete.

### Cap exemptions

- Operator's `_PURPOSE.md` files: no cap (intentional reflection space)
- Appendix files: per-file ≤20 KB, total ≤100 KB per appendix folder (enforced by audit_seed_dir)
- MEMORY.md: 150 chars per entry (auto-enforced by hook)

### How to know when to run the consolidation pass

Two triggers:

1. **Reactive**: a session reads the canon file and notices it has bloated. Run consolidation as a session-end task.
2. **Proactive**: monthly audit pass on all canon files; flag any over 15 KB; consolidation queued.

### What canon discipline is NOT

- It's not "delete history." History stays via supersession chains.
- It's not "cut detail that matters." Detail moves to appendix when relevant; doesn't disappear.
- It's not "auto-summarize via LLM." Operator-readable specificity matters; auto-summary often loses the load-bearing nuance.
