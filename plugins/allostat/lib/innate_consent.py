"""Inert compatibility surface for the retired global consent token.

The historic ``innate_consent.json`` file was model-writable and unscoped. It
cannot authenticate an operator or bind authority to an action. Current hooks
never create or consume it; these functions remain only so older callers fail
closed during a rolling upgrade.
"""
from __future__ import annotations

OVERRIDE_PHRASE = "allostat: override"
CONSENT_FILENAME = "innate_consent.json"


def _ttl_backstop() -> float:
    return 0.0


def prompt_grants_override(prompt: str) -> bool:
    del prompt
    return False


def grant(state_dir, now: float | None = None) -> None:
    del state_dir, now
    return None


def peek(state_dir, now: float | None = None) -> bool:
    del state_dir, now
    return False


def consume_atomically(state_dir, now: float | None = None) -> None:
    del state_dir, now
    return None


def restore(state_dir, token: dict) -> bool:
    del state_dir, token
    return False


def check_and_consume(state_dir, now: float | None = None) -> bool:
    del state_dir, now
    return False
