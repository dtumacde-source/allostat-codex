"""Server-side caught-exception observability (ITEM D / Phase 7, 2026-05-28).

The server is RAM-only — it does NOT persist operator content and does NOT
write the wrapper-style `observations.jsonl`. So "observe" here means: emit a
structured log line (stdout -> journald) so a swallowed exception stops being
invisible. This closes the observability gap where consequential catch sites
(subscription-gate DB failure, per-pillar invocation crash) silently degraded
with no log, no metric, no signal.

Deliberately dumb (per advisor signoff): no persistence, no operator content,
just `<component>_<event>_error` + error_class/request_id/context to the log
stream. Exception messages are deliberately excluded because they can quote
operator-derived input.

Privacy: callers pass only non-operator-content context (e.g. user_id, pillar
name) — never prompt/correction text. The structured log already excludes
request bodies by contract (see logging.py).
"""
from __future__ import annotations

from typing import Any

from ._time import utc_now_iso as _utc_now_iso
from .runtime_context import _emit_log_line, current_request_id


def observe_caught_exception(
    component: str,
    event_name: str,
    exc: BaseException,
    **context: Any,
) -> None:
    """Emit a `<component>_<event_name>_error` structured log line for a
    caught, otherwise-swallowed exception.

    Best-effort: this never raises. An observability helper must not become a
    new failure source at the very catch site it's meant to make visible.
    """
    try:
        payload: dict[str, Any] = {
            "event": f"{component}_{event_name}_error",
            "ts": _utc_now_iso(),
            "error_class": type(exc).__name__,
            "request_id": current_request_id.get(),
        }
        if context:
            payload["context"] = context
        _emit_log_line(payload)
    except Exception:
        # Never break the caller from inside the observability path.
        pass
