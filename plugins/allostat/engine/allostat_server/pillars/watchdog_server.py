"""Watchdog-server pillar — stateless handoff watchdog evaluator (S1 2026-05-25).

⚠️ INTENTIONAL DEFERRED PILLAR — DO NOT CUT AS DEAD/ABANDONED (operator decision
2026-06-05, pillar-wiring run). This pillar is fully built, unit-tested, and
MCP-registered (`watchdog_evaluate` via pillars/__init__.py) but is deliberately
NOT dispatched yet — the wrapper still runs the handoff watchdog 100% locally
(`lib/handoff_watchdog.py`). A code audit will see "registered tool, zero
dispatch callers" and may flag it dead — it is NOT. Status = parked, by design.
Planned: move the handoff watchdog server-side here so it can be subscription-
gated. The gating semantics need careful design first — handoff nagging is
PROTECTIVE (it prevents the context-bloat snowball for a lapsed customer), so it
must not be bluntly paywalled off for non-subscribers. Until that design lands,
this stays dormant-on-purpose and the wrapper-local watchdog is the live path.

Originally `lib/handoff_watchdog.py` on the wrapper computed threshold trips,
escalation level, and reminder text. S1 moves the decision logic server-side
so it can be subscription-gated. CRITICAL: the server side is STATELESS per
call — the RAM-only privacy invariant (LOCKED 2026-05-13, Layer 5 supplement
§1 third bullet) forbids per-user state caches on the server. The wrapper
round-trips its local watchdog state through the server on each call.

Wire protocol (no operator content crosses):
  Input — wrapper sends per UserPromptSubmit OR Stop:
    excerpt.observations: rolling window of recent observations (counters)
    question.details:
      session_id:                  str (the active session's id, UUID)
      prior_state:                 dict containing the wrapper's persisted
                                   counters from the prior call:
                                     turns_at_last_write
                                     context_size_at_last_write
                                     current_escalation_level
                                     anti_pattern_flag_count
      turn_count_since_last_write: int
      context_size_estimate:       int (per-turn occupancy)
      last_handoff_write_ts:       str | None (ISO; None = never written)
      anti_pattern_flag:           bool (wrapper's local _passes_anti_pattern_check)

  Output — server returns:
    decision.action: "watchdog_evaluation"
    decision.details:
      escalation_level:        0 | 1 | 2 | 3
      should_emit_reminder:    bool
      reminder_text:           str | None  (server-templated boilerplate)
      reset_after_substantive: bool
      subscription_state:      str (active|trial|lapsed|canceled)
      new_state:               dict — wrapper persists this verbatim for the
                               next round-trip:
                                 turns_at_last_write
                                 context_size_at_last_write
                                 current_escalation_level
                                 anti_pattern_flag_count

Reminder text source (per advisor's final-pass §1): templated server-side
USING the existing wrapper-side reminder strings as templates. Same phrasing
Claude already responds to, just sourced from server. The text strings live
in this module and MUST match the wrapper's existing strings.

Subscription gate: read users.subscription_state. If not in {trial, active},
return escalation_level=0 + should_emit_reminder=False. Memory continues
to accumulate locally regardless of state per privacy moat; we just stop
*regulating* against it.

RAM-only invariant: this module reads ONLY users.subscription_state (an
operational telemetry column that already exists). It writes NO per-user
or per-session state to the DB. Verified by
tests/test_ram_only_invariant.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from ..runtime_context import local_execution

from ..pillar_protocol import (
    DecisionPayload,
    coerce_int,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

logger = logging.getLogger(__name__)

PILLAR_ID = "watchdog-server"


# ============================================================================
# Thresholds — match wrapper defaults; configurable via server env vars
# ============================================================================

_DEFAULT_TURNS_THRESHOLD = 5
_DEFAULT_TOKENS_THRESHOLD = 50_000

_MAX_ESCALATION_LEVEL = 3


def _turns_threshold() -> int:
    import os
    raw = os.environ.get("ALLOSTAT_WATCHDOG_SERVER_TURNS", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_TURNS_THRESHOLD
        return v if v > 0 else _DEFAULT_TURNS_THRESHOLD
    except (ValueError, AttributeError):
        return _DEFAULT_TURNS_THRESHOLD


def _tokens_threshold() -> int:
    import os
    raw = os.environ.get("ALLOSTAT_WATCHDOG_SERVER_TOKENS", "")
    try:
        v = int(raw.strip()) if raw.strip() else _DEFAULT_TOKENS_THRESHOLD
        return v if v > 0 else _DEFAULT_TOKENS_THRESHOLD
    except (ValueError, AttributeError):
        return _DEFAULT_TOKENS_THRESHOLD


# ============================================================================
# Reminder text — server-templated. Copy of wrapper-side strings for parity.
# ============================================================================

_REMINDER_LEVEL_1 = (
    "[Allostat handoff watchdog — protocol reminder]\n\n"
    "Your rolling handoff at `{handoff_path}`\n"
    "is overdue per the MCS protocol. Write or update it now before continuing.\n\n"
    "Cadence reminder: every 3-5 turns of substantive work, plus metabolism pulses\n"
    "and idle. See `claude_handoff_protocol.md` in your additionalContext for full spec.\n"
)

_REMINDER_LEVEL_2 = (
    "[Allostat handoff watchdog — PROTOCOL VIOLATION level 2]\n\n"
    "Two threshold trips since last substantive handoff write. The MCS protocol\n"
    "needs you to write or update `{handoff_path}`\n"
    "NOW, before the next operator response. Continuity is breaking down.\n\n"
    "If you've been writing handoffs that don't pass the anti-pattern check\n"
    "(empty sections, TODO-only stubs, under 200 bytes), those don't count.\n"
    "Substantive means: real Focus, real Decisions, real Memory pointers.\n"
)

_REMINDER_LEVEL_3 = (
    "[Allostat handoff watchdog — level 3 STUB INJECTION]\n\n"
    "You have not written a substantive handoff in too many turns. Per MCS\n"
    "protocol, paste this stub at `{handoff_path}`\n"
    "and fill it in BEFORE responding to the operator's next prompt.\n\n"
    "```markdown\n"
    "# <session_id>\n\n"
    "_Rolling Claude-authored handoff. Last update: <ISO timestamp UTC>._\n\n"
    "## Focus\n"
    "TODO: <one paragraph or 2-3 bullets — what is this session about?>\n\n"
    "## Decisions\n"
    "TODO: <locked calls made this session, numbered>\n\n"
    "## Memory pointers\n"
    "TODO: <paths future-you should read to resume>\n\n"
    "## Open threads\n"
    "TODO: <work in progress>\n\n"
    "## Blocked\n"
    "(none)\n\n"
    "## Queued\n"
    "(none)\n"
    "```\n\n"
    "Replace each TODO with real content. Substance, not contrition.\n"
)


#: The server cannot name an absolute handoff path and must not try. The
#: watchdog's privacy invariant is wrapper-side-only state with no new server
#: payload, and shipping operator paths off the machine would break it. What it
#: CAN do is stop contradicting the client: until 2026-08-04 these templates
#: named a cwd-derived path inside the harness's own projects namespace, while
#: SessionStart had already surfaced the real project-rooted one. Sessions got
#: two destinations and handoffs landed in the harness tree.
#: Deferring to the hook's own value is always correct and never contradicts it.
_HANDOFF_PATH_DEFERRAL = "the exact handoff path the SessionStart hook surfaced"


def _reminder_for_level(level: int) -> str | None:
    if level <= 0:
        return None
    if level == 1:
        text = _REMINDER_LEVEL_1
    elif level == 2:
        text = _REMINDER_LEVEL_2
    else:
        text = _REMINDER_LEVEL_3
    return text.replace("{handoff_path}", _HANDOFF_PATH_DEFERRAL)


# ============================================================================
# Subscription gate (reads users.subscription_state operational column)
# ============================================================================

_ACTIVE_STATES: frozenset[str] = frozenset({"trial", "active"})

# Injectable DB connection factory. main.build_app() threads its
# db_path-bound factory here so the subscription gate reads the SAME DB
# as the middleware stack — db.connect()'s default path is wrong when the
# app was built with an explicit db_path (would fail open to "active").
# None → fall back to db.connect() on the default path (CLI / pillar-only
# test use).
_connection_factory: Callable[[], Any] | None = None


def set_connection_factory(factory: Callable[[], Any] | None) -> None:
    """Thread the app's connection factory into this pillar (main.build_app)."""
    global _connection_factory
    _connection_factory = factory


