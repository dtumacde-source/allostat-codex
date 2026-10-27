---
project: allostat
description: Anatomy mapping (Claude = nervous system, H/P axis = endocrine integrator, hormones, cells, tools), closed loop, three modes, three routing paths, architectural invariants. Loaded ALWAYS-ON for Allostat-managed cwds (post-A1 revert 2026-05-24); topic-conditional list kept for non-Allostat cwds via appendix system.
topics: [architecture, pillars, hpa-axis, regulator, anatomy]
always_on: true
confidence_threshold: 0.7
eager_fallback: false
---

## Allostat architecture (operator-tier, auto-loads when relevant)

The Allostat regulator follows real biological anatomy. Captured here so every session loads the frame, not only Generic Questions sessions.

**Anatomy mapping:**

- **Claude = nervous system** — perception, problem detection, integration. The actual controller. Learns over time which calls to make.
- **H/P axis = endocrine integrator** — the regulatory subsystem for allostasis. Emits hormones once Claude triggers it. Bidirectional channel with Claude.
- **Hormones = pillar signals** on the in-process pub/sub bus.
- **Cells = skills** (writer, designer, voice keeper, editor, researcher, PDF builder, etc.). Single-function each — downstream addons that perform specialized work.
- **Tools** (Read / Write / Bash / MCP / others) are what cells use to do their work.

## The closed loop (the strategic differentiator)

```
operator action → observation (afferent signal) → pillar evaluation (endocrine) →
nudge / context / pause (efferent signal) → operator-visible response →
operator feedback / acceptance → memory update → loop back
```

The loop is allostatic: setpoints adjust based on observed operator behavior. NOT homeostatic (which would maintain a fixed setpoint regardless of context).

## Three modes (operator-felt presence)

1. **Quiet** — pillar fires but no operator-visible chrome. Telemetry only. Default for routine work.
2. **Surface** — pillar fires + emits operator-visible nudge (grey chrome row, banner, additional context).
3. **Block** — pillar fires + emits operator-visible block + requires operator override to proceed. Reserved for lethal-severity rules.

## Three routing paths (afferent signal handling)

1. **Wrapper-local** — wrapper-side detection + wrapper-side response. Latency-sensitive (e.g., voice-keeper, interrupt-override).
2. **Server-mediated** — wrapper sends observation to server's dispatch_* tools; server runs pillar logic + returns response with potential client_state_writes. Async-friendly.
3. **Dispatcher-emit** — server detects → server emits silo entry → wrapper retrieves on next dispatch (silo inscribe→retrieve loop).

## Architectural invariants

- **Allostatic, never homeostatic.** Setpoints adjust; behavior adapts; the regulator learns.
- **Operator's machine = one customer install.** Substitutable. Whatever makes Allostat work for operator must work for any customer.
- **Memory CONTENT stays client-side.** Server returns abstract nudge metadata with interpolation markers; wrapper reads operator's memory locally, interpolates, then renders.
- **Pillars are graceful no-op when input absent** — empty observations → no_op return, never crash.
- **Hot-patches are NEVER a deploy mechanism.** All fixes ship through bundle → install code → installer. Operator updates via installer like any customer.
- **Ship STRUCTURE, never CONTENT.** Install scaffolding ships directory layout + headers + empty templates + mechanism; never operator's specific tree contents.

## The pillars (current set)

- **hypothalamic-axis** — central regulator; session-state router
- **innate-enforcer** — stateless rule matcher (rules.yaml-driven)
- **metabolism** — token-budget homeostasis + work-mode inference
- **stress-response** — anomaly detection + cascade activation
- **voice-keeper** — voice-fidelity evaluation (wrapper-local)
- **onboarding-interview** — calibration question generation
- **recall-silos** — silo entry retrieval + dedup
- **pattern-observer** — operator-correction clustering + rule promotion
- **drift-detection** — instruction-following drift surfacing
- **memory-reader** — operator-memory context for nudge interpolation
- **volume-control** — surface-area discipline (rollout-detected, legacy-aging, etc.)
