---
name: allostat-health
description: Check local Allostat health.
---

# /allostat-health

Activation-free local health. Call the local health_check MCP tool and report innate_ruleset_whole and any dropped rules. Check hook delivery and project memory separately. No network reachability or hosted authentication is required.

For the existing multi-project view: `py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" run health`.
Do not report full health from artifact integrity alone.

Resolve <installed-plugin> from this installed plugin's path/native inventory, not the working directory or ambient CLAUDE_PLUGIN_ROOT. Do not guess a cache version.
