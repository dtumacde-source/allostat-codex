"""Deprecated local protected-write and permission authorization surface."""
from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA = "allostat.scoped-action-consent/unsupported-v2"
STATE_DIRNAME = "scoped_action_authorization_v1"
DEFAULT_TTL = 0.0


def action_digest(
    action: dict[str, Any], *, rule_id: str, target_hint: str | None = None,
) -> str:
    raw = json.dumps(
        {"action": action, "rule_id": rule_id, "target_hint": target_hint},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_pending(
    state_dir: Any, action: dict[str, Any], *, rule_id: str,
    session_id: str, turn_id: str, target_hint: str | None = None,
    now: float | None = None, ttl: float = DEFAULT_TTL,
) -> None:
    del state_dir, action, rule_id, session_id, turn_id, target_hint, now, ttl
    return None


def arm_from_prompt(
    state_dir: Any, prompt: str, *, session_id: str, turn_id: str,
    now: float | None = None,
) -> bool:
    del state_dir, prompt, session_id, turn_id, now
    return False


def consume_matching(
    state_dir: Any, action: dict[str, Any], *, rule_id: str,
    session_id: str, turn_id: str, target_hint: str | None = None,
    now: float | None = None,
) -> None:
    del state_dir, action, rule_id, session_id, turn_id, target_hint, now
    return None


def restore(state_dir: Any, token: dict[str, Any], *, now: float | None = None) -> bool:
    del state_dir, token, now
    return False


def refusal_message(record: dict[str, Any] | None = None) -> str:
    del record
    return (
        "ACTION PAUSED\n\nNo trusted local authorization boundary is available. "
        "No local confirmation phrase can release this action."
    )


def safe_event_details(record: dict[str, Any] | None = None, *, state: str | None = None) -> dict[str, Any]:
    del record, state
    return {"schema": SCHEMA, "state": "unsupported"}
