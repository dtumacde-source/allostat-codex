---
name: Rollout + archival conventions
description: Your deploy / rollout / archival conventions. Loaded by Allostat when rollout/deploy/archival language fires.
type: operator_template
scope: user
shipped_via: Allostat install scaffolder (Bucket B template — empty, customize for your workflow)
---

# Rollout + archival

This file declares your rollout + archival conventions. Allostat loads this when rollout / deploy / archival language fires in your prompt.

## How to use this template

Fill in your specific rollout conventions. The template ships empty — populate as you develop your own deploy discipline.

## Pre-deploy checklist

TODO: describe what must happen before any deploy (e.g., "backup current state, run test suite green, verify external endpoints").

## Backup conventions

TODO: describe how + where backups should land before any state-change ship.

## Archival of superseded files

TODO: describe how to handle files that get replaced (e.g., "rename to `_LEGACY_pre_YYYYMMDD_rollout` suffix; never delete during the rollout").

## Rollback procedure

TODO: describe your rollback path if a deploy goes wrong.

## Post-deploy verification

TODO: describe what must be verified after the deploy completes.
