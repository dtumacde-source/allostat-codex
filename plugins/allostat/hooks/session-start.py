#!/usr/bin/env python3
"""SessionStart hook — wrapper plugin entrypoint at session begin.

Emits the operator-facing Allostat banner block. PATCH-103 extends to
five states so silent install failures (including the plugin-inert mode
the screenshot session showed) stop being indistinguishable from healthy:

    ✅ Allostat active.                                       ← healthy
    ⚠ Allostat degraded — plugin not loaded.                  ← Layer 1 fail*
       Fix: <hint>
    ⚠ Allostat degraded — MCP not registered.                ← Layer 2 fail
       Fix: <hint>
    ⚠ Allostat degraded — MCP failing to connect.            ← Layer 4 fail
       Fix: <hint>
    ❌ Allostat unreachable — server not responding.         ← Layer 3 fail

* Layer 1 (plugin not loaded) is paradoxical from the banner's POV:
  if the plugin isn't loaded, the SessionStart hook never fires and the
  banner never renders. The state is only reachable here in the
  fail-soft scenario where claude CLI returns plugin-disabled but the
  hook is somehow still firing. The installer's Step 12b probe is the
  primary catcher of this state.

Additionally appends:
- 📋 Recent handoff — when discover_handoffs finds one <48h old
- An orchestration directive instructing Claude to invoke the
  hypothalamic-axis skill (restores v2.3 inline-visibility surface)

(The rotating session-start tip was RETIRED with the banner — operator
2026-06-13, see the _build_banner docstring — and is no longer emitted.)

Force-display contract (PATCH-102): the banner ALWAYS renders, even when
the cwd-resolution falls back or the .allostat state dir is missing. The
only opt-out is ALLOSTAT_DISABLED=1 (bench-harness flag). Operator
sessions can no longer end up in a silent state.

Server-side: dispatches hypothalamic_axis_route session_start_state when
the token is available (logged silently, never surfaced as an error).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# H-26 — module-load crash-armor. These imports run at MODULE scope, BEFORE
# main()'s try/except can catch anything (main() is defined below and only
# runs after this block succeeds). A broken plugin update — a syntax error in
# _base/local_state, or a bad transitive import they pull in — would otherwise
# traceback on EVERY prompt instead of degrading to a stderr line + exit 0,
# breaking the _base.py "a hook must never traceback" contract. Because
# emit_stderr itself lives in _base (which may be the module that failed), the
# fallback writes straight to sys.stderr and exits 0, so the fail-soft contract
# holds even when the plugin's own base layer is what's broken. Normal behavior
# is unchanged when the imports succeed.
try:
    from _base import (  # noqa: E402
        detect_harness,
        disabled,
        emit_additional_context,
        emit_stderr,
        flush_pending_context,
        get_profile,
        HarnessProfile,
        has_mcp_token,
        read_payload,
        resolve_project_root_from_payload,
        run_after_physical_flush,
        should_inject,
        wrap_pending_context,
    )

    # Pre-release item 30b: hoisted from 15 repeated inline imports. Safe at
    # module top because _base (imported above) adds lib/ to sys.path.
    from local_state import append_observation  # noqa: E402
except BaseException as _module_load_exc:  # noqa: BLE001 — fail soft, never traceback
    # SystemExit/KeyboardInterrupt are re-raised; any other failure degrades.
    if isinstance(_module_load_exc, (SystemExit, KeyboardInterrupt)):
        raise
    sys.stderr.write(
        f"session-start hook: module-load failed (armor): {_module_load_exc}\n"
    )
    sys.exit(0)


# PATCH-140 v1.1.0 — session-start surface prune.
# Pre-prune: the orchestration directive was ~400 tokens injected every
# session start, telling Claude to invoke the hypothalamic-axis skill +
# render the banner once. v1.1.0 collapses both to a single short line.
# HPA fires autonomically via the server-side dispatch later in main();
# Claude doesn't need to be told to invoke a skill, and the banner
# positioning directive is now an operator convention documented in the
# wrapper README rather than re-injected per session.
ORCHESTRATION_DIRECTIVE = ""


# PATCH-105 — single source of truth for state→banner mapping. The
# install_validation module returns one of these five state values; the
# banner-composer must produce a correctly-matched line for each. Tested
# in tests/test_patch105_banner_state_correspondence.py.
_BANNER_STATUS_LINES: dict[str, str] = {
    "healthy": "✅ Allostat active.",
    # pending_restart is a WARN, not a failure (exit 0), but the human MUST be
    # told to act or they stay in limbo — so it renders LOUD, not a quiet note.
    "pending_restart": (
        "⚠ Allostat update landed — FULLY QUIT & REOPEN Claude Code to finish.\n"
        "   {fix_hint}"
    ),
    # The other four require fix_hint interpolation; placeholders below.
    "degraded_plugin_not_loaded": (
        "⚠ Allostat degraded — plugin not loaded.\n"
        "   Fix: {fix_hint}"
    ),
    "degraded_mcp_not_registered": (
        "⚠ Allostat degraded — MCP not registered.\n"
        "   Fix: {fix_hint}"
    ),
    "degraded_mcp_failed_connect": (
        "⚠ Allostat degraded — MCP failing to connect.\n"
        "   Fix: {fix_hint}"
    ),
    "unreachable_server": (
        "❌ Allostat unreachable — server not responding.\n"
        "   {fix_hint}"
    ),
}


def _build_status_line(install_status) -> str:
    """First line of the banner — reflects the composite install state.

    PATCH-105 fix for Defect A (banner-vs-observation mismatch):
      - Use _BANNER_STATUS_LINES as the single source of truth so the
        banner cannot disagree with install_validation's state output.
      - On any unknown / malformed state, emit a NEUTRAL "status unknown"
        line instead of silently defaulting to "✅ Allostat active." —
        falsely claiming health is worse than admitting the gap.
      - On any attribute-access exception (install_status not the expected
        dataclass shape), emit the neutral line too.

    Five known states (precedence per advisor 2026-05-16 review §2.1):
      healthy
      degraded_plugin_not_loaded
      degraded_mcp_not_registered
      degraded_mcp_failed_connect
      unreachable_server
    """
    try:
        state = getattr(install_status, "state", None)
        fix_hint = getattr(install_status, "fix_hint", None) or ""
    except Exception as e:
        emit_stderr(f"_build_status_line: attribute access failed: {e}")
        return _NEUTRAL_STATUS_LINE

    template = _BANNER_STATUS_LINES.get(state)
    if template is None:
        # Unknown / None / unexpected state — be honest about it rather
        # than falsely claim healthy. Pre-PATCH-105 the fallback was the
        # green "✅ Allostat active." line which produced Defect A
        # (banner said active while observations said degraded).
        emit_stderr(
            f"_build_status_line: unknown state {state!r} — emitting neutral "
            f"status line. install_status={install_status!r}"
        )
        return _NEUTRAL_STATUS_LINE

    # Healthy template has no {fix_hint} placeholder — `.format()` with an
    # unused kwarg is fine. The four degraded templates do interpolate it.
    return template.format(fix_hint=fix_hint)


# Pre-release item 14: the doctor invocation branches on the running
# platform — PowerShell command on Windows, bash + the plugin-tree
# allostat-doctor.sh everywhere else (pre-fix the PS1 hint surfaced to
# Mac/Linux customers, who can't run it).
if sys.platform == "win32":
    _NEUTRAL_STATUS_LINE = (
        "ℹ Allostat status unknown — health-check unavailable.\n"
        "   Run: & \"$env:USERPROFILE\\.claude\\plugins\\marketplaces\\local\\allostat-mcp\\scripts\\allostat-doctor.ps1\""
    )
else:
    _NEUTRAL_STATUS_LINE = (
        "ℹ Allostat status unknown — health-check unavailable.\n"
        f"   Run: bash \"{Path(__file__).resolve().parent.parent / 'scripts' / 'allostat-doctor.sh'}\""
    )


def _fail_safe_minimal_banner() -> str:
    """Last-resort banner when the regular composer fails entirely.

    PATCH-105 Defect B fix: pre-patch, an exception anywhere in the banner
    composition path silently dropped the banner — the operator saw a
    bare model response with no Allostat status header at all (post-v0.1.10
    install at 2026-05-17T03:20:50Z reproduced this). The fail-safe path
    in main() catches any exception during _build_banner and falls back
    to this minimal line. The contract "banner ALWAYS renders" is upheld.
    """
    return _RENDER_INSTRUCTION_PREFIX + _NEUTRAL_STATUS_LINE + "\n" + ORCHESTRATION_DIRECTIVE


def _safe_validate_install():
    """Run install validation, fail-soft on any unexpected error.

    Returns an InstallStatus. If validation itself crashes, returns a neutral
    "unknown" status — NOT "healthy". Audit #11 (2026-07-06): a check-module
    bug means we don't know the install state, and fabricating "healthy" hides
    real breakage (esp. dangerous while adding a second harness). "unknown"
    renders as the banner's neutral status line and is never cached."""
    try:
        # P2 (pillar-wiring 2026-06-05): cached variant skips the ~7-9s of
        # `claude` CLI subprocess spawns on every SessionStart when the last
        # healthy result is still fresh (user-global cache, keyed on plugin.json
        # mtime + TTL). Falls through to a live check on miss / degraded state.
        from install_validation import validate_install_cached  # noqa: E402
        return validate_install_cached()
    except Exception as e:
        emit_stderr(f"install_validation failed: {e}")
        from install_validation import InstallStatus  # noqa: E402
        return InstallStatus(
            state="unknown",
            plugin_loaded=None,
            mcp_registered=None,
            mcp_status=None,
            server_reachable=True,
            fix_hint=None,
            detail={"fallback": True, "reason": "validator_crashed"},
        )


