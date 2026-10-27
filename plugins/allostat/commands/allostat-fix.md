---
name: allostat-fix
description: Repair the bundled local runtime without touching memory.
---

# Repair local Allostat

Diagnose first with the bundled local-setup status action. On an explicit repair request, resolve this installed plugin from native inventory and run `py -3.12 -I -B "<installed-plugin>/bootstrap/local_entry.py" repair`. This creates and verifies a new offline runtime, then selects it for this package. Old attempts remain untouched. Failure before publication retains the prior selection. It does not change memory, owner settings, PATH or other plugin registrations.

If installed package bytes are corrupt, use the host native marketplace reinstall workflow for the same trusted catalog first. Never edit cache files, delete runtime state or live locks, or invoke activation, the private profile manager, hosted repair or billing. Restart/reload the host after success and check MCP, hooks and memory separately.
