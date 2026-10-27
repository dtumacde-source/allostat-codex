#!/usr/bin/env python3
"""PostToolUse hook — v0.2.0 thin shim.

v0.2.0 architectural change vs v0.1.12:
  - REMOVED the dispatch gate at line 59 (`if not is_error: return 0`).
    Every tool call now reaches the server-side dispatcher.
  - ENRICHED the local observation with file_path / command / exit_code
    so the server's classifier has actionable detail (not just tool_name).
  - REPLACED the client-side classifier+loop with ONE call to the
    server's `dispatch_post_tool_use` tool, which classifies + invokes
    pillars + aggregates server-side.
  - APPLIED any returned `client_state_writes` via lib/client_state_writer.

This is the load-bearing thin-shim conversion: the wrapper hook is now a
~80-line forwarder. Decision logic lives server-side.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import (  # noqa: E402
    detect_harness,
    disabled,
    emit_additional_context,
    emit_stderr,
    has_mcp_token,
    read_payload,
    resolve_project_root_from_payload,
    should_inject,
)
# _base put wrapper/lib on sys.path — redact every command sink (ultraswarm
# H-4/M-2, 2026-07-07), matching PreToolUse.
from secret_redaction import redact_secrets  # noqa: E402


def _extract_tool_input(payload: dict) -> dict:
    """Pull the toolInput dict from a Claude Code PostToolUse payload."""
    tool_input = payload.get("toolInput") or payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return {}
    return tool_input


def main() -> int:
    """Top-level crash armor (pre-release item 11).

    _base.py contract: hooks never raise — a broken plugin update must
    not traceback on every customer prompt. Any unexpected exception
    degrades to a stderr line + exit 0.
    """
    try:
        return _main()
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"post-tool-use hook crashed (armor): {e}")
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
        read_orphan_salience_state,
        read_recent_observations,
        resolve_state_dir,
        scrub_observations_for_wire,
        write_orphan_salience_state,
    )

    state_dir = resolve_state_dir(project_root)

    # S2 Block 3b (2026-05-27) — deactivation guard. Silent on lapsed/canceled.
    if not should_inject(state_dir):
        return 0

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    is_error = bool(payload.get("isError") or payload.get("is_error"))
    # Preserve a genuine exit_code of 0 — a falsy `or` chain drops it, so
    # successful commands reported no exit code (ultraswarm).
    exit_code = payload.get("exitCode")
    if exit_code is None:
        exit_code = payload.get("exit_code")
    tool_input = _extract_tool_input(payload)
    file_path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    command = tool_input.get("command")

    # Local observation — v0.2.0 enriched with file_path/command/exit_code.
    # v0.2.7 PATCH-129 (2026-05-19): restore v2.3 plugin's tool classification
    # so KISS detector + volume-control + adjacent pillars can fire on
    # file_edited / file_created / file_written event types. Without this,
    # the server's compose_stress_nudge.detect_kiss_drift counts zero
    # edits/creates/writes and KISS never surfaces — root cause of operator-
    # reported "wrapper communication" gap (P9 diagnosis).
    if tool_name:
        details = {"tool_name": tool_name, "is_error": is_error}
        # Deep-audit 2026-07-02 fix: label the failure with an `operation` so
        # the server's repeated-tool-failure detector buckets same-operation
        # retries together instead of collapsing ALL failures into "unknown"
        # and firing a high-severity stress cascade on 3 UNRELATED errors.
        # tool_name is the operation granularity the detector groups on.
        details["operation"] = tool_name
        if exit_code is not None:
            details["exit_code"] = exit_code
        if file_path:
            details["file_path"] = file_path
        if command:
            # Redact secrets BEFORE truncate/store — ultraswarm H-4/M-2
            # (2026-07-07): PreToolUse redacts every command sink but this
            # PostToolUse path stored + transmitted the raw command, leaking
            # credentials (e.g. PGPASSWORD=..., token-in-URL) to
            # observations.jsonl and, via the observation tail, to the server.
            # Redact first so a secret in the first 500 chars is masked.
            details["command"] = redact_secrets(str(command))[:500]

        # Always log the generic event (preserves v0.2.0 behavior for
        # repeated-tool-failure detector and request_log attribution).
        append_observation(
            state_dir,
            "tool_failure" if is_error else "tool_call_succeeded",
            details=details,
        )

        # ADDITIONALLY emit v2.3-parity classified event for KISS / volume-
        # control / adjacent detectors. Only on success — failures stay
        # generic since they don't represent real file activity.
        if not is_error:
            classified_event: str | None = None
            if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
                classified_event = "file_edited"
            elif tool_name == "Write":
                # Distinguish create vs overwrite. Pre-release item 12: this
                # hook fires AFTER the Write completed, so the target always
                # exists here — probing existence now classified every Write
                # as file_written and file_created never fired. The PreToolUse
                # hook stashes pre-write existence; pop it to classify.
                # Missing stash (older PreToolUse, missed fire) degrades to
                # file_written (pre-fix behavior).
                classified_event = "file_written"
                if file_path:
                    try:
                        from write_existence_stash import pop_pre_write_existence  # noqa: E402
                        existed = pop_pre_write_existence(state_dir, str(file_path))
                        if existed is False:
                            classified_event = "file_created"
                    except Exception as e:
                        emit_stderr(f"pre-write existence pop failed: {e}")
            if classified_event is not None:
                classified_details = dict(details)
                classified_details["auto_logged"] = True
                classified_details["v23_parity"] = True
                append_observation(
                    state_dir,
                    classified_event,
                    details=classified_details,
                )

            # ISSUE-005 (2026-08-07): remember scripts this session wrote, so
            # PreToolUse can recognise `bash /tmp/cl.sh` as running text the
            # destructive guard never saw. Best-effort — a tracker that fails
            # leaves innate-02 exactly as covered as it was before.
            try:
                from session_script_tracker import record_write  # noqa: E402
                from session_write_targets import successful_write_targets  # noqa: E402

                for written_path in successful_write_targets(tool_name, tool_input):
                    record_write(
                        state_dir,
                        payload.get("session_id") or payload.get("sessionId"),
                        written_path,
                    )
            except Exception as e:
                emit_stderr(f"session script tracking failed: {e}")

    # Server dispatch — v0.2.0: every tool call, not just errors.
    if not has_mcp_token() or not tool_name:
        return 0

    try:
        from client_state_writer import apply_writes  # noqa: E402
        from mcp_client import MCPClient, MCPClientError  # noqa: E402
    except ImportError as e:
        emit_stderr(f"import failed: {e}")
        return 0

    # Wave-2 H-02 (2026-07-11): scrub raw voice snippets (pre-fix legacy
    # records) at the wire boundary — no response prose in excerpt.observations.
    observations = scrub_observations_for_wire(
        read_recent_observations(state_dir, window=100)
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

    # PATCH-149 (2026-05-22): include session_id in cascade payload so
    # the server-side `_filter_decisions_by_dedup` can suppress
    # same-(session, pillar, body) re-fires within the 5-min window.
    session_id = payload.get("session_id") or payload.get("sessionId")

    # Pre-release item 50 (2026-06-10) — orphan-salience round-trip. The
    # server's orphan-tool-call detector is stateless per request; the
    # wrapper owns the salience state and ships it on every dispatch call.
    # The server returns the updated list in decision.details.orphan_state,
    # persisted below after the response.
    try:
        orphan_salience_entries = read_orphan_salience_state(state_dir)
    except Exception as e:
        emit_stderr(f"orphan salience read failed: {e}")
        orphan_salience_entries = []

    # Existing-customer privacy-disclosure migration (rem-consent, 2026-07-22;
    # fail-closed round 6, 2026-07-23). Withhold the ENTIRE server round-trip
    # while the corrected disclosure is pending: file_path/command ride both
    # the event fields AND the observation tail (tool_call details), so
    # blanking the event alone would still leak via the tail. Local telemetry
    # above already ran; only the network dispatch is skipped. session-start
    # surfaces the corrected notice (presentation); user-prompt-submit records
    # acknowledgment on the next prompt, then transmission resumes.
    try:
        import consent  # noqa: E402
        _disclosure_ok = consent.disclosure_ok_to_transmit()
    except Exception as e:
        # Round 6: a privacy control fails CLOSED toward transmission — this
        # skips the dispatch (fail-closed) instead of transmitting. The hook
        # stays non-blocking (withheld path returns 0).
        emit_stderr(f"disclosure gate check failed: {e}")
        _disclosure_ok = False
    if not _disclosure_ok:
        try:
            append_observation(
                state_dir, "disclosure_migration_withheld",
                details={"hook": "post_tool_use"},
            )
        except Exception:
            pass
        return 0

    try:
        client = MCPClient(harness=detect_harness())
        response = client.call_tool(
            "dispatch_post_tool_use",
            arguments={
                "excerpt": {
                    "observations": observations,
                    "retrievals": retrievals,
                },
                "event": {
                    "tool_name": tool_name,
                    "is_error": is_error,
                    "exit_code": exit_code,
                    "file_path": file_path,
                    # Redact before transmit — mirror PreToolUse's command-sink
                    # redaction (ultraswarm H-4, 2026-07-07). Never send raw
                    # credentials to the server.
                    "command": redact_secrets(str(command)) if command else command,
                    "session_id": session_id,
                    "orphan_salience_state": {
                        "entries": orphan_salience_entries,
                    },
                },
            },
        )
    except MCPClientError as e:
        emit_stderr(f"dispatch_post_tool_use failed: {e}")
        # W43: log MCP failures to observations so downstream auditing can
        # see drops without inspecting stderr.
        append_observation(
            state_dir,
            "mcp_error",
            details={
                "hook": "post_tool_use",
                "error_class": type(e).__name__,
                "status_code": getattr(e, "status_code", None),
            },
        )
        return 0
    except Exception as e:
        emit_stderr(f"dispatch_post_tool_use unexpected error: {e}")
        return 0

    if not isinstance(response, dict):
        return 0

    # Item 50 — persist the server's updated orphan-salience state (the
    # wrapper half of the round-trip). No orphan_state in the response
    # (older server) → leave the local file untouched, so both sides
    # roll independently.
    try:
        for _decision in response.get("decisions") or []:
            if not isinstance(_decision, dict):
                continue
            _details = _decision.get("details")
            _orphan_state = (
                _details.get("orphan_state") if isinstance(_details, dict) else None
            )
            if not isinstance(_orphan_state, dict):
                continue
            _entries = _orphan_state.get("entries")
            if isinstance(_entries, list):
                write_orphan_salience_state(
                    state_dir, [e for e in _entries if isinstance(e, dict)]
                )
            break
    except Exception as e:
        emit_stderr(f"orphan salience persist failed: {e}")

    # Apply server-instructed state writes.
    writes = response.get("client_state_writes") or []
    if writes:
        try:
            apply_writes(state_dir, writes)
        except Exception as e:
            emit_stderr(f"apply_writes failed: {e}")

    # Render aggregated additional context (joined nudges from fired pillars).
    text = response.get("additional_context") or ""

    # PATCH-138 / v0.6.0 Phase 2 Slice 5 — wrapper-side pillar nudge
    # interpolation. Per advisor §1.2: server returns nudge text with
    # `{{memory_context}}` markers; wrapper reads operator's memory file
    # LOCALLY + substitutes the snippet client-side. Zero memory CONTENT
    # crosses to server.
    if text and "{{memory_context}}" in text:
        try:
            import memory_lifecycle  # noqa: E402
            import memory_reader  # noqa: E402
            mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
            if mem_dir is not None:
                # Determine which pillar(s) fired from response metadata
                pillars_invoked = response.get("pillars_invoked") or []
                primary_pillar = pillars_invoked[0] if pillars_invoked else "stress-response"
                text = memory_reader.interpolate_nudge(text, mem_dir, primary_pillar)
                if state_dir is not None:
                    try:
                        from local_state import append_observation  # noqa: E402
                        append_observation(state_dir, "pillar_context_injected", details={
                            "pillar": primary_pillar,
                            "interpolated": "{{memory_context}}" not in text,
                        })
                    except Exception:
                        pass
        except Exception as e:
            emit_stderr(f"memory_reader interpolation failed: {e}")

    if text:
        try:
            emit_additional_context(text, hook_event="PostToolUse")
        except Exception as e:
            emit_stderr(f"emit_additional_context failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