# Pre-release item 55 — PyYAML-absent refusal teeth. On stock Python
# without PyYAML AND without the bundle-time vendored *.json rules,
# innate_rules loads ZERO rules and every destructive-action guard
# (rm -rf refusal, secrets-write refusal, force-push refusal, ...)
# silently vanishes. Degradation must be LOUD: observation + stderr
# banner, so the operator knows the guards are not standing.
_INNATE_RULES_DEGRADED_BANNER = (
    "⚠⚠ INNATE RULES UNAVAILABLE — destructive-action guards are NOT active "
    "this session (degraded mode). No loadable rule files were found "
    "(vendored *.json missing AND PyYAML not importable): rm -rf / "
    "force-push / secrets-write refusals will NOT fire. "
    "Fix: reinstall the plugin bundle (ships vendored JSON rules) or "
    "run `pip install pyyaml`."
)


def _emit_innate_rules_degradation_banner(state_dir: Path | None) -> bool:
    """Emit the loud degraded-mode signal when the innate ruleset is not whole.

    P1-7 (2026-07-20 audit): this used to fire only when ZERO rules loaded
    (`rules_available`). A single truncated lethal rule kept the count positive
    and the operator was told the safety net was intact. It now consults the
    coverage check (`ruleset_degraded`), which fires when any expected rule is
    missing or any loaded rule cannot enforce, and it names the offender so the
    banner is actionable.

    Still informational and still best-effort: this emits to stderr and never
    refuses a command or breaks the session.

    Returns True when the banner was emitted (degraded), False when the ruleset
    is whole (or the check itself failed).
    """
    try:
        import innate_rules  # noqa: E402
        degraded, reason = innate_rules.ruleset_degraded()
        if not degraded:
            return False
        rules_dir = innate_rules._bundled_rules_dir()
        if state_dir is not None:
            append_observation(
                state_dir,
                # Event name unchanged for backward compatibility with existing
                # observers and tests; the `reason` field is the new detail.
                "innate_rules_unavailable_banner",
                details={
                    "reason": reason,
                    "rules_dir_found": rules_dir is not None,
                    "rules_dir": str(rules_dir) if rules_dir else None,
                },
            )
        emit_stderr(_INNATE_RULES_DEGRADED_BANNER)
        if reason:
            emit_stderr(f"innate ruleset degraded: {reason}")
        return True
    except Exception as e:
        emit_stderr(f"innate-rules availability check failed: {e}")
        return False


# (Retired 2026-06-13) `_select_session_start_tip` selected a rotating tip from
# the curated catalog for the banner's `*Allostat tip:*` line. The tip surface
# was retired with the banner (operator 2026-06-13, see the _build_banner
# docstring); the function had zero call sites and was removed as dead code
# (H-28). The tip catalog + tip_selector library still ship and are exercised
# by the tip-selector unit tests — only the banner's emission was retired.


# (Retired 2026-06-19) `_find_recent_handoff_line` built the old `📋 Recent
# handoff (...) — say "read the handoff" to load` pointer+nag. Proposal A
# replaces it with `_resolve_handoff_continuity` below, which auto-injects the
# handoff body (no manual load step). `handoff_discoverer.format_banner_notice`
# is retained for any direct callers/tests but is no longer wired into the hook.


# === Proposal A (2026-06-19) — auto-inject the lean handoff body ===

_HANDOFF_INLINE_DEFAULT_MAX_AGE_H = 48.0


def _handoff_inline_max_age_hours() -> float:
    """Freshness dial (advisor guardrail): handoffs newer than this inject their
    body; older ones degrade to a calm one-line pointer. Operator-tunable via
    ALLOSTAT_HANDOFF_INLINE_MAX_AGE_H so the right value can be tested without a
    code change. Falls back to the default on any missing/invalid value.
    """
    raw = os.environ.get("ALLOSTAT_HANDOFF_INLINE_MAX_AGE_H")
    if raw is None:
        return _HANDOFF_INLINE_DEFAULT_MAX_AGE_H
    try:
        val = float(raw.strip())
    except ValueError:
        return _HANDOFF_INLINE_DEFAULT_MAX_AGE_H
    return val if val > 0 else _HANDOFF_INLINE_DEFAULT_MAX_AGE_H


def _resolve_handoff_continuity(
    project_root: Path | None,
    profile: HarnessProfile | None = None,
    state_dir: Path | None = None,
    provenance: str = "cwd",
) -> tuple[str | None, str | None]:
    """Proposal A — return (visible_notice, body_block) for THIS project's latest
    handoff.

      Fresh (≤ dial) → (loaded notice, injected body).
      Stale (> dial) → (age-first warning pointer, None).
      None found     → (None, None).

    D1 (2026-08-03) adds two honesty rails, per the ruling (durable and
    legible, never interruptive):

      * `provenance == "harness_transcript_guess"` — the "project root" is a
        harness directory standing in for an unknown project. Whatever
        handoffs live there belong to a PARALLEL tree; auto-loading one as
        the authoritative resume core is how a 2026-06-27 summary got
        presented as current state. Degraded mode: a warning line that says
        exactly that, NO body injection, one durable
        `continuity_resolution_degraded` observation.
      * Divergence sentinel — the selected newest handoff must live in the
        canonical tree this session WRITES to. If it does not, and the
        canonical tree's newest is older, reads and writes are split across
        two trees: warn with both paths, record
        `continuity_resolution_divergent`, and still inject the newest body
        (it is the best continuity available — the LINE is what must not
        reassure).

    Per-project latest (never a global handoff); excludes the `.detail.md`
    sibling. Best-effort — any failure returns (None, None) so continuity logic
    never breaks session start.

    `profile` controls the injected body's char budget (Codex adapter);
    defaults to `get_profile()` (reads --harness from argv) so existing
    callers/tests stay backward-compatible with the Claude 8000-char cap.
    """
    profile = profile or get_profile()
    if project_root is None:
        return (None, None)
    try:
        from handoff_discoverer import (  # noqa: E402
            discover_handoffs,
            format_degraded_resolution_notice,
            format_divergent_resolution_notice,
            format_loaded_notice,
            format_resume_core_block,
            format_stale_pointer,
            select_latest_handoff,
        )
    except ImportError:
        return (None, None)
    try:
        if provenance == "harness_transcript_guess":
            # F01: "undecodable" was only ever half the story. The decoder
            # also returns None when the name decodes to SEVERAL existing
            # paths — a collision it refuses to pick between. Both land here,
            # so the recorded reason must cover both or the forensic trail
            # says the wrong thing.
            _degraded_reason = (
                "no cwd in payload; transcript dir did not decode to a "
                "unique existing path"
            )
            if state_dir is not None:
                try:
                    append_observation(
                        state_dir, "continuity_resolution_degraded", details={
                            "harness_dir": str(project_root),
                            "reason": _degraded_reason,
                        },
                    )
                except Exception:
                    # Best-effort: the degraded NOTICE below is the load-bearing
                    # half and does not depend on this record landing.
                    pass
            return (format_degraded_resolution_notice(project_root), None)

        # Wide discovery window (1y) so even an old handoff still surfaces a
        # pointer rather than vanishing; the dial decides body-vs-pointer. The
        # advisor deferred an "ancient → nothing" tier, so none is added here.
        entries = discover_handoffs(
            project_root=project_root,
            age_cutoff_hours=8760.0,
            cwd=project_root,
        )
        entry = select_latest_handoff(entries)
        if entry is None:
            return (None, None)

        # Divergence sentinel: the newest handoff and the write target must
        # be the same tree, or the stale number is a resolution bug being
        # reported as fact.
        try:
            from session_handoff import resolve_canonical_handoff_dir  # noqa: E402
            canonical = resolve_canonical_handoff_dir(project_root)
            if entry.path.parent.resolve() != canonical.resolve():
                canonical_newest = 0.0
                if canonical.is_dir():
                    canonical_newest = max(
                        (p.stat().st_mtime for p in canonical.glob("*.md")),
                        default=0.0,
                    )
                if entry.mtime > canonical_newest:
                    if state_dir is not None:
                        try:
                            append_observation(
                                state_dir,
                                "continuity_resolution_divergent",
                                details={
                                    "newest_handoff": str(entry.path),
                                    "canonical_dir": str(canonical),
                                },
                            )
                        except Exception:
                            pass
                    return (
                        format_divergent_resolution_notice(entry, canonical),
                        format_resume_core_block(entry)
                        if entry.age_hours <= _handoff_inline_max_age_hours()
                        else None,
                    )
        except Exception as e:
            emit_stderr(f"divergence sentinel failed: {e}")

        if entry.age_hours <= _handoff_inline_max_age_hours():
            # Inject the LEAN resume core (bounded, survives the ~2KB inline cap),
            # not the full body. `profile` is retained for API/back-compat but the
            # core is size-independent of the harness budget by design.
            return (
                format_loaded_notice(entry),
                format_resume_core_block(entry),
            )
        return (format_stale_pointer(entry), None)
    except Exception as e:
        emit_stderr(f"_resolve_handoff_continuity failed: {e}")
        return (None, None)


def _pending_merge_proposal_line(project_root: Path | None) -> str | None:
    """F1 — SessionStart surface for pending merge proposals (cheap half).

    Reads ONLY the cached count from the merge queue file
    (`<memory>/_processed/merge_queue.json`) — NEVER runs the heavy TF-IDF
    detection at session start (that's the Stop hook's job; detection here
    would re-pay the scan cost on every dawn). When count>0, returns a single
    functional cue line; otherwise None.

    The queue persists across abandoned sessions, so a proposal produced by a
    session whose Stop never fired still surfaces on the NEXT session start.
    Best-effort: any failure returns None (continuity never breaks dawn).
    """
    if project_root is None:
        return None
    try:
        import memory_lifecycle  # noqa: E402
        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is None:
            return None
        count = memory_lifecycle.read_pending_merge_count(mem_dir)
        if count <= 0:
            return None
        return (
            f"{count} merge proposal(s) pending — /allostat-tend to review"
        )
    except Exception as e:
        emit_stderr(f"_pending_merge_proposal_line failed: {e}")
        return None


