"""Proposal B — live-time line for per-turn injection.

The UserPromptSubmit hook injects a fresh `now:` line every turn so the agent
always has a current clock. Without it, the agent anchors on the SessionStart
date (which never refreshes) and confabulates time-of-day on multi-day sessions
— the operator-reported "starts telling me to go to bed" drift.

`format_now_line` is a pure function (datetime in → line out) so it is fully
deterministic under test. The hook supplies `datetime.now().astimezone()` (an
aware local datetime) so the operator's local wall-clock offset is visible and
the agent can reason about local time-of-day.
"""
from __future__ import annotations

from datetime import datetime


def format_now_line(now: datetime) -> str:
    """Return the single-line `now:` injection for the given datetime.

    Format: ``now: 2026-06-19T13:34-05:00 (Friday)``

    The ISO timestamp (minute precision; an aware datetime carries its UTC
    offset) plus the day-of-week. Minute precision keeps it stable within a
    turn; the offset lets the agent reason about local time-of-day.
    """
    return f"now: {now.isoformat(timespec='minutes')} ({now.strftime('%A')})"
