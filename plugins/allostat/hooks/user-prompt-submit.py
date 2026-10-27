#!/usr/bin/env python3
"""UserPromptSubmit hook — v0.2.0 thin shim.

v0.2.0 architectural change vs v0.1.12:
  - CAPTURE prompt text (up to 500 chars) into the local observation
    log so the server's classifier + drift detection have actionable
    context. Operator consented per round4 brief §3.1 + §0:
    "no privacy concerns, RAM only, deleted." Server NEVER persists
    the prompt — only uses it to compute a decision then releases.
  - REPLACED the client-side classifier+loop with ONE call to
    `dispatch_user_prompt_submit` (server-side classification + pillar
    invocation + aggregation).
  - APPLIES returned `client_state_writes` via client_state_writer.

Session-end signal detection (α.5 acceptance) stays client-side as a
fast local check that doesn't require the server.
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
    flush_pending_context,
    get_profile,
    has_mcp_token,
    is_terminally_inactive,
    read_payload,
    resolve_project_root_from_payload,
    should_inject,
)
# _base put wrapper/lib on sys.path.
from secret_redaction import redact_secrets  # noqa: E402

# PATCH-141 v1.1.1 — Interrupt-override chrome suppression.
# Two-layer detection lives in lib/interrupt_detection.py so it can be
# unit-tested in isolation (the hook filename has a hyphen and isn't
# import-friendly). Gate is wired into main() below.
from interrupt_detection import (  # noqa: E402
    last_assistant_was_interrupted,
    looks_like_interrupt_override,
)


# Per round4 brief §3.1: 500-char prompt-text capture. Truncation cap on
# what the server sees + what the local observation log records.
PROMPT_CAPTURE_CHARS = 500


# PATCH-189 (2026-05-23): Layer A observation classifier refinement.
# Classifier moved to lib/operator_correction_classifier.py so it can be
# unit-tested directly (the hook filename has a hyphen and isn't
# import-friendly). Server-side mirror at
# allostat-mcp/allostat_server/dispatch_tools.py:_is_operator_correction
# stays inline (different repo, separate ship pipeline).
from operator_correction_classifier import is_operator_correction as _is_operator_correction_prompt  # noqa: E402, F401


def _resolve_cumulative_tokens(payload: dict, harness: str = "claude") -> int | None:
    """Best-effort extraction of session cumulative token count.

    Slice 3 (Codex adapter): dispatched by explicit `harness` — Codex writes
    token telemetry into its rollout jsonl as `event_msg`/`token_count`
    entries (verified against real rollouts 2026-07-05), a different shape
    from Claude's per-assistant-turn `usage` blocks. Default "claude" keeps
    existing callers unchanged.

    v0.2.7 PATCH-129 (2026-05-19): primary path is reading the transcript
    JSONL file (which Claude Code writes to disk on every turn) and summing
    the most recent assistant turn's `input_tokens + cache_read_input_tokens`.
    This is the v2.3 plugin's working method — `scripts/token_check.py:
    sum_transcript_tokens`. Without this, the wrapper's `cumulative_tokens`
    is always None because Claude Code's UserPromptSubmit payload does NOT
    carry token telemetry directly in any harness version we've tested.
    Result before this fix: server's metabolism pillar never received a
    cumulative_tokens value → handoff threshold checks never fired → silent.

    Pre-release item 30d: the payload-key fallback (probing
    cumulative_tokens/cumulativeTokens/totalTokens inline keys) was removed —
    no harness version has ever exposed token telemetry in the payload.
    """
    # Primary path — read transcript JSONL and sum last assistant turn's
    # in-context tokens. v2.3 plugin behavior, ported here for v0.2.7.
    transcript_path_str = payload.get("transcript_path") or payload.get("transcriptPath")
    if not transcript_path_str:
        return None
    try:
        transcript_path = Path(transcript_path_str)
        if not transcript_path.exists():
            return None
        if harness == "codex":
            return _sum_codex_rollout_tokens_last_turn(transcript_path)
        return _sum_transcript_tokens_last_turn(transcript_path)
    except (OSError, ValueError):
        return None


def _sum_transcript_tokens_last_turn(transcript_path: Path) -> int:
    """Return the most recent assistant turn's in-context token count
    (input_tokens + cache_read_input_tokens). Mirrors v2.3 plugin's
    `scripts/token_check.py:sum_transcript_tokens` (PATCH-034, 2026-05-06):
    metric is THIS session's last-turn context-window occupancy, NOT cumulative
    billing-side sum.

    Why these two fields:
      input_tokens — fresh content uploaded for this turn (in context)
      cache_read_input_tokens — cached content served (still in context)
      cache_creation_input_tokens — drops out (write-side cost, not in next-turn context)
      output_tokens — drops out (assistant's reply doesn't accumulate in context)

    Returns 0 on any read/parse error (silent fail; the wrapper falls back
    to no-threshold-check rather than crashing).
    """
    import json
    last_context = 0
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "assistant":
                    continue
                usage = msg.get("message", {}).get("usage", {})
                last_context = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                )
    except OSError:
        return 0
    return last_context


def _sum_codex_rollout_tokens_last_turn(transcript_path: Path) -> int:
    """Return the most recent Codex turn's context-window occupancy from the
    rollout jsonl: `event_msg`/`token_count` → `info.last_token_usage.
    input_tokens + cached_input_tokens`. Mirrors the Claude metric
    (input + cache_read = what's IN context next turn); output tokens drop
    out. Last telemetry event wins. Real shape (2026-07-05 rollout):

      {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": N, "cached_input_tokens": M, …},
        "model_context_window": 258400}}}

    Returns 0 on any read/parse error or when no telemetry is present
    (silent fail — the wrapper falls back to no-threshold-check).
    """
    import json
    last_context = 0
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload", {})
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info", {})
                usage = info.get("last_token_usage", {}) if isinstance(info, dict) else {}
                if not isinstance(usage, dict):
                    continue
                last_context = (
                    int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("cached_input_tokens", 0) or 0)
                )
    except OSError:
        return 0
    return last_context


def main() -> int:
    """Top-level crash armor (pre-release item 11).

    _base.py contract: hooks never raise — a broken plugin update must
    not traceback on every customer prompt. Any unexpected exception
    degrades to a stderr line + exit 0.
    """
    try:
        return _main()
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"user-prompt-submit hook crashed (armor): {e}")
        return 0


def _main() -> int:
    if disabled():
        return 0
    payload = read_payload()
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return 0

    project_root = resolve_project_root_from_payload(payload)
    if project_root is None:
        return 0

    from local_state import (  # noqa: E402
        append_observation,
        read_orphan_salience_state,
        read_recent_observations,
        read_session_state,
        resolve_state_dir,
        scrub_observations_for_wire,
        update_session_state_key,
        write_orphan_salience_state,
    )
    from session_end_matcher import is_session_end_signal  # noqa: E402

    state_dir = resolve_state_dir(project_root)

    # Local authorization JSON from the blocked R3 candidate is untrusted and
    # never consulted. Remove the exact flat legacy stores as defense in depth;
    # failure does not turn them into authority.
    try:
        import send_authorization  # noqa: E402
        if not send_authorization.purge_untrusted_authority_state(state_dir):
            append_observation(
                state_dir, "untrusted_authority_state_cleanup_failed",
                details={"state_classes": len(send_authorization.LEGACY_STATE_DIRNAMES)},
            )
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"authorization-state cleanup failed: {type(e).__name__}")

    # Cooperative accident prevention only, not authenticated authority.
    # No other rule or native host permission is affected by this phrase.
    try:
        from destructive_confirmation import observe_prompt, PHRASE
        armed = observe_prompt(
            state_dir, prompt,
            session_id=payload.get("session_id") or payload.get("sessionId") or "",
            harness=detect_harness(), turn_id=payload.get("turn_id"),
        )
        if prompt == PHRASE:
            emit_additional_context(
                "One pending destructive request confirmed; retry only that unchanged request."
                if armed else "No unexpired unambiguous destructive request was confirmed.",
                hook_event="UserPromptSubmit",
            )
    except Exception as exc:
        emit_stderr(f"destructive confirmation unavailable: {type(exc).__name__}")

    # S2 Block 3b (2026-05-27) — deactivation guard. Silent on lapsed/canceled.
    # No observations, no MCP dispatch, no watchdog updates, no rule injection.
    # (Value-add regulation only; local safety remains in PreToolUse.)
    if not should_inject(state_dir):
        return 0

    # Slice 3 (Codex adapter): ONE shared flow, guarded at the divergent
    # points. Under codex, four Claude-only blocks are skipped (each depends
    # on machinery that isn't wired for Codex yet — see the guard comments);
    # everything else (observations, now-line, server dispatch + nudge emit)
    # runs identically. Claude resolves to profile "claude" (no --harness
    # argv) and is byte-for-byte unchanged.
    profile = get_profile()
    _is_codex = profile.name == "codex"

    truncated_prompt = prompt[:PROMPT_CAPTURE_CHARS] if prompt else ""
    # ultraswarm (2026-07-07): redact secret shapes from the captured prompt
    # before it is persisted to observations.jsonl OR transmitted — an operator
    # who pastes a credential into a prompt must not leak it. Masking secret
    # VALUES leaves correction phrasing intact, so drift/correction detection is
    # unaffected. Raw `prompt` is still used for local-only signal checks below.
    if truncated_prompt:
        truncated_prompt = redact_secrets(truncated_prompt)

    # α.5 session-end signal detection (local fast check).
    if prompt and is_session_end_signal(prompt):
        append_observation(
            state_dir,
            "session_end_signal_detected",
            details={"phrase": truncated_prompt[:200]},
        )

    # v0.2.0: capture prompt text in observations so server-side classifier
    # + drift detection have actionable context. Operator consented.
    if prompt:
        append_observation(
            state_dir,
            "operator_message",
            details={
                "prompt": truncated_prompt,
                "char_count": len(prompt),
                "truncated": len(prompt) > PROMPT_CAPTURE_CHARS,
            },
        )

        # PATCH-164 (2026-05-22): operator-correction observation persistence.
        # When the prompt matches one of the correction patterns (mirror of
        # server-side dispatch_tools._CORRECTION_PATTERNS), append a separate
        # `operator_correction` observation so pattern-observer.detect_proposals
        # at Stop can fingerprint it and accumulate toward the N=4 cross-
        # session threshold (geometric ladder: N=2 in-session / N=2²=4 cross-session).
        #
        # Pre-PATCH-164 the server-side dispatch detected the correction via
        # _is_operator_correction(prompt) and invoked pattern-observer's
        # fingerprint_event question_type — but the resulting fingerprint
        # was RAM-only on the server and never persisted as an observation
        # the wrapper could re-read. Advisor's 2026-05-22 probe of
        # detect_proposals returned events_seen=0 because observations.jsonl
        # only contained `operator_message` events (which are NOT in
        # pattern_observer._FINGERPRINTABLE_CLASSES). PATCH-164 closes the
        # persistence gap on the wrapper side: when the prompt is a
        # correction, BOTH `operator_message` (audit trail) and
        # `operator_correction` (fingerprintable, for N=4 detection) get
        # appended.
        if _is_operator_correction_prompt(prompt):
            append_observation(
                state_dir,
                "operator_correction",
                details={
                    "subject": truncated_prompt[:80],
                    "operator_language": truncated_prompt,
                },
            )

    # PATCH-141 v1.1.1 — Interrupt-override chrome suppression gate.
    #
    # If operator's message is a bare interrupt-override command (Layer A)
    # OR the most recent assistant turn was interrupted by the operator
    # (Layer B), skip:
    #   - the synchronous `dispatch_user_prompt_submit` server round-trip
    #   - the response-banner `emit_additional_context` call
    #   - the auto_memory_sweep directive emit
    #   - the appendix-content emit
    # ...and return 0 early.
    #
    # The observation log (above) still captures the prompt so the audit
    # trail is intact. SessionStart hook chrome is untouched.
    transcript_path_str = (
        payload.get("transcript_path") or payload.get("transcriptPath") or ""
    )
    layer_a_fired = looks_like_interrupt_override(prompt)
    layer_b_fired = False
    if (not layer_a_fired) and transcript_path_str:
        try:
            layer_b_fired = last_assistant_was_interrupted(Path(transcript_path_str))
        except Exception as e:
            emit_stderr(f"layer_b interrupt detection failed: {e}")
            layer_b_fired = False

    if layer_a_fired or layer_b_fired:
        try:
            append_observation(
                state_dir,
                "interrupt_chrome_suppressed",
                details={
                    "trigger": "layer_a_vocab" if layer_a_fired else "layer_b_transcript_marker",
                    "prompt_excerpt": truncated_prompt[:100],
                },
            )
        except Exception:
            pass
        return 0

    # Proposal B (2026-06-19) — live-time injection. Inject a fresh `now:` line
    # every turn so the agent always has a current clock instead of anchoring on
    # the never-refreshed SessionStart date and confabulating time-of-day across
    # multi-day sessions (the operator-reported "starts telling me to go to bed"
    # drift). Local wall-clock (aware) so the operator's time-of-day is visible.
    # Runs regardless of MCP availability — time is purely local; best-effort,
    # never breaks the turn. The behavioral half (treat `now` as the only clock,
    # no fatigue presumption) ships as the universal claude_livetime_protocol
    # seed loaded at SessionStart.
    try:
        from datetime import datetime as _now_dt  # noqa: E402

        import live_time  # noqa: E402
        _now_line = live_time.format_now_line(_now_dt.now().astimezone())
        emit_additional_context(_now_line, hook_event="UserPromptSubmit")
    except Exception as e:
        emit_stderr(f"live_time inject failed: {e}")

    # A3 watchdog (2026-05-24): bump turn counter + token estimate, consume
    # any pending reminder Stop hook queued. Fires every prompt regardless
    # of MCP availability — wrapper-side only. Reminder text (if any) is
    # injected via additionalContext at the appropriate escalation level
    # (soft / strong / stub-injection).
    # Slice 4: runs under codex too — the Stop-side writer queues there now,
    # and this channel delivers. Token estimate comes from the harness-
    # appropriate transcript reader (codex rollout telemetry vs Claude usage).
    try:
        import handoff_watchdog  # noqa: E402
        import session_handoff  # noqa: E402
        _cum_tokens_for_watchdog = _resolve_cumulative_tokens(
            payload, harness=profile.name,
        )
        _wd_session_id = payload.get("session_id") or payload.get("sessionId") or ""
        # Hand the watchdog the SAME destination SessionStart surfaced. Without
        # it the reminder named a cwd-derived harness path and a session got two
        # different destinations in one run (2026-08-04) — handoffs landed under
        # ~/.claude/projects/ and the operator moved them by hand.
        try:
            _wd_handoff_path = (
                session_handoff.resolve_memory_root(project_root)
                / "handoffs" / f"{_wd_session_id}.md"
            ) if _wd_session_id else None
        except Exception:
            _wd_handoff_path = None
        watchdog_reminder = handoff_watchdog.consume_reminder_if_due(
            state_dir, _wd_session_id, cumulative_tokens=_cum_tokens_for_watchdog,
            handoff_path=_wd_handoff_path,
        )
        if watchdog_reminder:
            try:
                emit_additional_context(
                    watchdog_reminder, hook_event="UserPromptSubmit",
                )
            except Exception as e:
                emit_stderr(f"handoff_watchdog reminder emit failed: {e}")
            try:
                append_observation(
                    state_dir,
                    "handoff_watchdog_reminder_emitted",
                    details={"chars": len(watchdog_reminder)},
                )
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"handoff_watchdog consume failed: {e}")

    if not has_mcp_token() or not prompt:
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
        read_recent_observations(state_dir, window=200)
    )
    cumulative_tokens = _resolve_cumulative_tokens(payload, harness=profile.name)

    # PATCH-194 (2026-05-23) — Phase 3.4b drift silo retrieval. Read
    # operator's local silo entries from `<state_dir>/silos/<class>.jsonl`
    # and pass them as `excerpt.retrievals` so server's recall_silos pillar
    # can match queries against prior inscribed cases. Returns [] when
    # silos/ dir is missing (pre-PATCH-194 baseline) — server's query
    # falls through to no_match cleanly.
    try:
        import silo_excerpt  # noqa: E402
        retrievals = silo_excerpt.read_silo_entries_for_excerpt(state_dir)
    except Exception as e:
        emit_stderr(f"silo_excerpt read failed: {e}")
        retrievals = []

    # PATCH-140 v1.1.0 (Lever D): pass last_surfaced_threshold so server's
    # innate-enforcer rule fires once per band per session.
    _session_state = read_session_state(state_dir) or {}
    last_surfaced_threshold = _session_state.get(
        "last_surfaced_threshold", 0
    )
    if not isinstance(last_surfaced_threshold, (int, float)):
        last_surfaced_threshold = 0

    # PATCH-149 (2026-05-22): include session_id in cascade payload so
    # the server-side `_filter_decisions_by_dedup` can suppress
    # same-(session, pillar, body) re-fires within the 5-min window.
    session_id = payload.get("session_id") or payload.get("sessionId")

    # data-min Option A (2026-07-14): generate pattern-learning proposals LOCALLY
    # from the operator's unscrubbed local observations, so operator language need
    # not cross the wire for the learning loop. Deduped downstream by fingerprint
    # hash, and the client hash is LOCKSTEP with the server's, so this runs safely
    # in parallel with the server path (which a later wire-strip retires). Uses the
    # full local observations, never the scrubbed wire copy. Best-effort.
    try:
        import pattern_observer_local  # noqa: E402
        pattern_observer_local.generate_and_persist(
            state_dir, current_session_id=session_id
        )
    except Exception as _pol_err:
        emit_stderr(f"local pattern proposal gen failed: {_pol_err}")

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
    # fail-closed round 6, 2026-07-23). A customer whose consent.json predates
    # the 2026-07-20 copy correction (or a legacy install that never captured
    # consent) must NOT transmit content under the retired disclosure.
    # session-start surfaces the corrected notice and records that it was
    # PRESENTED (a separate fact from acceptance). Continued use after a
    # CONFIRMED presentation is the affirmative acknowledgment the notice
    # defines — recorded here, on the customer's next prompt, before dispatch,
    # so this very prompt transmits under the accepted corrected disclosure.
    # Until presentation is confirmed the gate stays False and the ENTIRE
    # dispatch is withheld — not just event.prompt but the observation tail
    # too, which carries command/file_path on tool observations. (Local
    # logging, watchdog, the now-line and every guard already ran above; only
    # the server round-trip is skipped.)
    try:
        import consent  # noqa: E402
        if consent.disclosure_migration_needed() and consent.presentation_confirmed():
            # Continued use after a CONFIRMED presentation is the affirmative
            # acknowledgment the notice defines. Recorded before dispatch, so
            # this prompt transmits under the accepted corrected disclosure.
            consent.acknowledge_disclosure()
        _disclosure_ok = consent.disclosure_ok_to_transmit()
    except Exception as e:
        # Round 6: a privacy control fails CLOSED toward transmission. The
        # hook stays non-blocking (withheld path returns 0); only the
        # network dispatch is skipped.
        emit_stderr(f"disclosure gate check failed: {e}")
        _disclosure_ok = False
    if not _disclosure_ok:
        try:
            append_observation(
                state_dir, "disclosure_migration_withheld",
                details={"hook": "user_prompt_submit"},
            )
        except Exception:
            pass
        return 0

    try:
        client = MCPClient(harness=detect_harness())
        response = client.call_tool(
            "dispatch_user_prompt_submit",
            arguments={
                "excerpt": {
                    "observations": observations,
                    "retrievals": retrievals,
                },
                "event": {
                    "prompt": truncated_prompt,
                    "cumulative_tokens": cumulative_tokens,
                    "last_surfaced_threshold": int(last_surfaced_threshold),
                    "session_id": session_id,
                    "orphan_salience_state": {
                        "entries": orphan_salience_entries,
                    },
                },
            },
        )
    except MCPClientError as e:
        emit_stderr(f"dispatch_user_prompt_submit failed: {e}")
        append_observation(
            state_dir,
            "mcp_error",
            details={
                "hook": "user_prompt_submit",
                "error_class": type(e).__name__,
                "status_code": getattr(e, "status_code", None),
            },
        )
        return 0
    except Exception as e:
        emit_stderr(f"dispatch_user_prompt_submit unexpected error: {e}")
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

    writes = response.get("client_state_writes") or []
    if writes:
        try:
            apply_writes(state_dir, writes)
        except Exception as e:
            emit_stderr(f"apply_writes failed: {e}")

        # Phase 2 (2026-05-28): bind any silo_entry writes' fingerprint hashes
        # to the operator's just-dispatched prompt LOCALLY. Silo entries on
        # disk (and on the wire) carry hash + provenance only; the canonical
        # text lives in <state_dir>/silos/canonical_resolutions.jsonl. The
        # wrapper is the only party that can build this mapping — it has the
        # prompt; the server (post-Phase-2) doesn't echo it back.
        try:
            from canonical_resolutions import inscribe as _cr_inscribe  # noqa: E402
            from silo_base import SiloFingerprint  # noqa: E402

            for write in writes:
                if not isinstance(write, dict):
                    continue
                if write.get("target") != "silo_entry.jsonl":
                    continue
                content = write.get("content") or {}
                if not isinstance(content, dict):
                    continue
                silo_class = content.get("silo_class")
                fp_dict = content.get("fingerprint")
                if not isinstance(silo_class, str) or not silo_class:
                    continue
                if not isinstance(fp_dict, dict):
                    continue
                try:
                    sf = SiloFingerprint(
                        class_name=str(fp_dict.get("class_name") or ""),
                        key_tokens=[str(t) for t in fp_dict.get("key_tokens") or []],
                    )
                    fp_hash = sf.to_hash()
                except Exception:
                    continue
                _cr_inscribe(
                    state_dir,
                    fingerprint_hash=fp_hash,
                    canonical_value=truncated_prompt,
                    silo_class=silo_class,
                    session_id=str(session_id or ""),
                )
        except Exception as e:
            emit_stderr(f"canonical_resolutions bind failed: {e}")

    text = response.get("additional_context") or ""
    # W5 (pillar-wiring 2026-06-05) — enrich recall_match nudges with the prior
    # canonical text. The server surfaces silo recall hits HASH-ONLY (privacy
    # moat: it can't read canonical_resolutions.jsonl). Join each match's
    # fingerprint_hash to its wrapper-local canonical text so Claude sees WHAT
    # was resolved before, not just "N prior cases". Best-effort — the base
    # recall body already surfaced in additional_context regardless.
    try:
        import silo_excerpt as _silo_excerpt  # noqa: E402

        _recall_extra = _silo_excerpt.compose_recall_enrichment(
            response.get("decisions") or [], state_dir
        )
        if _recall_extra:
            text = f"{text}\n\n{_recall_extra}" if text else _recall_extra
    except Exception as e:
        emit_stderr(f"recall enrichment failed: {e}")
    # W11 (pillar-wiring 2026-06-05) — autonomic memory-lifecycle routing. Detect
    # operator retirement/forgetting language in the prompt → surface a routing
    # nudge (/allostat-prune review or /recall search). Disambiguation only —
    # never auto-deletes. This wires the pruning trigger detectors that existed
    # but were never called (the "automatic routing" the docs claimed).
    try:
        import pruning as _pruning  # noqa: E402

        _prune_nudge = _pruning.compose_trigger_nudge(truncated_prompt)
        if _prune_nudge:
            text = f"{text}\n\n{_prune_nudge}" if text else _prune_nudge
    except Exception as e:
        emit_stderr(f"pruning trigger nudge failed: {e}")
    if text:
        try:
            emit_additional_context(text, hook_event="UserPromptSubmit")
        except Exception as e:
            emit_stderr(f"emit_additional_context failed: {e}")

        # PATCH-140 v1.1.0 (Lever D): after successful emit, record which
        # threshold was surfaced so the next turn's dispatch suppresses
        # re-firing within the same band.
        try:
            import re as _re
            m = _re.search(
                r"HANDOFF CHECKPOINT\s+—\s+(\d+)k", text
            )
            if m:
                new_threshold = int(m.group(1)) * 1000
                if new_threshold > int(last_surfaced_threshold):
                    update_session_state_key(
                        state_dir,
                        "last_surfaced_threshold",
                        new_threshold,
                    )
        except Exception as e:
            emit_stderr(f"last_surfaced_threshold update failed: {e}")

    # A4 (2026-05-24) — Sunset session-end-signal sweep directive + forced
    # handoff write. The signal-phrase-fires-sweep-prompt pattern was the
    # same cumbersome failure mode operator wanted gone — shifted from
    # operator-side reminder to Claude-side reminder. A3's ambient rolling
    # handoff (every 3-5 turns + metabolism pulses + idle) replaces the
    # signal-eventful firing.
    #
    # Pre-A4 (PATCH-139): UserPromptSubmit matched session-end phrases
    # ("close session", "we're done", standalone "close"), then emitted
    # a sweep directive via additionalContext + (PATCH-181 addition)
    # force-wrote a session-end handoff via session_handoff.
    #
    # Post-A4: the phrase observation still logs above (session_end_matcher
    # path at line ~164) for audit trail; no directive emit; no forced
    # handoff write. Claude's rolling handoffs (A3) are the continuity
    # mechanism; operator no longer needs to type a signal phrase.

    # PATCH-190 / Phase 3.2 — voice-keeper wrapper-side evaluation.
    #
    # Reads the prior assistant turn from Claude Code's session transcript,
    # runs the ported voice-keeper evaluator locally (preserving the
    # ExcerptPayload "NEVER include LLM response content" invariant —
    # server never sees the raw prose), and emits a nudge via
    # additionalContext if recent violations warrant.
    #
    # Throttle via `ALLOSTAT_VOICE_KEEPER_CADENCE`:
    #   0 → disabled, 1 → every prompt (default), N → every Nth prompt.
    #
    # First turn (no prior assistant message) returns None silently.
    # Server-side `voice_keeper_evaluate` MCP tool is preserved for
    # direct-tool-call invocations (operator-explicit text scans);
    # autonomic firing happens here, wrapper-side, per PATCH-143
    # precedent (innate rules moved wrapper-side for the same reason).
    # Codex guard: voice-keeper parses Claude-format transcripts; on a codex
    # rollout it would find no assistant turns — meaningless per-turn file IO
    # plus a turn counter nothing uses. Skipped until a codex-format port.
    if not _is_codex:
        try:
            import voice_keeper_local  # noqa: E402
            # Bump turn counter (used by cadence throttle)
            _vk_state = read_session_state(state_dir) or {}
            _vk_turn = int(_vk_state.get("voice_keeper_turn_count", 0) or 0) + 1
            update_session_state_key(state_dir, "voice_keeper_turn_count", _vk_turn)
            if voice_keeper_local.should_evaluate_this_turn(_vk_turn) and transcript_path_str:
                _vk_recent = [
                    e for e in read_recent_observations(state_dir, window=30)
                    if isinstance(e, dict) and e.get("event") == "voice_violation_detected"
                ]
                _vk_nudge = voice_keeper_local.evaluate_and_record(
                    Path(transcript_path_str),
                    state_dir,
                    _vk_recent,
                    session_id=session_id,
                )
                if _vk_nudge:
                    try:
                        emit_additional_context(_vk_nudge, hook_event="UserPromptSubmit")
                    except Exception as e:
                        emit_stderr(f"voice_keeper nudge emit failed: {e}")
        except Exception as e:
            emit_stderr(f"voice_keeper_local evaluate failed: {e}")

    # Deep-audit 2026-07-02 wire fix (advisor greenlit, release-gating) —
    # client-side drift-recurrence: "you corrected this before". The server's
    # detector was starved (excerpt.corrections never sent — and correction
    # records carry operator content that must not cross the wire). All
    # inputs live locally, so the detection runs HERE, per the PATCH-143
    # precedent of wrapper-side autonomic firing. The server-side detector
    # is preserved for direct tool-call invocations.
    try:
        import drift_recurrence_local  # noqa: E402
        _dr_nudge = drift_recurrence_local.check_and_render(state_dir, session_id)
        if _dr_nudge:
            emit_additional_context(
                f"[allostat / stress-response] {_dr_nudge}",
                hook_event="UserPromptSubmit",
            )
    except Exception as e:
        emit_stderr(f"drift_recurrence_local failed: {e}")

    # PATCH-138 / v0.6.0 Phase 2 Slice 4 — appendix topic-trigger loading.
    # Reads operator's per-project appendix files; loads any whose YAML
    # frontmatter topics match the prompt. Content injected into
    # additionalContext client-side; server never sees appendix content.
    # Codex guard: topic-matched appendix bodies can run to multiple KB —
    # held out of the Codex per-turn budget for slice 3 (tuning decision
    # deferred to a later slice).
    if _is_codex:
        return 0
    try:
        import appendix_system  # noqa: E402
        import conditional_loader  # noqa: E402
        resolution = conditional_loader.resolve_tier_includes(
            cwd=str(project_root) if project_root else "",
            first_prompt=prompt or "",
        )
        if resolution.matched_project:
            appendix_files = appendix_system.load_project_appendix_files(
                Path.home(), resolution.matched_project,
            )
            matched_appendices = appendix_system.match_topics(
                prompt or "", appendix_files,
            )
            if matched_appendices:
                appendix_content = appendix_system.render_appendix_content(matched_appendices)
                if appendix_content.strip():
                    emit_additional_context(appendix_content, hook_event="UserPromptSubmit")
                if state_dir is not None:
                    try:
                        append_observation(state_dir, "appendix_loaded", details={
                            "project": resolution.matched_project,
                            "matched_files": [f.path.name for f in matched_appendices],
                            "topics_hit": list({t for f in matched_appendices for t in f.topics}),
                        })
                    except Exception:
                        pass
    except Exception as e:
        emit_stderr(f"appendix loader failed: {e}")

    return 0


if __name__ == "__main__":
    # Slice 3: deterministic single-object flush for Codex (same pattern as
    # session-start.py — Codex accepts ONE stdout object; the atexit hook in
    # _base stays as fallback). No-op under Claude (buffer never fills).
    try:
        _rc = main()
    finally:
        try:
            flush_pending_context("UserPromptSubmit")
        except Exception:  # best-effort — a hook must never crash the turn
            pass
    sys.exit(_rc)
