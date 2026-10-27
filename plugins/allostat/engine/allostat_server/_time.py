"""Shared ISO-8601 UTC time helpers — single source of truth.

Pre-release item 25c (2026-06-10): the server grew three identical
`_parse_iso` copies (hypothalamic_axis / stress_response / metabolism)
and five identical `utc_now_iso` copies (db / logging / dispatch_tools /
pattern_observer / recall_silos). This module deduplicates them; each
prior definition site now imports from here (aliased to keep local
call-site names unchanged).
"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 string (e.g. 2026-06-10T12:00:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO-8601 (Z or offset) → tz-aware datetime. Returns None on failure."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None
