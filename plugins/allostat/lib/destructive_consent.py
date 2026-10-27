"""Deprecated local destructive-consent surface.

Same-user executors can modify every project-local record and obtain every
same-user key. Until the hook platform supplies a trusted writer/capability,
this compatibility module never creates, arms, consumes, or restores consent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA = "allostat.destructive-consent/unsupported-v3"
STATE_DIRNAME = "destructive_consent_v2"
DEFAULT_TTL = 0.0


def action_digest(action: dict[str, Any]) -> str:
    raw = json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_pending(
    state_dir: Any, action: dict[str, Any], *, session_id: str, turn_id: str,
    action_class: str = "other_destructive", target_count: int = 0,
    now: float | None = None, ttl: float = DEFAULT_TTL,
) -> None:
    del state_dir, action, session_id, turn_id, action_class, target_count, now, ttl
    return None


def arm_from_prompt(
    state_dir: Any, prompt: str, *, session_id: str, turn_id: str,
    now: float | None = None,
) -> bool:
    del state_dir, prompt, session_id, turn_id, now
    return False


def consume_matching(
    state_dir: Any, action: dict[str, Any], *, session_id: str, turn_id: str,
    now: float | None = None,
) -> None:
    del state_dir, action, session_id, turn_id, now
    return None


def restore(state_dir: Any, token: dict[str, Any], *, now: float | None = None) -> bool:
    del state_dir, token, now
    return False


def refusal_message(record: dict[str, Any] | None = None) -> str:
    del record
    return (
        "DESTRUCTIVE ACTION PAUSED\n\nNo trusted local authorization boundary "
        "is available. No local override phrase can release this action."
    )


def safe_event_details(record: dict[str, Any] | None = None, *, state: str | None = None) -> dict[str, Any]:
    del record, state
    return {"schema": SCHEMA, "state": "unsupported"}
