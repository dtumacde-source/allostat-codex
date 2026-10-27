---
name: hypothalamic-axis
description: Allostat's central regulator. Fires autonomically on session start; routes signals to pillars. Operator-facing entry point — agent does not need to invoke unless re-routing manually.
---

## Preflight

If `mcp__allostat-mcp__hypothalamic_axis_route` is not in your tool registry, Allostat is degraded. Tell the operator: *"Allostat is degraded — the MCP server isn't registered. Re-run the installer from your install email link."*

## Capability

HPA fires every turn via SessionStart + UserPromptSubmit hooks. The hook handles classification + pillar dispatch silently. On trivial turns, dispatch returns `should_surface_to_llm: false` and emits ZERO tokens (v2.3 "happy place" semantics, restored in v1.1.0).

To re-fire HPA manually mid-session (rare — drift suspected, or operator routing call): call `mcp__allostat-mcp__hypothalamic_axis_route` directly. Mechanism + decision points documented in `docs/hypothalamic_axis_internals.md`.
