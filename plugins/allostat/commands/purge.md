---
name: purge
description: Move aged _LEGACY memory files (>30d) into _PURGE cold storage on demand. Archive-not-destroy — the manual trigger for the sweep that also runs automatically at session end.
---

# /purge

On-demand run of the memory-tree lifecycle sweep: moves `_LEGACY/*.md` files
older than 30 days into `_PURGE/<today>/` (permanent cold storage). This is the
command the volume-control `legacy_aging` nudge points at when aged `_LEGACY_*`
files accumulate.

**Archive-not-destroy.** The sweep is MOVE-ONLY — nothing is ever deleted
(`_PURGE` is terminal cold storage). It is the same sweep the Stop hook runs
automatically once per session; `/purge` just lets the operator trigger it now
instead of waiting for session end.

Run:

```bash
py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run purge
```

Pass an explicit memory-tree path as the first positional arg if you are not
inside the target project (e.g. `... sweep /path/to/memory`); otherwise the tree
is resolved from the active project's cwd.

The command prints JSON listing what was swept (`swept_to_purge`) and any
`sweep_errors`. No confirmation prompt is needed because the operation is
non-destructive and reversible by moving files back out of `_PURGE/`.

## Related

- `/allostat-prune` — a DIFFERENT lifecycle: archives aged `feedback_*` /
  `project_*` / `reference_*` memory files to `_archived_<YYYYMMDD>/`
  (preview/execute/restore). `/purge` is specifically the `_LEGACY → _PURGE`
  move.
- `/allostat-tend legacy <rule_id>` — moves a rule INTO `_LEGACY/` (the step
  before `/purge` later sweeps it to `_PURGE`).

Client-side only. Backed by `lib/memory_lifecycle.py` (`sweep` verb →
`run_lifecycle_sweep`).

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