def _subscription_state_for_user(user_id: str | None) -> str:
    """Look up subscription state from the users table. Fail-open to
    'active' on any error so a paying user doesn't accidentally lose
    service if the DB read fails.
    """
    if local_execution.get() or not user_id:
        return "active"
    try:
        from .. import db
        conn = _connection_factory() if _connection_factory is not None else db.connect()
        try:
            return db.lookup_subscription_state(conn, user_id)
        finally:
            conn.close()
    except Exception as e:
        # Fail-open to "active" is OPERATOR-LOCKED (2026-05-27 directive:
        # unreachable != bypass) — never change the return value here.
        # M5 (fixpass 2026-07-01), observability half only: log the error
        # class plus the FIRST LINE of its message so operators can tell
        # auth/permission/corruption failures apart from transient ones.
        # Driver error messages carry no SQL row content; capping to one
        # line keeps any multi-line context out of the log regardless.
        msg = str(e)
        first_line = msg.splitlines()[0][:200] if msg else ""
        logger.warning(
            "watchdog_server subscription_state lookup failed (fail-open active): %s: %s",
            type(e).__name__,
            first_line,
        )
        return "active"


# ============================================================================
# Stateless decision logic — prior_state in, new_state + decision out
# ============================================================================


_BASELINE_STATE: dict[str, int] = {
    "turns_at_last_write": 0,
    "context_size_at_last_write": 0,
    "current_escalation_level": 0,
    "anti_pattern_flag_count": 0,
}