def _hpa_visible_fire_suppressed() -> bool:
    """Visibility-finding §4.4 Path A — operator opt-in HPA-quiet mode.

    When `ALLOSTAT_HPA_QUIET=1` is set, the orchestration directive that
    drives Claude to invoke the hypothalamic-axis skill via the Skill
    tool is NOT injected at session start. The server-side dispatch from
    main() still fires silently (observation logged); only the visible
    grey-text "Ran skill" entry in the UI is suppressed.

    Pair with the session-end debrief block (visibility-finding §4.2,
    `session_debrief.build_session_end_debrief`) for broader visibility
    that doesn't depend on per-session-start noise.

    Default: NOT suppressed (current behavior — directive always injected).
    Operator opts in via env var per advisor §4.4 Path A recommendation.
    """
    raw = os.environ.get("ALLOSTAT_HPA_QUIET")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


# E9 / PATCH-200 — Render-instruction wrapper for banner content.
#
# Pre-E9, the banner content was injected into additionalContext but Claude
# rendered it only because operator's CLAUDE.md had a SESSION-START DISPLAY
# block (~50 lines authored over many sessions) telling Claude to render
# the hook's additionalContext verbatim as the last visible content.
#
# That CLAUDE.md block is operator-machine-only. Customers have no such
# block, so the banner content was rendered loosely or summarized.
#
# E9 closes the gap: the hook itself emits an explicit render instruction
# alongside the banner content. Customer install gets banner rendering
# without needing any CLAUDE.md block.
#
# Backward-compatible: works whether or not operator's CLAUDE.md still has
# the block. Operator can strip the block post-E9 (separate operator+advisor
# session per the E9 brief §2 "Post-ship operator+advisor work").
#
# E9-A path: drops the memory-arch self-check (`✅ Memory architecture intact.`)
# from the banner. Claude-side @-include verification can only be done by
# Claude inspecting its own context, and the value-vs-complexity tradeoff
# favors A. If @-includes silently fail, behavior degrades and operator
# notices naturally.
_RENDER_INSTRUCTION_PREFIX = (
    "<ALLOSTAT_RENDER_INSTRUCTION>\n"
    "The block below is the Allostat session-start banner. Render it VERBATIM "
    "as the LAST visible content of your first message in this session. Place "
    "it after any standard skill-load reporting Claude Code does, but before "
    "you respond to the operator's prompt. Do not paraphrase, summarize, or "
    "reformat. Do not echo this instruction line itself.\n"
    "</ALLOSTAT_RENDER_INSTRUCTION>\n\n"
)


def _build_banner(
    install_status,
    state_dir: Path | None,
    project_root: Path | None,
    notice_line: str | None = None,
    handoff_notice: str | None = None,
    merge_proposal_line: str | None = None,
    include_auth_recovery: bool = True,
) -> str:
    """Build the operator-facing additionalContext block.

    BANNER RETIRED (operator 2026-06-13): Allostat runs silently in the
    background — its operation is evident from the memory/handoff files it
    writes, not a session-start status banner. So this NO LONGER emits the
    `✅ active` / `⚠ degraded` status line (which was also false-firing on a
    healthy MCP) or the rotating tip.

    What it still surfaces — functional cues only, not chrome:
      - genuine auth-stale (401) warning (a real actionable error: the
        server rejected this session's bearer, so regulation is silently off),
      - the update/restart action notice,
      - the recent-handoff continuity pointer (resume cue).

    When NONE of those are present there is nothing operator-facing, so the
    render instruction is omitted entirely (a healthy session shows nothing).

    `install_status` is retained in the signature (callers still pass it) but
    no longer drives a visible line. E9 render-verbatim wrapping is kept so the
    customer install can render the surviving cues without the operator's
    CLAUDE.md block. ALLOSTAT_HPA_QUIET=1 still suppresses the directive.
    """
    lines: list[str] = []

    # Project opened INSIDE `~/.claude/projects/` (2026-08-04). A functional
    # cue by the strictest reading of this banner's scope: memory and handoffs
    # are landing somewhere that does not travel with the project, and if the
    # project also exists elsewhere the two trees are diverging silently. The
    # operator hit exactly this and had to move handoffs by hand. Detection
    # already existed in resolve_memory_root's neighbourhood; the reporting
    # path is what was missing, so it is wired here rather than left as a
    # function nothing calls.
    if project_root is not None:
        try:
            import session_handoff  # noqa: E402
            _ns_warning = session_handoff.harness_namespace_project_warning(
                project_root
            )
            if _ns_warning:
                lines.append(_ns_warning)
        except Exception as e:
            emit_stderr(f"harness-namespace check failed: {e}")

    # Genuine auth-stale (401) — subscription_gate saw HTTP 401 on its most
    # recent /user/state poll. KEPT (real error, not status chrome): a
    # silently-rejected bearer means zero server-side state writes this session.
    if state_dir is not None and include_auth_recovery:
        try:
            import subscription_gate  # noqa: E402
            if subscription_gate.is_bearer_rejected(state_dir):
                lines.append(
                    "⚠ Allostat auth stale — server rejected this session's "
                    "bearer (HTTP 401)."
                )
                lines.append(
                    "   Fix: "
                    + subscription_gate.bearer_rejected_fix_hint(
                        detect_harness()
                    )
                )
        except Exception as e:
            emit_stderr(f"bearer_rejected banner check failed: {e}")

    # Update notice (E8 version-skew "restart needed" / "update available") —
    # a functional action prompt; kept like the handoff cue.
    if notice_line:
        if lines:
            lines.append("")
        lines.append(notice_line)

    # Recent-handoff continuity cue (Proposal A): the one-line VISIBLE notice —
    # "loaded last handoff" when the body was auto-injected, or a calm pointer
    # for a stale handoff. The handoff BODY rides a separate non-rendered rail in
    # _main(); it must never land in this render-verbatim banner (else the agent
    # prints the whole handoff to the operator every session).
    if handoff_notice:
        if lines:
            lines.append("")
        lines.append(handoff_notice)

    # F1 — pending merge-proposal cue (a functional action prompt, like the
    # handoff/update cues). Cheap cached-count read upstream; nothing heavy here.
    if merge_proposal_line:
        if lines:
            lines.append("")
        lines.append(merge_proposal_line)

    banner = "\n".join(lines)

    # Codex wall-of-text fix (operator report 2026-07-05): under Codex every
    # SessionStart payload travels in ONE combined object, so the E9
    # render-VERBATIM directive gets read against the WHOLE payload (handoff
    # body, memory index, protocols) and the agent dumps it all as its first
    # visible message. Under codex the cue lines are DATA — they ride inside
    # the <allostat_internal_context> envelope, and the separate
    # <allostat_opening_contract> (emitted at flush in __main__) tells the
    # agent to surface them briefly. The Skill-orchestration directive is
    # Claude-UI-specific (grey chrome rows) and is dropped too.
    if detect_harness() == "codex":
        return banner  # may be "" — healthy session surfaces nothing

    directive = "" if _hpa_visible_fire_suppressed() else ORCHESTRATION_DIRECTIVE

    if not banner:
        # Healthy session, nothing to surface — no render instruction at all,
        # just the (invisible) orchestration directive when not suppressed.
        return directive
    if directive:
        return _RENDER_INSTRUCTION_PREFIX + banner + "\n" + directive
    return _RENDER_INSTRUCTION_PREFIX + banner


def _session_start_emission_blocks(banner: str, server_text: str) -> list[str]:
    """Ordered additionalContext blocks to emit at session start.

    The render-verbatim `banner` (functional cues only — the handoff-loaded
    line, genuine auth/update warnings) and the server-side dawn-phase carryover
    (`server_text` — the HPA "resumed" line + the previous-session carryover)
    are returned as SEPARATE blocks, never concatenated.

    Why separate (operator 2026-07-12): the render instruction lives at the top
    of `banner`. Folding `server_text` onto the end (`banner + "\n\n" +
    server_text`) put it UNDER that "render VERBATIM" directive, so whenever a
    cue made the banner non-empty (e.g. a recent handoff), the retired verbose
    banner — "[allostat / hypothalamic-axis] Allostat resumed" + the dawn-phase
    carryover — printed to the operator again every session. Kept apart, the
    carryover still reaches the agent as silent context but is never rendered.
    Empty blocks are omitted so no stray empty envelopes are emitted.
    """
    blocks: list[str] = []
    if banner:
        blocks.append(banner)
    if server_text:
        blocks.append(server_text)
    return blocks


def _read_pending_dusk_surface(state_dir: Path) -> list[dict]:
    """v0.2.6 PATCH-128 A4 — read pending dusk-phase entries persisted by
    the previous session's Stop hook. Returns the list of entries (empty
    on missing file or read error)."""
    pending_file = state_dir / "pending_dusk_surface.jsonl"
    if not pending_file.is_file():
        return []
    import json as _json
    entries: list[dict] = []
    try:
        with pending_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError as e:
        emit_stderr(f"_read_pending_dusk_surface read failed: {e}")
    return entries


def _archive_pending_dusk_surface(state_dir: Path) -> None:
    """After successful dawn-phase surfacing, rotate the pending file to
    an archived name so the same content doesn't resurface every session.
    Forensic preservation: archived file is kept, not deleted.
    """
    pending_file = state_dir / "pending_dusk_surface.jsonl"
    if not pending_file.is_file():
        return
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = state_dir / f"pending_dusk_surface_{stamp}.surfaced.jsonl"
    try:
        pending_file.rename(archived)
    except OSError as e:
        emit_stderr(f"_archive_pending_dusk_surface rename failed: {e}")


