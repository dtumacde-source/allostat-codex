---
name: allostat-init
description: First-run setup for a new project. Runs the onboarding interview + calibration.
---

# /allostat-init

Initialize Allostat for the active project. v2.4 wrapper flow:

1. Branch: new project or existing? (Skip auto-detect — the operator
   confirms, since detection failures are silent and recoverable.)
2. Phase 0 (REQUIRED): show the locked file layout and accept overrides
   for any path before any other phase starts.
3. New-project: organism selection → voice capture → behavioral
   calibration → project linking → confirmation summary.
4. Existing-project (8-phase): discovery → inventory → confirm-and-
   amplify calibration → architecture audit → optimization proposals
   → apply approved changes → verify → confirm final state.

The wrapper:
  - Fetches `allostat://question-bank/calibration` from the server.
  - Scans the operator's existing rule sources CLIENT-SIDE to pre-fill
    answers from detectable patterns. (The former server-side
    `onboarding_interview_evaluate.precompute_calibration_answers` call
    was deliberately retired for privacy — AUDIT-D25, 2026-05-24: it
    accepted raw operator content — and is permanently inert. Do NOT call
    it; the prefill is a local scan. A wrapper-local precompute helper can
    be added if richer prefill is wanted.)
  - Walks the operator through gaps in conversation.
  - Writes resulting imprinted-rules YAML to `.allostat/imprinted/`
    client-side.
  - Scaffolds `.allostat/state.json` + `.allostat/observations.jsonl`.

No MCP call required beyond the initial question-bank fetch (the prefill
scan is entirely client-side).
