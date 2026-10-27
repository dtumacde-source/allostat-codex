#!/usr/bin/env python3
"""Stop hook — v0.2.0 thin shim + session_id debounce.

v0.2.0 architectural changes:
  - SESSION_ID DEBOUNCE: tracks last Stop session_id in
    `<state_dir>/state/last_stop_session.json`. Claude Code emits Stop
    on every model-reply boundary, not only at session-end — so v0.1.12's
    Stop hook fired ~30:1 vs real session count. v0.2.0 fires the heavy
    dusk sweep (server dispatch + proposal extraction) only once per
    distinct session_id.
  - REPLACED the client-side classifier+loop with ONE call to
    `dispatch_stop` (server-side aggregation of volume-control +
    pattern-observer).
  - APPLIES returned `client_state_writes` (e.g., pending_proposals.jsonl).

v0.2.6 PATCH-128 A4 (Stop protocol mismatch fix):
  Claude Code's Stop event silently drops `hookSpecificOutput.additionalContext`
  per its hook docs. Pre-PATCH-128 emit-context calls at Stop were going
  to a black hole. Replaced with persistence-only flow: dispatch_stop
  emits pending_dusk_surface.jsonl client_state_writes; next SessionStart
  reads them and renders the carryover at dawn (where additionalContext
  IS delivered to the agent). Operator gets one consolidated dawn review
  window per dawn/dusk cycle.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import (  # noqa: E402
    detect_harness,
    disabled,
    emit_stderr,
    get_profile,
    has_mcp_token,
    read_payload,
    resolve_project_root_from_payload,
    should_inject,
)

# Hoisted to module scope so helper functions (e.g. _run_consolidation_enforcer)
# and tests can reference it without the per-call inline import. Safe at module
# top because _base (imported above) adds lib/ to sys.path.
from local_state import append_observation  # noqa: E402


def _persist_session_end_debrief_for_dawn(state_dir) -> None:
    """v0.2.6 PATCH-128 A4: PATCH-105 session-end debrief block used to
    be emitted at Stop, but the harness silently drops additionalContext
    at Stop. We now persist the debrief text as a pending_dusk_surface.jsonl
    entry so the next SessionStart can surface it at dawn (when
    additionalContext IS delivered). Toggled off via ALLOSTAT_SESSION_DEBRIEF=0.
    """
    try:
        from session_debrief import build_session_end_debrief  # noqa: E402
    except ImportError as e:
        emit_stderr(f"session_debrief import failed: {e}")
        return
    try:
        block = build_session_end_debrief(state_dir)
    except Exception as e:
        emit_stderr(f"build_session_end_debrief failed: {e}")
        return
    if not block:
        return
    # Persist for dawn surfacing via the standard client_state_writer path.
    try:
        from client_state_writer import apply_writes  # noqa: E402
        from local_state import utc_iso  # noqa: E402
        apply_writes(state_dir, [{
            "target": "pending_dusk_surface.jsonl",
            "mode": "append",
            "content": {
                "timestamp": utc_iso(),
                "pillar": "session-debrief",
                "body": block,
            },
            "pillar_id": "session-debrief",
        }])
    except Exception as e:
        emit_stderr(f"persist session-end debrief failed: {e}")


def _run_consolidation_enforcer(state_dir, project_root) -> None:
    """F1 — Memory Consolidation Enforcer (heavy half, runs at Stop).

    Two tiers, both best-effort and bounded:
      TIER 1 — AUTO de-index any self-declared `status: RETIRED` memory file
        from MEMORY.md (the file stays on disk; only the index line is cut).
      TIER 2 — run the TF-IDF merge detection and PERSIST the candidate pairs
        to the queue file (`<memory>/_processed/merge_queue.json`). SessionStart
        later reads the cached count cheaply and surfaces it — the queue
        persists across abandoned sessions so the proposal isn't lost when a
        Stop never fires for the session that produced it.

    The actual merge stays operator-gated inside /allostat-tend; this only
    PROPOSES. Fires inside the once-per-session_id debounce of the Stop hook.
    Never raises (hook never-raise contract).
    """
    try:
        import memory_lifecycle  # noqa: E402
        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is None:
            return

        # TIER 1 — de-index self-declared RETIRED files (cheap, safe).
        try:
            deindexed = memory_lifecycle.detect_and_deindex_retired(mem_dir)
            if deindexed:
                append_observation(state_dir, "retired_deindexed", details={
                    "count": len(deindexed),
                    "files": deindexed[:20],
                })
        except Exception as e:
            emit_stderr(f"detect_and_deindex_retired failed: {e}")

        # TIER 2 — run detection + persist the merge queue (supersede-idempotent).
        try:
            queue_result = memory_lifecycle.run_merge_detection_and_queue(mem_dir)
            append_observation(state_dir, "merge_proposals_queued", details={
                "count": queue_result.get("count", 0),
                "generation": queue_result.get("generation", 0),
            })
        except Exception as e:
            emit_stderr(f"run_merge_detection_and_queue failed: {e}")
    except Exception as e:
        emit_stderr(f"consolidation enforcer failed: {e}")


def _codex_stop_main(payload, project_root, state_dir, session_id) -> int:
    """Codex Stop path: A3 watchdog check (slice 4) + floor-capture (slice 2).

    Slice 4 closes the nag loop for Codex: both ends now exist (this check
    queues; slice 3's UserPromptSubmit channel consumes + injects next turn).
    The dusk debrief, consolidation enforcer, and dispatch_stop remain
    Claude-only — later slices.

    The floor-capture stays UNCONDITIONAL (`ending_overdue=True`) underneath
    the watchdog, unlike Claude's at-ceiling-only gating: under Codex any
    Stop may be the session boundary (probe 2026-07-05: `codex exec` fires
    Stop once, at session end), so waiting for the escalation ceiling would
    lose short sessions entirely. The two compose correctly: the floor file
    is `autocaptured` → non-substantive to the watchdog → the nag ladder
    keeps climbing until the agent authors a real handoff; and
    detect-before-write means the floor never clobbers that real handoff.
    """
    append_observation(state_dir, "session_end", details={
        "session_id": session_id,
        "harness": "codex",
    })
    try:
        import handoff_watchdog  # noqa: E402
        watchdog_result = handoff_watchdog.check_and_set_reminder_state(
            state_dir, project_root, session_id,
        )
        if watchdog_result.get("action") not in (None, "no_change", "skipped"):
            append_observation(
                state_dir,
                "handoff_watchdog",
                details=watchdog_result,
            )
    except Exception as e:
        emit_stderr(f"codex handoff_watchdog check failed: {e}")
    try:
        import handoff_autocapture  # noqa: E402
        transcript = payload.get("transcript_path") or payload.get("transcriptPath")
        transcript_path = Path(transcript) if transcript else None
        floor_path = handoff_autocapture.maybe_write_floor_handoff(
            transcript_path, project_root, session_id,
            ending_overdue=True, harness="codex",
        )
        if floor_path is not None:
            append_observation(
                state_dir,
                "handoff_floor_autocaptured",
                details={"path": str(floor_path), "trigger": "stop_hook_codex"},
            )
    except Exception as e:
        emit_stderr(f"codex floor-capture failed: {e}")
    return 0


def _is_repeat_stop(state_dir: Path, session_id: str | None) -> bool:
    """v0.2.0 debounce: return True when this Stop's session_id matches
    the last-seen session_id. The dusk sweep skips on repeats.

    State file: `<state_dir>/state/last_stop_session.json` carrying
    {"session_id": "...", "last_stop_at": "..."}.

    Missing session_id (some Claude Code harness versions don't supply
    one) → treat every Stop as a non-repeat (degrade to v0.1.12 behavior).
    File read errors → non-repeat (defensive).
    """
    if not session_id:
        return False
    state_file = state_dir / "state" / "last_stop_session.json"
    last_id: str | None = None
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last_id = data.get("session_id")
        except (OSError, json.JSONDecodeError):
            last_id = None
    return last_id == session_id


def _record_stop_session(state_dir: Path, session_id: str | None) -> None:
    """Persist the session_id we just dispatched for, for next Stop's
    debounce check. Best-effort."""
    if not session_id:
        return
    from datetime import datetime, timezone
    state_file = state_dir / "state" / "last_stop_session.json"
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({
                "session_id": session_id,
                "last_stop_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        emit_stderr(f"could not write last_stop_session.json: {e}")


def main() -> int:
    """Top-level crash armor (pre-release item 11).

    _base.py contract: hooks never raise — a broken plugin update must
    not traceback on every customer prompt. Any unexpected exception
    degrades to a stderr line + exit 0.
    """
    try:
        return _main()
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"stop hook crashed (armor): {e}")
        return 0


def _main() -> int:
    if disabled():
        return 0
    payload = read_payload()
    project_root = resolve_project_root_from_payload(payload)
    if project_root is None:
        return 0

    from local_state import (  # noqa: E402
        append_observation,
        read_recent_observations,
        resolve_state_dir,
        scrub_observations_for_wire,
    )

    state_dir = resolve_state_dir(project_root)

    # S2 Block 3b (2026-05-27) — deactivation guard. Silent on lapsed/canceled.
    # No session_end observation, no handoff watchdog reminders, no
    # session_debrief, no MCP dispatch.
    if not should_inject(state_dir):
        return 0

    session_id = payload.get("session_id") or payload.get("sessionId")

    # Codex harness (slice 2): floor-capture only — everything below this
    # branch is Claude-only machinery until later slices wire its
    # dependencies for Codex. No --harness argv → claude → flow unchanged.
    if get_profile().name == "codex":
        return _codex_stop_main(payload, project_root, state_dir, session_id)

    # Always append the session_end observation (cheap, local-only).
    append_observation(state_dir, "session_end", details={"session_id": session_id})

    # A3 watchdog (2026-05-24): runs on every Stop fire BEFORE the debounce
    # gate. Checks if Claude has written a substantive rolling handoff at the
    # canonical path since last check; if not and thresholds (turns OR tokens)
    # exceeded, queues an escalating reminder for the next UserPromptSubmit
    # to inject. Anti-pattern detection catches junk handoffs (<500 bytes
    # or empty sections). Wrapper-side only — no server round-trip.
    watchdog_result: dict = {}
    try:
        import handoff_watchdog  # noqa: E402
        watchdog_result = handoff_watchdog.check_and_set_reminder_state(
            state_dir, project_root, session_id,
        )
        if watchdog_result.get("action") not in (None, "no_change", "skipped"):
            append_observation(
                state_dir,
                "handoff_watchdog",
                details=watchdog_result,
            )
    except Exception as e:
        emit_stderr(f"handoff_watchdog check failed: {e}")

    # A3 (2026-06-26) — transcript→handoff floor-capture (detect-before-write).
    # If the watchdog escalation has reached the ceiling and the agent STILL
    # hasn't written a substantive handoff, distill a floor handoff from the
    # transcript so the session isn't lost. handoff_autocapture is strict: it
    # writes only when no substantive handoff exists (never overwrites one) and
    # the file is marked autocaptured (so the watchdog keeps expecting a real
    # replacement — it is insurance, not "protocol satisfied"). Best-effort.
    try:
        import handoff_autocapture  # noqa: E402
        ceiling = handoff_watchdog.MAX_ESCALATION_LEVEL
        at_ceiling = watchdog_result.get("escalation_level", 0) >= ceiling
        if at_ceiling and session_id:
            transcript = payload.get("transcript_path") or payload.get("transcriptPath")
            transcript_path = Path(transcript) if transcript else None
            floor_path = handoff_autocapture.maybe_write_floor_handoff(
                transcript_path, project_root, session_id,
                ending_overdue=True, at_ceiling=at_ceiling,
                escalation_level=watchdog_result.get("escalation_level", 0),
            )
            if floor_path is not None:
                append_observation(
                    state_dir,
                    "handoff_floor_autocaptured",
                    details={"path": str(floor_path), "trigger": "stop_hook"},
                )
    except Exception as e:
        emit_stderr(f"handoff_autocapture floor-write failed: {e}")

    # PATCH-181 (2026-05-23): write handoff ABOVE the debounce gate so it
    # fires on every Stop boundary, not just the first-Stop-of-session.
    #
    # Pre-PATCH-181 the handoff write lived BELOW the `_is_repeat_stop`
    # debounce — so when operator typed "end session please" mid-session,
    # the wrapper saw session_id already in `last_stop_session.json` from
    # the FIRST Stop hours earlier, short-circuited at line ~231, and
    # never reached the handoff-write call at line ~334. Operator's
    # explicit "end session" intent produced no fresh handoff. v3 dev's
    # session 5 + operator's "previous session" earlier 2026-05-23 both
    # exhibited this. Same architectural mechanism as Finding #3 (PATCH-180
    # closed that one for detect_proposals).
    #
    # New flow: handoff write moves above the debounce + has its own
    # fingerprint dedup (n_observations_at_prior_handoff). Writes when
    # delta > 0 since prior handoff; skips otherwise. The dispatch_stop
    # body (volume-control + pattern-observer.detect_in_session_overrides)
    # remains debounced as before.
    try:
        from session_handoff import write_session_handoff_if_needed  # noqa: E402
        handoff_path = write_session_handoff_if_needed(
            state_dir, project_root, session_id,
        )
        if handoff_path is not None:
            append_observation(
                state_dir,
                "handoff_written",
                details={"path": str(handoff_path), "trigger": "stop_hook"},
            )
    except Exception as e:
        emit_stderr(f"session-handoff write failed: {e}")

    # v0.2.0 debounce: skip the dusk dispatch when this Stop is a repeat
    # for the same session_id. PATCH-181 moved handoff write above this
    # gate; what remains below is dispatch_stop body (volume-control +
    # pattern-observer.detect_in_session_overrides).
    #
    # PATCH-163 (2026-05-22): debounce GUARD moved ABOVE the dusk persist.
    # Pre-PATCH-163 the persist call was unconditional and ran before this
    # debounce, so every Stop event in a session (including the many
    # repeat-fires Claude Code generates on every `[Request interrupted by
    # user]`) appended another session-debrief copy to pending_dusk_surface.
    # jsonl. Next session-start surfaced ALL of them at dawn — the 8x
    # repetition advisor flagged 2026-05-22 §7. Fix: gate persist on
    # !is_repeat_stop so exactly one debrief lands per distinct session_id.
    if _is_repeat_stop(state_dir, session_id):
        return 0

    # PATCH-128 A4 (gated by PATCH-163): persist visibility debrief for
    # dawn surface (Stop's additionalContext is silently dropped by the
    # harness). Only fires for the FIRST Stop of this session_id; repeat
    # Stops short-circuit above.
    _persist_session_end_debrief_for_dawn(state_dir)

    # F1 — Memory Consolidation Enforcer (heavy half). Tier 1 de-indexes
    # self-declared RETIRED files from MEMORY.md; Tier 2 runs merge detection
    # and persists the queue for the NEXT SessionStart to surface. Placed ABOVE
    # the has_mcp_token() gate ON PURPOSE: this is a purely LOCAL operation
    # (TF-IDF + index rewrite, no server round-trip), and the whole point of the
    # persisted queue is that it survives even when the server is unreachable —
    # so it must run regardless of token presence. Still inside the
    # once-per-distinct-session_id debounce (above), so it fires once per
    # session, not on every Stop boundary. Best-effort — never breaks the hook.
    _run_consolidation_enforcer(state_dir, project_root)

    if not has_mcp_token():
        # Even without server, record the session_id so we don't dispatch
        # repeatedly when MCP comes back up mid-session.
        _record_stop_session(state_dir, session_id)
        return 0

    try:
        from client_state_writer import apply_writes  # noqa: E402
        from mcp_client import MCPClient, MCPClientError  # noqa: E402
    except ImportError as e:
        emit_stderr(f"import failed: {e}")
        _record_stop_session(state_dir, session_id)
        return 0

    # Wave-2 H-02 (2026-07-11): scrub raw voice snippets (pre-fix legacy
    # records) at the wire boundary — no response prose in excerpt.observations.
    observations = scrub_observations_for_wire(
        read_recent_observations(state_dir, window=200)
    )

    # PATCH-194 (2026-05-23) — Phase 3.4b drift silo retrieval. Read
    # operator's local silo entries from `<state_dir>/silos/<class>.jsonl`
    # and pass as `excerpt.retrievals` so server's recall_silos pillar can
    # match queries against prior inscribed cases.
    try:
        import silo_excerpt  # noqa: E402
        retrievals = silo_excerpt.read_silo_entries_for_excerpt(state_dir)
    except Exception as e:
        emit_stderr(f"silo_excerpt read failed: {e}")
        retrievals = []

    try:
        client = MCPClient(harness=detect_harness())
        response = client.call_tool(
            "dispatch_stop",
            arguments={
                "excerpt": {
                    "observations": observations,
                    "retrievals": retrievals,
                },
                "event": {"session_id": session_id},
            },
        )
    except MCPClientError as e:
        emit_stderr(f"dispatch_stop failed: {e}")
        append_observation(
            state_dir,
            "mcp_error",
            details={
                "hook": "stop",
                "error_class": type(e).__name__,
                "status_code": getattr(e, "status_code", None),
            },
        )
        _record_stop_session(state_dir, session_id)
        return 0
    except Exception as e:
        emit_stderr(f"dispatch_stop unexpected error: {e}")
        _record_stop_session(state_dir, session_id)
        return 0

    if not isinstance(response, dict):
        _record_stop_session(state_dir, session_id)
        return 0

    writes = response.get("client_state_writes") or []
    if writes:
        try:
            apply_writes(state_dir, writes)
        except Exception as e:
            emit_stderr(f"apply_writes failed: {e}")

    # PATCH-128 A4: Stop event's additionalContext is dropped by the
    # harness per hook docs. Dispatch_stop now emits dusk content as
    # pending_dusk_surface.jsonl client_state_writes (applied above);
    # next SessionStart reads + surfaces at dawn.

    # LEGACY → _PURGE (cold storage) archival sweep (best-effort). Fires once
    # per distinct session_id (already debounced upstream by _is_repeat_stop).
    # Move-only: _LEGACY/*.md >30d → _PURGE/<today>/. Archive is terminal —
    # nothing is ever deleted. No-op when memory tree absent.
    try:
        import memory_lifecycle  # noqa: E402
        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is not None:
            sweep_result = memory_lifecycle.run_lifecycle_sweep(mem_dir)
            total_swept = len(sweep_result.get("swept_to_purge", []))
            if total_swept > 0:
                append_observation(state_dir, "lifecycle_sweep", details={
                    "swept_to_purge": total_swept,
                    "sweep_errors": len(sweep_result.get("sweep_errors", [])),
                })
    except Exception as e:
        emit_stderr(f"lifecycle_sweep failed: {e}")

    # H1 (S1 closure 2026-05-26): handoff_consolidation removed entirely.
    # Audit finding confirmed architecturally correct — the consolidation
    # sweep conflicted with the rolling-handoff protocol (A3) where Claude
    # overwrites a single per-session file in place. With overwriting,
    # there's no chain to consolidate; the helper was solving a problem
    # that A3 already prevents. handoff_consolidation.py + its tests
    # deleted from the repo; this block (which previously invoked the
    # sweep at Stop) removed.

    # A4 (2026-05-24) — auto_memory_sweep clearance removed. Pre-A4 the
    # UserPromptSubmit hook set a session_ending flag on signal-phrase
    # detection ("close session" etc.) and the Stop hook cleared it after
    # the handoff was generated. A4 sunset the signal-phrase sweep
    # directive emit + the forced handoff write; nothing sets the flag
    # anymore, so nothing needs to clear it. Ambient rolling handoffs (A3)
    # replace the eventful firing.

    _record_stop_session(state_dir, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