def _dispatch_server_state_observation(
    state_dir: Path, project_root: Path,
) -> dict | None:
    """Best-effort server-side state observation. Silent on failure.

    v0.2.0: calls `dispatch_session_start` (the new server-side dispatch
    tool) which handles classification + pillar invocation + aggregation
    server-side. Applies any returned client_state_writes to the operator's
    local .allostat/ tree.

    v0.2.6 PATCH-128 A4: also passes pending_dusk_surface entries from
    last session's Stop in the excerpt; server surfaces them via dawn-phase
    additional_context. Returns the full response dict (or None on
    failure) so the caller can read additional_context for the banner.
    """
    if not has_mcp_token():
        return None
    try:
        from client_state_writer import apply_writes  # noqa: E402
        from mcp_client import MCPClient, MCPClientError  # noqa: E402
    except ImportError as e:
        emit_stderr(f"mcp_client/client_state_writer import failed: {e}")
        return None

    has_allostat_state = (state_dir / "state.json").is_file()
    has_existing_context = any(
        (project_root / candidate).exists()
        for candidate in ("CLAUDE.md", "memory")
    )
    pending_dusk = _read_pending_dusk_surface(state_dir)
    # PRIVACY (deep-audit 2026-07-02, core-promise fix): the dusk carryover
    # body contains raw operator content (prompt excerpts + file paths from the
    # session-end debrief). The server does NOTHING with it but echo it back
    # verbatim for the dawn display — a pointless round-trip that leaks operator
    # content across the wire, violating the content-stays-local contract. We
    # now render the carryover LOCALLY (below) and send an EMPTY list to the
    # server so no operator content leaves the machine.
    local_dusk_text = _compose_dusk_surface_local(pending_dusk)
    # Rotate the pending file NOW, as soon as it's been read + composed locally
    # (2026-07-11). The carryover is captured in `local_dusk_text` for this dawn
    # display, so the file has served its purpose regardless of what the server
    # does next. Previously this archive ran only AFTER a successful dispatch,
    # and the function early-returns None on dispatch failure — so any session
    # where the server was unreachable neither surfaced nor rotated the pile,
    # and debriefs accumulated across sessions and re-surfaced en masse.
    if pending_dusk:
        _archive_pending_dusk_surface(state_dir)
    try:
        client = MCPClient(harness=detect_harness())
        response = client.call_tool(
            "dispatch_session_start",
            arguments={
                "excerpt": {"pending_dusk_surface": []},
                "event": {
                    "has_allostat_state": has_allostat_state,
                    "has_existing_context": has_existing_context,
                    "has_other_projects": False,
                    "local_only": False,
                },
            },
        )
    except MCPClientError as e:
        emit_stderr(f"dispatch_session_start failed: {e}")
        return None
    except Exception as e:
        emit_stderr(f"dispatch_session_start unexpected error: {e}")
        return None

    if not isinstance(response, dict):
        return None
    writes = response.get("client_state_writes") or []
    if writes:
        try:
            apply_writes(state_dir, writes)
        except Exception as e:
            emit_stderr(f"apply_writes (session-start) failed: {e}")

    # Merge the LOCALLY-composed dusk carryover into additional_context so the
    # dawn banner still shows it — the content never left the machine (see the
    # privacy note above; the server no longer receives or echoes it).
    if local_dusk_text:
        existing = response.get("additional_context") or ""
        response["additional_context"] = (
            (existing + "\n\n" + local_dusk_text) if existing else local_dusk_text
        )

    return response


def _collapse_session_debrief(body: str) -> str:
    """Collapse a full session-end debrief body to a one-line pointer.

    The debrief body is a ~60-line "files touched / operator messages" wall
    whose full text already lives in the handoff and the archived
    .surfaced.jsonl. At dawn we only want the headline — the ops/files count —
    plus a pointer to the handoff. Prefer the "N file operations across M
    distinct files" line; fall back to the first non-empty line so an
    unexpected body shape still yields something meaningful (never the wall)."""
    headline = ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "file operations across" in stripped:
            headline = stripped.lstrip("- ").strip()
            break
        if not headline:
            headline = stripped  # first non-empty line as fallback
    if not headline:
        return ""
    return f"last session: {headline} → see handoff"


def _compose_dusk_surface_local(dusk: list[dict]) -> str:
    """Render the previous session's pending dusk-phase content as a dawn-phase
    carryover block, CLIENT-SIDE. Deliberately mirrors the server's former
    `_compose_dawn_dusk_surface` (dispatch_tools.py) — the rendering must happen
    locally now because the dusk body carries operator content that must not
    cross the wire (deep-audit 2026-07-02 privacy fix). Best-effort: any
    malformed entry is skipped; returns "" when there's nothing to show.

    session-debrief entries are COLLAPSED to a single one-line pointer and
    de-duplicated to the most recent one only (2026-07-11): pending_dusk_surface
    accumulates a full debrief per session, and inlining every full body ballooned
    the dawn banner into repeated ~60-line walls. Other pillars (e.g.
    volume-control) are already one line each and render unchanged."""
    if not dusk:
        return ""
    lines = ["[allostat / dawn-phase] Carryover from previous session:"]
    latest_debrief: str | None = None
    for entry in dusk:
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body", "")).strip()
        if not body:
            continue
        pillar = entry.get("pillar")
        if pillar == "session-debrief":
            # Keep only the most recent debrief; collapse to a pointer below.
            latest_debrief = _collapse_session_debrief(body)
            continue
        lines.append(f"  • ({pillar}) {body}" if pillar else f"  • {body}")
    if latest_debrief:
        lines.append(f"  • (session-debrief) {latest_debrief}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _root_project_memory(project_root, state_dir) -> None:
    """Workstream C (R3): make memory project-rooted BEFORE the index injects.

    Reconcile the legacy harness tree into `<project>/memory/` (self-healing,
    newest-wins, archive-not-destroy), then regenerate the harness `MEMORY.md`
    as a thin pointer to the project tree. Ordering matters (council Fork 3):
    this runs ahead of `_inject_project_memory` so the injected index is
    post-reconcile-fresh. Fail-safe: any error is swallowed and we fall through
    to serving the project tree (the inject reads `<project>/memory/` regardless).
    Never raises.
    """
    if project_root is None:
        return
    try:
        import session_handoff  # noqa: E402
        mig = session_handoff.migrate_harness_memory_to_in_project(project_root)
        if state_dir is not None:
            reason = mig.get("skipped_reason")
            benign = (None, "no_harness_tree", "project_root_is_harness_path")
            if (mig.get("errors") or 0) > 0 or reason not in benign:
                # ordering-4: a partial/failed migration must be VISIBLE, not
                # silent — a broken session must not look healthy.
                try:
                    append_observation(state_dir, "memory_migration_failed", details={
                        "project_root": str(project_root),
                        "skipped_reason": reason,
                        "errors": mig.get("errors"),
                        "error_paths": (mig.get("error_paths") or [])[:10],
                        "copied": mig.get("copied"),
                        "reconciled": mig.get("reconciled"),
                    })
                except Exception:
                    pass
        if state_dir is not None and mig.get("migrated"):
            try:
                append_observation(state_dir, "memory_tree_migrated", details={
                    "project_root": str(project_root),
                    "copied": mig.get("copied"),
                    "reconciled": mig.get("reconciled"),
                    "skipped_existing": mig.get("skipped_existing"),
                    "source": mig.get("source"),
                    "dest": mig.get("dest"),
                })
            except Exception:
                pass
        ptr = session_handoff.regenerate_harness_pointer(project_root)
        if state_dir is not None and ptr.get("regenerated"):
            try:
                append_observation(state_dir, "harness_memory_pointer_regenerated",
                                   details={"project_root": str(project_root)})
            except Exception:
                pass
        # D1 (2026-08-03): the STANDARD harness encoding gets its pointer
        # above; stray encodings (bare project name, double-encoded harness
        # path) carried nothing — which is how the ExampleProject banner read a
        # 5-week-old orphan as current state. Stamp every populated orphan
        # with the same do-not-read-here pointer; content is never moved or
        # deleted.
        orphans = session_handoff.reconcile_orphan_harness_trees(project_root)
        if state_dir is not None and orphans.get("stamped"):
            try:
                append_observation(state_dir, "orphan_memory_trees_stamped",
                                   details={
                                       "project_root": str(project_root),
                                       "stamped": orphans["stamped"],
                                   })
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"project-memory rooting failed: {e}")


def _inject_project_memory(project_root, state_dir, profile=None) -> None:
    """Phase 1 (project-rooted memory): inject the project-dir MEMORY.md INDEX
    into SessionStart additionalContext, so memory loads from <project>/memory/
    instead of depending on Claude Code's C-drive auto-load.

    Additive — does NOT remove the harness auto-load (the later consolidation +
    tombstone phases do that, gated on this load being verified). Index only;
    leaf files load on demand. Best-effort: any failure is logged + swallowed
    so a broken inject never breaks session start.

    Known + benign (ordering-2): on a customer's FIRST upgraded session the
    harness still auto-loads the full MEMORY.md AND this injects it, so the index
    loads twice that one session. It self-corrects next session, once
    `_root_project_memory` regenerates the harness MEMORY.md into a thin pointer.
    Do NOT skip the inject to dodge this — skipping risks a blank session when the
    harness auto-load did not fire.

    `profile` controls the injected block's char budget (Codex adapter);
    defaults to `get_profile()` (reads --harness from argv). Claude's profile
    has `memory_max_chars=None` (uncapped) so its behavior is unchanged.
    """
    profile = profile or get_profile()
    if project_root is None:
        return
    try:
        import memory_lifecycle  # noqa: E402
        import memory_reader  # noqa: E402
        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is None:
            return
        block = memory_reader.build_memory_index_context(
            mem_dir, max_chars=profile.memory_max_chars
        )
        if not block:
            return
        if profile.memory_max_chars is not None and len(block) > profile.memory_max_chars:
            # Backstop only — the renderer above already fitted the budget with
            # the on-disk pointer preserved. This catches a header that ran
            # long, not the index body.
            block = block[: profile.memory_max_chars] + (
                "\n\n[memory index truncated for context budget — full MEMORY.md on disk]"
            )
        emit_additional_context(block, hook_event="SessionStart")
        if state_dir is not None:
            try:
                append_observation(state_dir, "project_memory_index_injected", details={
                    "path": str(mem_dir / "MEMORY.md"),
                    "byte_count": len(block),
                })
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"project memory inject failed: {e}")


