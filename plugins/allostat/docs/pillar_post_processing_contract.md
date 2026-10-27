# Pillar post-processing contract — v0.6.0 Phase 2 Slice 5

Version: 1.0
Effective: Allostat v0.6.0
Author: dev (closes advisor brief 2026-05-20 Concern 4)

## Privacy invariant (load-bearing)

**Memory CONTENT never crosses to the server.** Pillar nudges that need
operator-specific context use a marker-substitution pattern: server emits
abstract nudge text with `{{memory_context}}` markers; wrapper's
post-tool-use hook reads the operator's memory locally and substitutes
the snippet client-side BEFORE rendering to operator.

Server-side observability of operator memory: ZERO bytes.

## Server response schema additions

`dispatch_post_tool_use` response (existing fields preserved):

```json
{
  "additional_context": "string with optional {{memory_context}} markers",
  "client_state_writes": [ ... ],
  "pillars_invoked": ["stress-response", "voice-keeper", ...]
}
```

New convention (no schema field add):
- The `additional_context` text MAY contain the literal string `{{memory_context}}` as an interpolation marker
- Wrapper's `post-tool-use.py` checks for the marker; if present, calls `memory_reader.interpolate_nudge(text, mem_dir, primary_pillar)` to substitute
- `primary_pillar` is taken from `pillars_invoked[0]` (first-fired pillar gets the context)

## Wrapper-side substitution rules

`memory_reader.interpolate_nudge(nudge_text, memory_root, pillar)`:

| Marker | Source | Per-pillar binding |
|---|---|---|
| `{{memory_context}}` | `nudge_context_from_memory(memory_root, pillar)` | Decision-aware pillars (volume-control, stress-response, confidence-gate) → first locked decision from project_*.md. Voice-aware pillars (voice-keeper) → voice reference path. Other pillars → empty string. |

Snippet bounded at 180 chars per v2.3 default. Configurable via `max_chars` parameter.

## Template ID stability + drift handling

No explicit `template_id` field in v0.6.0 schema. The marker-substitution
pattern is simple enough to not need versioning yet. If the marker
substitution needs to evolve (multiple markers per nudge, conditional
markers, etc.), a future v0.7.0 contract bump may add:

```json
{
  "additional_context": "...",
  "interpolation_targets": [
    {"slot": "memory_context", "accessor": "memory_reader.locked_decisions[0]", "fallback": ""}
  ]
}
```

For now: wrapper's interpolation logic is hardcoded to the single marker
pattern. Server's templates MUST use `{{memory_context}}` literally (or
not at all) for substitution to fire.

## Versioning protocol (for future evolution)

If server adds a marker the wrapper doesn't recognize:
- Wrapper degrades gracefully: emits the raw text (marker visible to
  operator as `{{unknown_marker}}`); logs `pillar_interpolation_unknown_marker`
- Server-side: never remove `{{memory_context}}`; only add new markers as
  parallel patterns

If wrapper version > server template version:
- Wrapper interpolation runs; unknown markers in newer wrapper templates
  pass through unchanged

## Privacy verification

Pre-deploy checks (gates Slice 5 ship):
1. **Code-grep:** zero new fields on outbound dispatch calls. Verify:
   ```bash
   grep -rE "dispatch_post_tool_use\\(|arguments=\\{" hooks/post-tool-use.py
   # → no `context=`, `memory_snippet=`, `memory_content=`, etc.
   ```
2. **Unit test:** mock server response with marker; assert outbound
   server call payload (existing dispatch invocation) is byte-equivalent
   to a control run without the marker present.
3. **Code-state diff against v0.5.0 baseline:** the dispatch call signature
   in `hooks/post-tool-use.py` has NOT changed; only the post-response
   processing has added interpolation. Verify:
   ```bash
   git diff v0.5.0..v0.6.0 hooks/post-tool-use.py | grep -E "^\\+.*dispatch_post_tool_use"
   # → empty (no change to dispatch call signature)
   ```

Post-deploy:
- Server-side `/opt/allostat-server/allostat_server/` grep for memory-content keywords (already documented in v0.5.0 ship brief §3) — must show zero NEW matches vs v0.5.0 baseline.
- Server's request_log has no `request_body` field at all (verified v0.5.0 ship brief §3), so structural inability for content leakage via logs persists.

## Reference implementation

See:
- `lib/memory_reader.py` — `interpolate_nudge()`, `nudge_context_from_memory()`
- `hooks/post-tool-use.py` — calls `interpolate_nudge` when `{{memory_context}}` is detected in server response

## Operator-facing impact

Pillar nudges become context-aware. For example, when stress-response fires
during a deploy action, the operator sees:

```
[allostat] · stress-response: locked: PROD freeze suspended through autonomous run
```

instead of the generic:

```
[allostat] · stress-response: high-risk action detected
```

The "locked: ..." text comes from the operator's `project_<name>.md` file
and is interpolated client-side. The server returned `{{memory_context}}`
in place of that text.