def _coerce_prior_state(raw: Any) -> dict[str, int]:
    """Sanitize the wrapper-provided prior_state. Missing/invalid fields
    default to baseline. Defensive against schema-drift wrapper versions."""
    if not isinstance(raw, dict):
        return dict(_BASELINE_STATE)
    out: dict[str, int] = {}
    for key, default in _BASELINE_STATE.items():
        v = raw.get(key, default)
        try:
            out[key] = int(v) if v is not None else default
        except (TypeError, ValueError):
            out[key] = default
    return out


def _evaluate(
    *,
    user_id: str | None,
    session_id: str,
    prior_state: dict[str, int],
    turn_count_since_last_write: int,
    context_size_estimate: int,
    last_handoff_write_ts: str | None,
    anti_pattern_flag: bool,
) -> dict[str, Any]:
    """Pure decision function: prior_state + current counters → decision +
    new_state. NO DB writes for per-session state; subscription gate is
    the only DB read.
    """
    # Subscription gate.
    sub_state = _subscription_state_for_user(user_id)
    if sub_state not in _ACTIVE_STATES:
        return {
            "escalation_level": 0,
            "should_emit_reminder": False,
            "reminder_text": None,
            "reset_after_substantive": False,
            "subscription_state": sub_state,
            "new_state": dict(prior_state),
        }

    prior_escalation = prior_state["current_escalation_level"]

    # Substantive-write reset detection.
    reset_after_substantive = (
        turn_count_since_last_write == 0
        and not anti_pattern_flag
        and last_handoff_write_ts is not None
    )
    if reset_after_substantive:
        new_state = {
            "turns_at_last_write": 0,
            "context_size_at_last_write": context_size_estimate,
            "current_escalation_level": 0,
            "anti_pattern_flag_count": prior_state["anti_pattern_flag_count"],
        }
        return {
            "escalation_level": 0,
            "should_emit_reminder": False,
            "reminder_text": None,
            "reset_after_substantive": True,
            "subscription_state": sub_state,
            "new_state": new_state,
        }

    # Threshold check.
    over_turn = turn_count_since_last_write >= _turns_threshold()
    token_delta = max(0, context_size_estimate - prior_state["context_size_at_last_write"])
    over_tokens = token_delta >= _tokens_threshold()
    threshold_tripped = over_turn or over_tokens

    new_anti_pattern_count = (
        prior_state["anti_pattern_flag_count"] + (1 if anti_pattern_flag else 0)
    )

    if not threshold_tripped:
        new_state = {
            "turns_at_last_write": prior_state["turns_at_last_write"],
            "context_size_at_last_write": prior_state["context_size_at_last_write"],
            "current_escalation_level": prior_escalation,
            "anti_pattern_flag_count": new_anti_pattern_count,
        }
        return {
            "escalation_level": prior_escalation,
            "should_emit_reminder": False,
            "reminder_text": None,
            "reset_after_substantive": False,
            "subscription_state": sub_state,
            "new_state": new_state,
        }

    # Threshold tripped — escalate one level (capped at MAX).
    new_escalation = min(prior_escalation + 1, _MAX_ESCALATION_LEVEL)
    new_state = {
        "turns_at_last_write": prior_state["turns_at_last_write"],
        "context_size_at_last_write": context_size_estimate,
        "current_escalation_level": new_escalation,
        "anti_pattern_flag_count": new_anti_pattern_count,
    }
    return {
        "escalation_level": new_escalation,
        "should_emit_reminder": True,
        "reminder_text": _reminder_for_level(new_escalation),
        "reset_after_substantive": False,
        "subscription_state": sub_state,
        "new_state": new_state,
    }