def _reconcile_memory_index(project_root, state_dir) -> None:
    """Fold unindexed leaves into MEMORY.md before the index is read.

    Append-only and non-destructive: no existing index line is rewritten or
    reordered, no leaf is moved or deleted. Best-effort — a failure here must
    never break session start, and leaves the index exactly as it was (the
    injected block then carries its own "may be incomplete" note, since the
    orphans it warns about are still there to be found).
    """
    if project_root is None:
        return
    try:
        import memory_lifecycle  # noqa: E402

        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is None:
            return
        report = memory_lifecycle.reconcile_orphans(mem_dir)
        if state_dir is not None and (report.get("indexed") or report.get("failed")
                                      or report.get("pairs")):
            try:
                append_observation(state_dir, "index_reconciled", details={
                    "scanned": report.get("scanned", 0),
                    "indexed": len(report.get("indexed") or []),
                    "failed": len(report.get("failed") or []),
                    "sections": report.get("sections") or {},
                    "convention_pairs": len(report.get("pairs") or []),
                })
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"memory index reconcile failed: {e}")


def _recover_topical_memory(project_root, state_dir) -> None:
    """Resume a leaf-first managed-record transaction only when dirty.

    The clean path checks one fixed marker and performs no leaf/index read or
    write. Recovery runs before ordinary orphan reconciliation and injection.
    """
    if project_root is None:
        return
    try:
        import memory_lifecycle  # noqa: E402
        import topical_memory_records  # noqa: E402

        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is None or not topical_memory_records.dirty_marker_path(mem_dir).is_file():
            return
        report = topical_memory_records.recover_if_dirty(mem_dir)
        if state_dir is not None:
            append_observation(
                state_dir, "topical_memory_recovered",
                details={"status": report.get("status"), "reads": report.get("reads", 0), "writes": report.get("writes", 0)},
            )
    except Exception as e:
        emit_stderr(f"topical memory recovery failed: {type(e).__name__}")


def _filter_appendix_exclude(files, profile):
    """Slice 2 (write-loop) — apply the profile's granular appendix mute.

    Returns `files` minus any whose filename is in `profile.always_on_exclude`.
    Claude's exclude set is empty (identity — byte-for-byte unchanged). Codex
    excludes only the livetime/clock protocol until its per-turn `now:` line
    (UserPromptSubmit) lands for Codex; the handoff-authoring protocol now
    injects there because slice 2 wired the real write mechanism behind it.
    """
    if not profile.always_on_exclude:
        return files
    return [f for f in files if f.path.name not in profile.always_on_exclude]


def _inject_learned_rules(state_dir) -> None:
    """Deep-audit 2026-07-02 (advisor greenlit, release-gating): inject the
    operator's approved learned rules as standing session defaults.

    This is the terminal half of the learning loop that was never wired —
    /allostat-promote persisted rules to `<state_dir>/rules/learned/` and
    nothing ever read them. Guidance-tier by construction: the rules are
    surfaced as context, never fed to the refusal path. Best-effort."""
    if state_dir is None:
        return
    try:
        from learned_rule_writer import (  # noqa: E402
            read_learned_rules,
            render_learned_defaults_block,
        )
        rules = read_learned_rules(state_dir)
        block = render_learned_defaults_block(rules)
        if not block:
            return
        emit_additional_context(block, hook_event="SessionStart")
        try:
            append_observation(state_dir, "learned_rules_injected", details={
                "count": len(rules),
            })
        except Exception:
            pass
    except Exception as e:
        emit_stderr(f"learned rules inject failed: {e}")


def _record_presentation_best_effort() -> None:
    """Commit 'the corrected disclosure was shown' — called only from a point
    where physical output is proven (immediate emit under Claude; post-flush
    callback under Codex). Never raises: a failed record just means the
    presentation is re-attempted next session."""
    try:
        import consent  # noqa: E402
        consent.record_disclosure_presented()
    except Exception as e:
        emit_stderr(f"disclosure presentation record failed: {e}")


def main() -> int:
    """Top-level crash armor (pre-release item 11).

    _base.py contract: hooks never raise — a broken plugin update must
    not traceback on every customer prompt. Any unexpected exception
    degrades to a stderr line + exit 0.
    """
    try:
        return _main()
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"session-start hook crashed (armor): {e}")
        return 0


