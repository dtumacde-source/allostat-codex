---
name: allostat
description: Local Allostat setup, health and marketplace update guidance.
---

# Local Allostat

For initial runtime setup use the packaged local-setup skill. For health use allostat-health; report MCP, hooks and memory separately. There is no account, install code or billing portal.

For an explicit update request, use the host's native marketplace update workflow for this installed marketplace. Do not invoke legacy installer scripts, activation, saved credentials, or edit cached files. A new content version needs its bundled local-setup before use; retain existing memory/settings and restart/reload the host for hook/MCP pickup. Do not report an update complete from a manifest version alone.

For memory work retain allostat-init, allostat-forget, allostat-prune, allostat-tend, allostat-promote, loadhandoff and recall. They keep their existing scope and user approval rules.
