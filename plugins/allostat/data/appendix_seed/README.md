---
project: meta
description: Allostat appendix-seed directory README. This folder ships canonical default appendix files into operator's ~/.claude/allostat/projects/<project>_appendix/ on SessionStart.
---

# Allostat default appendix seed

Plugin ships canonical default appendix .md files in this folder. On SessionStart for a matched project, `lib/appendix_system.py:seed_default_appendices` copies any missing seed files into the operator's per-project appendix folder.

**Idempotent.** Once seeded, operator edits are preserved across re-installs.

## Per-file frontmatter convention

Each seed .md must have YAML frontmatter declaring its project + topics + load-trigger threshold:

```yaml
---
project: allostat
description: short description for /allostat-appendix-list
topics: [hpa-axis, pillars, drift, voice]
confidence_threshold: 0.7
eager_fallback: false
---
```

## Size caps (advisor brief 2026-05-20 Concern 3)

- Per-file: ≤20 KB
- Total seed dir: ≤100 KB
- Enforced by `lib/appendix_system.py:audit_seed_dir()` — make_bundle.py gates bundle build on this

## v0.6.0 seeded content

(This is the only file in the seed dir at v0.6.0 ship. Per-project seeds will be operator-curated via PRs to allostat-wrapper or via local edits to operator's appendix folder.)