def _main() -> int:
    if disabled():
        return 0

    # Codex adapter: resolve the harness injection profile once per session
    # (reads --harness from argv; defaults to the claude profile). Threaded
    # through to _inject_project_memory + _resolve_handoff_continuity below
    # so their char budgets follow the active harness.
    profile = get_profile()

    # NOTE: install validation used to run HERE, ahead of the entitlement gate
    # (PATCH-102's "force-display" contract: validate first so a banner always
    # renders even when downstream context resolution fails). It has moved to
    # just below the gate — see the comment there for why. PATCH-102's intent is
    # preserved: validation still precedes banner composition on every path that
    # produces a banner.

    payload = read_payload()
    # D1 (2026-08-03): carry the resolution's PROVENANCE. A no-cwd payload
    # used to silently substitute the transcript's harness directory as
    # "project root," and every downstream consumer — banner, memory
    # injection, handoff write target — then operated on a parallel orphan
    # universe with full confidence (the ExampleProject 5-week-stale banner).
    from _base import resolve_project_root_with_provenance  # noqa: E402
    project_root, root_provenance = resolve_project_root_with_provenance(payload)

    state_dir: Path | None = None
    if project_root is not None:
        try:
            from local_state import resolve_state_dir  # noqa: E402
            state_dir = resolve_state_dir(project_root)
        except Exception as e:
            emit_stderr(f"state_dir resolve failed: {e}")

    # S2 Block 2b (2026-05-27) — refresh subscription_state cache early so
    # the deactivation guard below has a fresh cache to read. Best-effort,
    # fail-safe-toward-active. Emits `subscription_state_resolved`
    # observation event (the ONLY observation allowed in lapsed state).
    if state_dir is not None:
        try:
            import subscription_gate  # noqa: E402
            subscription_gate.refresh_subscription_state(
                state_dir,
                harness=detect_harness(),
                append_observation_fn=append_observation,
            )
        except Exception as e:
            emit_stderr(f"subscription_gate refresh failed: {e}")

    # R1494 recovery exception: a server-confirmed rejected bearer would make
    # the entitlement gate return before the historical banner composer could
    # surface its fix. Emit one narrow, rate-limited cue here — before the gate
    # and before install validation, memory, regulation, observations, or MCP
    # dispatch. Confirmed lapse/cancellation remains completely silent.
    auth_recovery_handled = False
    if state_dir is not None:
        try:
            import subscription_gate  # noqa: E402

            if (
                subscription_gate.is_bearer_rejected(state_dir)
                and not subscription_gate.is_terminally_inactive(state_dir)
            ):
                auth_recovery_handled = True
                reservation = (
                    subscription_gate.reserve_bearer_rejected_recovery_cue(
                        state_dir
                    )
                )
                if reservation:
                    try:
                        emit_additional_context(
                            "⚠ Allostat auth stale — the server rejected this "
                            "credential. Fix: "
                            + subscription_gate.bearer_rejected_fix_hint(
                                detect_harness()
                            )
                            + ".",
                            hook_event="SessionStart",
                        )
                    except Exception:
                        subscription_gate.cancel_bearer_rejected_recovery_cue(
                            state_dir,
                            token=reservation,
                        )
                        raise
                    if detect_harness() == "codex":
                        def _finalize_auth_recovery_cue() -> None:
                            subscription_gate.finalize_bearer_rejected_recovery_cue(
                                state_dir,
                                token=reservation,
                            )

                        def _cancel_auth_recovery_cue() -> None:
                            subscription_gate.cancel_bearer_rejected_recovery_cue(
                                state_dir,
                                token=reservation,
                            )

                        run_after_physical_flush(
                            _finalize_auth_recovery_cue,
                            on_failure=_cancel_auth_recovery_cue,
                        )
                    else:
                        subscription_gate.finalize_bearer_rejected_recovery_cue(
                            state_dir,
                            token=reservation,
                        )
        except Exception as e:
            emit_stderr(f"auth recovery cue failed: {e}")

    # S2 Block 3a (2026-05-27) — deactivation guard. When subscription is
    # lapsed/canceled, SessionStart is SILENT per operator's locked directive
    # ("a message would be intrusive — Allostat's presence on their machine
    # after they manually went into their account and unsubscribed"). No
    # banner, no appendix, no scaffolding, no further observations, no
    # MCP dispatch. The subscription_state_resolved observation just
    # emitted above is the only signal of this hook's invocation.
    if not should_inject(state_dir):
        return 0

    # === Active/Trial path — full SessionStart flow continues below ===

    # Install validation runs HERE, not before the gate (moved 2026-07-20).
    #
    # It is not a passive read: on a cache miss `_safe_validate_install()`
    # spawns `claude plugin list` and `claude mcp list`, and the latter PINGS
    # every registered MCP server. Running that above the gate meant a lapsed or
    # cancelled account still shelled out and still generated network traffic on
    # Allostat's behalf — a part of Allostat running unregulated, which is
    # exactly what the operator's directive forbids. Codex flagged it; the S1
    # harness had been hiding it behind ALLOSTAT_SKIP_INSTALL_VALIDATION=1.
    #
    # PATCH-102's force-display contract is unaffected: this still precedes
    # banner composition, and the only path that skips it is the one that emits
    # no banner at all.
    install_status = _safe_validate_install()

    if state_dir is not None:
        try:
            append_observation(
                state_dir,
                "session_start",
                details={"sessionId": payload.get("session_id") or payload.get("sessionId")},
            )
            # Log the validation result so future audits can see history.
            append_observation(
                state_dir,
                "install_validation",
                details=install_status.as_dict(),
            )
        except Exception as e:
            emit_stderr(f"local_state observation failed: {e}")

        # A1 (2026-06-26) — handoff-watchdog SessionStart resume-flush.
        # A hard restart where the Stop hook never fired leaves an overdue
        # rolling handoff unqueued: the turns/tokens counters climbed via
        # UserPromptSubmit but `check_and_set_reminder_state` (Stop-only) never
        # ran to arm the reminder. session_id is stable across a Claude Code
        # restart, so the per-session watchdog state file persists; re-running
        # the check here re-arms an overdue reminder for the next prompt.
        # Mirrors stop.py's guarded watchdog call. Best-effort — never raises.
        if project_root is not None:
            try:
                import handoff_watchdog  # noqa: E402
                resume_session_id = (
                    payload.get("session_id") or payload.get("sessionId")
                )
                resume_result = handoff_watchdog.resume_flush(
                    state_dir, project_root, resume_session_id,
                )
                if resume_result.get("action") not in (None, "no_change", "skipped"):
                    try:
                        append_observation(
                            state_dir,
                            "handoff_watchdog_resume_flush",
                            details=resume_result,
                        )
                    except Exception:
                        pass

                # A3 (2026-06-26) — floor-capture at restart. If resume_flush
                # finds the prior session at the watchdog ceiling with no
                # substantive handoff (a hard restart where Stop never fired and
                # the agent never wrote one), distill a floor handoff from the
                # transcript as continuity insurance. Strict detect-before-write:
                # never overwrites a real handoff, and the file is marked
                # autocaptured so the watchdog still expects a real replacement.
                try:
                    import handoff_autocapture  # noqa: E402
                    ceiling = handoff_watchdog.MAX_ESCALATION_LEVEL
                    at_ceiling = resume_result.get("escalation_level", 0) >= ceiling
                    if at_ceiling and resume_session_id:
                        transcript = (
                            payload.get("transcript_path")
                            or payload.get("transcriptPath")
                        )
                        transcript_path = Path(transcript) if transcript else None
                        floor_path = handoff_autocapture.maybe_write_floor_handoff(
                            transcript_path, project_root, resume_session_id,
                            ending_overdue=True, at_ceiling=at_ceiling,
                            escalation_level=resume_result.get("escalation_level", 0),
                        )
                        if floor_path is not None:
                            try:
                                append_observation(
                                    state_dir,
                                    "handoff_floor_autocaptured",
                                    details={"path": str(floor_path),
                                             "trigger": "session_start_resume_flush"},
                                )
                            except Exception:
                                pass
                except Exception as e:
                    emit_stderr(f"handoff_autocapture floor-write failed: {e}")
            except Exception as e:
                emit_stderr(f"handoff_watchdog resume_flush failed: {e}")

        # Phase 2 (2026-05-28) — silo canonical-text migration.
        # Walks existing <state_dir>/silos/<class>.jsonl files and relocates
        # any residual `resolution.canonical_value` + `fingerprint.raw_excerpt`
        # to the wrapper-local canonical_resolutions.jsonl. Idempotent —
        # writes a marker file after the first successful pass; subsequent
        # sessions short-circuit. Best-effort: any failure logs to
        # `silo_phase2_migration` observation and continues.
        try:
            import canonical_resolutions_migration  # noqa: E402

            report = canonical_resolutions_migration.run(state_dir)
            if report.silos_dir_present and not report.already_migrated:
                append_observation(
                    state_dir,
                    "silo_phase2_migration",
                    details={
                        "files_walked": report.files_walked,
                        "entries_inspected": report.entries_inspected,
                        "entries_migrated": report.entries_migrated,
                        "canonical_bindings_written": report.canonical_bindings_written,
                        "files_rewritten": report.files_rewritten,
                        "errors": report.errors,
                    },
                )
        except Exception as e:
            emit_stderr(f"silo phase2 migration failed: {e}")

    # Pre-release item 55 — loud degradation when innate rules can't load
    # (no vendored *.json + no PyYAML, or rules dir missing). The refusal
    # teeth are wrapper-local (PATCH-143); if they silently vanish, the
    # operator runs unguarded without knowing. Observation + stderr banner.
    _emit_innate_rules_degradation_banner(state_dir)

    # PATCH-105 Defect B fix — banner emission must NEVER silently drop.
    # Wrap _build_banner + emit_additional_context in a fail-safe so any
    # exception in handoff-discovery, tip-selection, ORCHESTRATION_DIRECTIVE
    # concatenation, or anywhere else in the composer still emits at minimum
    # the neutral status line + orchestration directive. The "banner ALWAYS
    # renders" contract is held even when downstream subsystems break.
    # Compute the banner notice (displaces the tip). Two sources, in priority:
    #   1. E8 version-skew — "you upgraded on disk but didn't restart" (F4 class)
    #   2. Leg 3a update-available — "a newer version exists, run the installer"
    # E8 wins: restart what you have before fetching more. Both best-effort.
    notice_line: str | None = None
    plugin_root = Path(__file__).resolve().parent.parent
    try:
        import version_skew  # noqa: E402
        decision = version_skew.evaluate(state_dir, plugin_root, payload)
        if decision is not None:
            notice_line = version_skew.format_banner_line(decision)
            # Validation-honesty (advisor 2026-07-06): a Layer-4 "MCP failed to
            # connect" during a pending-restart (update landed, process stale) is
            # benign — downgrade DEGRADED → pending_restart so the banner tells the
            # user to fully restart instead of crying a false failure. Non-sticky:
            # after the restart there is no skew, so a still-red Layer 4 stays real.
            try:
                from install_validation import apply_update_pending  # noqa: E402
                install_status = apply_update_pending(install_status, decision.skew)
            except Exception as e:
                emit_stderr(f"apply_update_pending failed: {e}")
    except Exception as e:
        emit_stderr(f"version_skew evaluate failed: {e}")
    if notice_line is None:
        try:
            import update_check  # noqa: E402
            status = update_check.evaluate(state_dir, plugin_root)
            if status is not None:
                notice_line = update_check.format_banner_line(status, harness=detect_harness())
        except Exception as e:
            emit_stderr(f"update_check evaluate failed: {e}")

    # Existing-customer privacy-disclosure migration (rem-consent, 2026-07-22).
    # A customer whose consent.json predates the 2026-07-20 copy correction (or a
    # legacy install that never captured consent) has been transmitting a prompt
    # excerpt + each tool call's command/file path under a retired disclosure that
    # wrongly implied the content never left the machine. Surface the corrected,
    # honest notice NON-BLOCKINGLY on the banner (the three dispatch hooks withhold
    # that content via consent.disclosure_ok_to_transmit until this fires). Takes
    # priority over the version/update notice for the one session it is pending.
    # Best-effort: any failure leaves the prior notice untouched.
    _disclosure_migration_pending = False
    try:
        import consent  # noqa: E402
        if consent.disclosure_migration_needed():
            _disclosure_migration_pending = True
            notice_line = consent.disclosure_notice_line()
    except Exception as e:
        emit_stderr(f"disclosure migration check failed: {e}")

    # Proposal A — resolve this project's latest handoff once: the visible notice
    # rides the banner; the body injects separately (non-rendered) below.
    handoff_notice, handoff_body = _resolve_handoff_continuity(
        project_root, profile=profile,
        state_dir=state_dir, provenance=root_provenance,
    )

    # F1 — cheap cached-count read of pending merge proposals (NO heavy
    # detection at session start; that's the Stop hook's job).
    merge_proposal_line = _pending_merge_proposal_line(project_root)

    try:
        banner = _build_banner(
            install_status, state_dir, project_root,
            notice_line=notice_line, handoff_notice=handoff_notice,
            merge_proposal_line=merge_proposal_line,
            include_auth_recovery=not auth_recovery_handled,
        )
    except Exception as e:
        emit_stderr(f"_build_banner failed: {e} — using fail-safe minimal banner")
        banner = _fail_safe_minimal_banner()

    # v0.2.6 PATCH-128 — dispatch BEFORE emit so the server's response
    # additional_context (PATCH-128 body composers + A4 dawn-phase
    # carryover) can be combined with the banner. Pre-PATCH-128 the
    # dispatch ran AFTER emit, meaning server-side body content never
    # reached Claude's context. The dispatch is wrapped in try/except
    # so any failure falls through to banner-only behavior.
    dispatch_response: dict | None = None
    if (
        getattr(install_status, "state", None) == "healthy"
        and project_root is not None
        and state_dir is not None
    ):
        try:
            dispatch_response = _dispatch_server_state_observation(
                state_dir, project_root,
            )
        except Exception as e:
            emit_stderr(f"_dispatch_server_state_observation failed: {e}")

    # Emit the session-start additionalContext blocks. The render-verbatim
    # banner (functional cues only) and the server-side dawn-phase carryover
    # are kept as SEPARATE blocks so the carryover is NEVER governed by the
    # banner's render-VERBATIM instruction (operator 2026-07-12 — pre-fix the
    # code folded them into one string, so the retired verbose banner printed
    # again whenever a cue made the banner non-empty). See
    # _session_start_emission_blocks for the full rationale. The carryover still
    # reaches the agent as silent context; it just never prints.
    #
    # When ALLOSTAT_HPA_QUIET=1, suppress the server-side content at session
    # start (at this hook only hypothalamic-axis fires; §4.4 opt-out mutes it).
    server_text = ""
    if dispatch_response and not _hpa_visible_fire_suppressed():
        server_text = dispatch_response.get("additional_context") or ""

    _notice_emitted = False
    for block in _session_start_emission_blocks(banner, server_text):
        try:
            emit_additional_context(block, hook_event="SessionStart")
        except Exception as e:
            # If even emit_additional_context fails (JSON encode, stdout
            # closed), we can't surface anything. Log to stderr and move on;
            # the hook has done what it can.
            emit_stderr(f"emit_additional_context failed: {e}")
            continue
        if _disclosure_migration_pending and notice_line and notice_line in block:
            _notice_emitted = True

    # Round-7 inversion (advisor 2026-07-23): presentation is UNPROVEN until
    # the notice PHYSICALLY reached stdout. Under Claude, emit_additional_context
    # prints and flushes immediately, so a non-raising emit IS physical output
    # and we record here — same behavior as round 6, now backed by the explicit
    # flush. Under Codex, emit only BUFFERS; the bytes leave the process in
    # flush_pending_context() at exit. Recording here would record a hope, so
    # we register a post-flush callback that _base runs ONLY after a successful
    # physical flush. A failed flush records nothing: migration stays pending,
    # the dispatch hooks keep withholding, and next session retries.
    if _disclosure_migration_pending and _notice_emitted:
        if detect_harness() == "codex":
            run_after_physical_flush(_record_presentation_best_effort)
        else:
            _record_presentation_best_effort()

    # Deconfliction visibility (advisor Q4): when the server-side dawn-phase
    # carryover AND the auto-injected handoff body BOTH fire, log it so the
    # overlap is measurable (not silent). The handoff body header declares
    # itself authoritative so the agent writes one update; this observation
    # tells us how often the structural server-side suppression would matter.
    if server_text and handoff_body and state_dir is not None:
        try:
            append_observation(
                state_dir,
                "continuity_sources_coincident",
                details={"server_chars": len(server_text)},
            )
        except Exception:
            pass

    # Proposal A — inject the lean handoff BODY as a SEPARATE non-rendered
    # additionalContext block (never the render-verbatim banner, so the agent
    # resumes from it without reciting it). The verify-before-claim header is
    # baked into the block; the agent-behavior spec (update-not-recite,
    # activity-vs-outcome) ships in claude_handoff_protocol.md. Best-effort.
    #
    # SHIP-GATE DECONFLICT (advisor Q4): the server-side dawn-phase carryover
    # (dispatch_response.additional_context, concatenated above) is ALSO a
    # "previous session" summary. Before customer ship, decide the precedence —
    # the handoff body is THE continuity source and dawn-phase should defer to
    # or merge into it, so the agent never reconciles two overlapping inputs.
    if handoff_body:
        try:
            emit_additional_context(handoff_body, hook_event="SessionStart")
            if state_dir is not None:
                try:
                    append_observation(
                        state_dir,
                        "handoff_body_injected",
                        details={"byte_count": len(handoff_body)},
                    )
                except Exception:
                    pass
        except Exception as e:
            emit_stderr(f"handoff body inject failed: {e}")

    # PATCH-191 / Phase 3.3a — legacy_aging detector.
    #
    # Producer for the `legacy_aging` observation event that
    # volume-control's 4th detector consumes. Pre-PATCH-191 zero
    # wrapper-side code produced this event; volume-control's 4th branch
    # was structurally unable to fire. rglob the project tree for
    # `_LEGACY_*` files >30d old; emit count via observation. Best-effort.
    #
    # Skips well-known build/cache dirs + caps depth at 8 to bound walk
    # cost. Operator can disable via `ALLOSTAT_LEGACY_AGING_SCAN=0` for
    # large monorepos.
    if state_dir is not None and project_root is not None:
        try:
            import legacy_aging_detector  # noqa: E402
            legacy_aging_detector.detect_and_emit(state_dir, project_root)
        except Exception as e:
            emit_stderr(f"legacy_aging_detector failed: {e}")

    # PATCH-136 / v0.4.0 — tick apoptotic_retirement counters on every
    # session start. Best-effort: any failure is logged and swallowed so
    # banner emission is never affected. Finalizes counter-zero rules.
    try:
        import memory_lifecycle  # noqa: E402
        mem_dir = memory_lifecycle.resolve_memory_dir(project_root)
        if mem_dir is not None:
            tick_result = memory_lifecycle.tick_at_session_start(mem_dir)
            if state_dir is not None and tick_result.get("ticked", 0) > 0:
                try:
                    append_observation(
                        state_dir,
                        "memory_lifecycle_tick",
                        details=tick_result,
                    )
                except Exception:
                    pass
    except Exception as e:
        emit_stderr(f"memory_lifecycle tick failed: {e}")

    # Reliable-memory session-1 fix (2026-08-07): scaffold a brand-new
    # project's memory tree BEFORE the index injects, so the VERY FIRST
    # session already gets the template index — which teaches the agent that
    # Allostat memory exists, where it lives, and how to write to it. The
    # old ordering scaffolded after the inject: session 1 of every new
    # project injected nothing, the agent was never told about the tree,
    # and "remember this" fell through to Claude Code's native memory.
    #
    # Gated on conservative adoption (scaffolder.is_adoptable_project_root):
    # quiet auto-registration must never plant trees in $HOME, temp dirs,
    # drive roots, or the harness namespace (the 2026-08-03 orphan class).
    # Refusal is observable, never silent. NEVER overwrites existing files.
    if project_root is not None:
        try:
            import scaffolder  # noqa: E402
            if scaffolder.is_adoptable_project_root(project_root):
                tier4_actions: list = []
                expected_mem_dir = scaffolder.compute_memory_dir_path(project_root)
                if not expected_mem_dir.is_dir():
                    tier4_actions.extend(
                        scaffolder.scaffold_project_memory_tree(project_root)
                    )
                tier4_actions.extend(
                    scaffolder.scaffold_project_sandbox(project_root)
                )
                if state_dir is not None and tier4_actions:
                    try:
                        append_observation(state_dir, "project_scaffolder_run", details={
                            "project_root": str(project_root),
                            "actions_count": len(tier4_actions),
                            "tier4_created": sum(1 for a in tier4_actions if a.kind == "tier4_created"),
                            "tier3_created": sum(1 for a in tier4_actions if a.kind == "tier3_created"),
                        })
                    except Exception:
                        pass
            elif state_dir is not None:
                try:
                    append_observation(state_dir, "memory_scaffold_refused", details={
                        "project_root": str(project_root),
                        "reason": "unadoptable_root (home/temp/drive-root/harness-namespace)",
                    })
                except Exception:
                    pass
        except Exception as e:
            emit_stderr(f"project scaffolder failed: {e}")

    # Root memory first: migration can copy a previously unseen harness leaf,
    # and that leaf must be reachable in this same session, not one later.
    _root_project_memory(project_root, state_dir)

    # A committed leaf is authoritative. Finish an interrupted index update
    # before ordinary completeness reconciliation or injection.
    _recover_topical_memory(project_root, state_dir)

    # Index completeness is mechanical after migration. Any live leaf not named
    # by MEMORY.md is appended before anything reads or injects the index.
    _reconcile_memory_index(project_root, state_dir)

    # Phase 1 (project-rooted memory): inject the project-dir MEMORY.md index
    # so memory loads from <project>/memory/ (portable, not C-drive-bound).
    # Best-effort.
    _inject_project_memory(project_root, state_dir, profile=profile)
    _inject_learned_rules(state_dir)

    # PATCH-A / Tier 1 scaffolding — first-run install scaffolder.
    # Runs Bucket B template writes on first install (gated on
    # ~/.allostat/.first_run_complete marker — never overwrites existing
    # files). Bucket A inject deprecated 2026-05-24 — Bucket A files now
    # ship via data/appendix_seed/ and load topic-conditional via
    # appendix_system. Best-effort; failures don't break banner.
    try:
        import scaffolder  # noqa: E402
        scaffold_result = scaffolder.run_scaffolder()
        # Bucket A injection deprecated; bucket_a_content is now always
        # "" (kept on dataclass for API compat). Skip the emit entirely.
        if state_dir is not None and scaffold_result.actions:
            try:
                append_observation(state_dir, "scaffolder_run", details={
                    "first_run": scaffold_result.first_run,
                    "actions_count": len(scaffold_result.actions),
                    "created": sum(1 for a in scaffold_result.actions if a.kind == "bucket_b_created"),
                    "skipped": sum(1 for a in scaffold_result.actions if a.kind == "bucket_b_skipped_existing"),
                })
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"scaffolder failed: {e}")

    # (PATCH-D / Tier 4 per-project memory tree + sandbox scaffolder now runs
    # ABOVE, before _root_project_memory + _inject_project_memory — the
    # reliable-memory session-1 fix, 2026-08-07 — behind the conservative
    # adoption gate. A brand-new project's first session injects the fresh
    # template index instead of injecting nothing.)

    # PATCH-MIGRATE — verify scaffolding completed + write migration marker.
    # Idempotent via ~/.allostat/.migration_complete marker. Distinct from
    # first_run_complete marker so the two phases reason separately.
    # Best-effort; failures logged but don't break banner.
    try:
        import migrate  # noqa: E402
        migration_result = migrate.run_migration()
        if state_dir is not None and not migration_result.already_complete:
            try:
                append_observation(state_dir, "migration_run", details={
                    "verification_passed": migration_result.verification_passed,
                    "actions_count": len(migration_result.actions),
                    "failures_count": len(migration_result.failures),
                })
            except Exception:
                pass
    except Exception as e:
        emit_stderr(f"migrate failed: {e}")

    # PATCH-138 / v0.6.0 Phase 2 Slice 3 — conditional content loading.
    # Resolves Tier 2/3 includes from cwd + first prompt; reads matched
    # files client-side; injects into additionalContext. Best-effort.
    try:
        import conditional_loader  # noqa: E402
        first_prompt = ""  # SessionStart has no first prompt yet; cwd-only resolution
        resolution = conditional_loader.resolve_tier_includes(
            cwd=str(project_root) if project_root else "",
            first_prompt=first_prompt,
        )
        if resolution.tier2_files or resolution.tier3_files:
            tier_content = conditional_loader.render_tier_includes(resolution)
            if tier_content.strip():
                emit_additional_context(tier_content, hook_event="SessionStart")
            if state_dir is not None:
                try:
                    append_observation(state_dir, "tier_loaded", details={
                        "matched_project": resolution.matched_project,
                        "trigger_source": resolution.trigger_source,
                        "tier2_count": len(resolution.tier2_files),
                        "tier3_count": len(resolution.tier3_files),
                    })
                except Exception:
                    pass
    except Exception as e:
        emit_stderr(f"conditional_loader failed: {e}")

    # PATCH-138 / v0.6.0 Phase 2 Slice 4 — appendix seed bootstrap on SessionStart.
    # Copies missing seed files into operator's per-project appendix folder.
    # Idempotent: operator edits preserved across runs.
    cwd_matched_project: str | None = None
    try:
        import appendix_system  # noqa: E402
        import conditional_loader  # noqa: E402
        cwd_resolution = conditional_loader.resolve_tier_includes(
            cwd=str(project_root) if project_root else "",
            first_prompt="",
        )
        if cwd_resolution.matched_project:
            cwd_matched_project = cwd_resolution.matched_project
            # plugin_root: walk up from this hook file to the plugin install root
            plugin_root = Path(__file__).resolve().parent.parent
            copied, seed_errors = appendix_system.seed_default_appendices(
                home=Path.home(),
                project_name=cwd_resolution.matched_project,
                plugin_root=plugin_root,
            )
            if copied and state_dir is not None:
                try:
                    append_observation(state_dir, "appendix_seeded", details={
                        "project": cwd_resolution.matched_project,
                        "files_copied": [p.name for p in copied],
                    })
                except Exception:
                    pass
            # H3 audit fix (S1 closure 2026-05-26): surface seeder OSError
            # failures so /allostat-handoff-status can count them. Previously
            # silent — always-on architecture files could be invisibly absent.
            if seed_errors and state_dir is not None:
                try:
                    append_observation(state_dir, "appendix_seed_failed", details={
                        "project": cwd_resolution.matched_project,
                        "errors": [
                            {"file": name, "error_class": err}
                            for name, err in seed_errors
                        ],
                    })
                except Exception:
                    pass
    except Exception as e:
        # H3 audit fix (S1 closure 2026-05-26): the exception path here
        # silently disables Bucket A always-on injection by leaving
        # cwd_matched_project = None. Emit an observation so the failure
        # surfaces in /allostat-handoff-status counts instead of going
        # invisible. Exactly the Phase 3.1 regression Fix Now A1 repaired.
        emit_stderr(f"appendix seeder failed: {e}")
        if state_dir is not None:
            try:
                append_observation(state_dir, "appendix_resolution_failed", details={
                    "error_class": type(e).__name__,
                    "error_message": str(e)[:200],
                })
            except Exception:
                pass

    # Ship-2 follow-on (2026-05-24) — Universal always-on injection.
    #
    # Files with `universal: true` frontmatter in data/appendix_seed/ inject
    # on EVERY SessionStart regardless of cwd. The handoff protocol is the
    # canonical example: session continuity is universal, not Allostat-
    # specific. Any project where the wrapper is installed benefits from
    # rolling-handoff cadence + watchdog enforcement, so all need the
    # protocol spec injected.
    #
    # Read directly from the bundle path (no per-project seeding). Operator-
    # edit isn't expected for these spec files — they're the protocol itself.
    #
    # Codex adapter (slice 2): the coarse skip-all gate is replaced by the
    # profile's granular `always_on_exclude` filter. Codex now injects the
    # handoff-authoring protocol (slice 2 wired the write loop behind it)
    # but still mutes the livetime/clock protocol. Claude's exclude set is
    # empty, so its behavior stays byte-for-byte unchanged.
    try:
        import appendix_system  # noqa: E402
        plugin_root = Path(__file__).resolve().parent.parent
        universal_files = _filter_appendix_exclude(
            appendix_system.load_universal_appendix_from_bundle(plugin_root),
            profile,
        )
        if universal_files:
            universal_content = appendix_system.render_appendix_content(
                universal_files,
            )
            if universal_content.strip():
                emit_additional_context(universal_content, hook_event="SessionStart")
            if state_dir is not None:
                try:
                    append_observation(state_dir, "universal_appendix_loaded", details={
                        "files_injected": [f.path.name for f in universal_files],
                        "byte_count": len(universal_content),
                    })
                except Exception:
                    pass
    except Exception as e:
        emit_stderr(f"universal appendix inject failed: {e}")

    # v1.4.28 (2026-05-26) — Canonical handoff_path injection.
    #
    # Closes the protocol-spec gap where Claude had to infer cwd-sanitization
    # to compute the handoff write path. Diagnostic 2026-05-26 P1 found Claude
    # writing to divergent cwd-sanitization trees (and to legacy timestamp
    # filenames pattern-matched off operator chat references) because the
    # protocol said `<cwd>` without specifying which sanitization. Result:
    # watchdog looked at one path, Claude wrote to another, reminders fired
    # forever.
    #
    # Fix: wrapper computes the canonical path here (via the same
    # `session_handoff.sanitize_cwd_for_harness` the watchdog uses) and
    # surfaces it to Claude via additionalContext as `handoff_path`. Claude's
    # protocol then prescribes "write to the provided handoff_path"; no
    # inference required, no divergence possible.
    if project_root is not None:
        session_id_for_path = (
            payload.get("session_id") or payload.get("sessionId") or ""
        )
        if session_id_for_path:
            try:
                import session_handoff  # noqa: E402
                canonical_dir = session_handoff.resolve_canonical_handoff_dir(
                    project_root,
                )
                handoff_path = canonical_dir / f"{session_id_for_path}.md"
                marker = (
                    "\n\n=== Allostat handoff path (authoritative) ===\n\n"
                    "Your rolling handoff for this session must be written to "
                    "this exact path (do NOT infer from cwd, do NOT pattern-match "
                    "from prior session filenames):\n\n"
                    f"  `{handoff_path}`\n\n"
                    "Overwrite in place per the protocol. One file per session, "
                    "keyed on YOUR sessionId.\n"
                )
                emit_additional_context(marker, hook_event="SessionStart")
                if state_dir is not None:
                    try:
                        append_observation(state_dir, "handoff_path_injected", details={
                            "handoff_path": str(handoff_path),
                            "session_id": session_id_for_path,
                        })
                    except Exception:
                        pass
            except Exception as e:
                emit_stderr(f"handoff_path inject failed: {e}")

    # A1 REVERT (2026-05-24) — Bucket A always-on injection.
    #
    # Restores the always-on injection of memory architecture files
    # (memory_architecture.md, allostat_architecture.md, artifact_taxonomy.md,
    # canon_size_discipline.md, plus A3's claude_handoff_protocol.md).
    # Phase 3.1 Bucket A dedup (v1.4.21) moved these to topic-conditional
    # via `appendix_system.match_topics`; operator + advisor diagnosed that
    # as a regression — architecture context silently absent from most
    # sessions because predictions miss. Always-on is the restored design.
    #
    # cwd-scoped: only fires when the cwd matches an Allostat-managed
    # project (per PROJECT_REGISTRY in conditional_loader). Non-Allostat
    # cwds (scratch folders, experimental work) don't pay the ~17KB token
    # cost where there's zero recall value.
    #
    # Files are read from operator-side appendix folder (already seeded
    # above by `seed_default_appendices`). Edits operator made to those
    # files are preserved per `feedback_migration_never_destroys_existing_files.md`.
    # Codex adapter (slice 2): the coarse skip-all gate is replaced by the
    # profile's granular `always_on_exclude` filter (see HarnessProfile.
    # always_on_exclude docstring in _base.py). Claude's exclude set is
    # empty, so its behavior stays byte-for-byte unchanged.
    if cwd_matched_project:
        try:
            import appendix_system  # noqa: E402
            always_on_files = _filter_appendix_exclude(
                appendix_system.load_always_on_appendix_files(
                    Path.home(), cwd_matched_project,
                ),
                profile,
            )
            if always_on_files:
                always_on_content = appendix_system.render_appendix_content(
                    always_on_files,
                )
                if always_on_content.strip():
                    emit_additional_context(always_on_content, hook_event="SessionStart")
                if state_dir is not None:
                    try:
                        append_observation(state_dir, "always_on_appendix_loaded", details={
                            "project": cwd_matched_project,
                            "files_injected": [f.path.name for f in always_on_files],
                            "byte_count": len(always_on_content),
                        })
                    except Exception:
                        pass
        except Exception as e:
            emit_stderr(f"always_on appendix inject failed: {e}")

    return 0