def watchdog_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """MCP entry point for watchdog_evaluate. Stateless per call."""
    record_pillar_invocation(PILLAR_ID)
    pillar_token = current_pillar_id.set(PILLAR_ID)
    try:
        user_id = None
        if not local_execution.get():
            try:
                from .. import auth
                current = auth.current_auth.get()
                user_id = current.user_id if current else None
            except Exception:
                user_id = None

        details = question.details or {}
        session_id = str(details.get("session_id", ""))
        if not session_id:
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="no_op",
                    details={"reason": "missing session_id"},
                ),
                stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="no_op"),
            )

        prior_state = _coerce_prior_state(details.get("prior_state"))

        result = _evaluate(
            user_id=user_id,
            session_id=session_id,
            prior_state=prior_state,
            turn_count_since_last_write=coerce_int(details.get("turn_count_since_last_write"), 0),
            context_size_estimate=coerce_int(details.get("context_size_estimate"), 0),
            last_handoff_write_ts=details.get("last_handoff_write_ts"),
            anti_pattern_flag=bool(details.get("anti_pattern_flag", False)),
        )
        decision_class = "fire" if result["should_emit_reminder"] else "no_op"
        return PillarToolResponse(
            decision=DecisionPayload(
                action="watchdog_evaluation",
                details=result,
            ),
            stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
        )
    finally:
        current_pillar_id.reset(pillar_token)


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="watchdog_evaluate",
        description=(
            "Handoff watchdog (server-side, STATELESS per call). "
            "Inputs: session_id, prior_state (wrapper-persisted counters), "
            "turn_count_since_last_write, context_size_estimate, "
            "last_handoff_write_ts, anti_pattern_flag. Returns "
            "escalation_level + should_emit_reminder + reminder_text + "
            "new_state (wrapper persists for next round-trip). Subscription "
            "gate: inactive subs receive escalation=0."
        ),
    )
    async def watchdog_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        # #4 hardening (2026-07-04): watchdog_decision performs a synchronous
        # DB round-trip (_subscription_state_for_user). Under Postgres that's
        # a real network call; run it in a worker thread so it never blocks the
        # asyncio event loop — matching every other DB call site
        # (PATCH-197/AUDIT-S29). FastMCP awaits async tool fns, so this is safe.
        return await asyncio.to_thread(watchdog_decision, excerpt, question)