# Codex wall-of-text fix (2026-07-05): the handoff is DATA, not display text.
# Everything the hook buffered rides inside this envelope; the opening
# contract rides after it and owns the first visible reply's shape.
_CODEX_INTERNAL_OPEN = (
    "<allostat_internal_context>\n"
    "Everything inside this tag is PRIVATE working context — prior-session "
    "handoff, memory index, protocols, functional cue lines. Load it and use "
    "it; treat its claims as unverified until checked. Do NOT quote, dump, or "
    "recite it to the user unless they explicitly ask.\n"
)
_CODEX_INTERNAL_CLOSE = "</allostat_internal_context>"
_CODEX_OPENING_CONTRACT = (
    "<allostat_opening_contract>\n"
    "On your FIRST reply this session: acknowledge continuity in one short "
    "sentence; if a prior-session handoff loaded above, summarize its work in "
    "at most 3 brief bullets; surface any ⚠/📋 cue lines from the internal "
    "context as one short line each; then respond to the user's actual "
    "message. Never print the raw contents of allostat_internal_context.\n"
    "</allostat_opening_contract>"
)


if __name__ == "__main__":
    # Deterministic flush on any return path (Important finding fix):
    # flush_pending_context previously ran ONLY via the atexit hook
    # registered in _base.emit_additional_context. If this process exits
    # abnormally (os._exit, SIGTERM), atexit callbacks never run and the
    # buffered Codex additionalContext is silently lost. Flushing here in
    # a `finally` around main() guarantees the flush on normal completion
    # regardless of exit path, while the atexit registration in _base
    # remains as the ultimate fallback for the abnormal-exit case this
    # finally block cannot reach (e.g. os._exit called from within main()).
    #
    # Safe under Claude: flush_pending_context is a no-op when the buffer
    # is empty (the Claude path never buffers), so this call is harmless
    # there. Safe under Codex: flush_pending_context clears the buffer as
    # it emits, so the later atexit-registered call finds an empty buffer
    # and no-ops — no double emission.
    try:
        _rc = main()
    finally:
        try:
            # Codex: bracket all buffered payloads in the private envelope,
            # then append the opening contract OUTSIDE it — the one visible-
            # behavior instruction the agent receives. No-ops under Claude.
            if detect_harness() == "codex":
                wrap_pending_context(_CODEX_INTERNAL_OPEN, _CODEX_INTERNAL_CLOSE)
                emit_additional_context(
                    _CODEX_OPENING_CONTRACT, hook_event="SessionStart",
                )
            if not flush_pending_context("SessionStart"):
                # Round-7: the flush result is the only failure signal we
                # have — do not swallow it. The buffer stays populated so the
                # atexit fallback retries; presentation was never recorded
                # (the post-flush callback only runs on success).
                emit_stderr("SessionStart flush failed; disclosure presentation NOT recorded")
        except Exception as e:  # a hook must never crash the session
            try:
                emit_stderr(f"SessionStart flush crashed: {e}")
            except Exception:
                pass
    sys.exit(_rc)
