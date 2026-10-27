"""Authoritative outbound-send classifier and fail-closed guard.

The current Claude/Codex hook contract does not expose an authenticated user
input identity or a writer/signing key unavailable to same-user executors.
Project-local authorization JSON is therefore not authority. This module is
intentionally stateless: it classifies and screens outbound effects, but it
cannot release one until the platform supplies a trustworthy capability.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = "allostat.send-authorization/unsupported-v2"
STATE_DIRNAME = "send_authorization_v1"
DEFAULT_TTL = 0.0
COMPOSED_MAX_BYTES = 65_536
TRUSTED_TASK_ROUTING_CAPABILITY = "codex.task-routing.send-message/v1"
LEGACY_STATE_DIRNAMES = frozenset({
    "send_authorization_v1", "destructive_consent_v2",
    "scoped_action_authorization_v1",
})
LEGACY_STATE_FILENAMES = frozenset({"innate_consent.json"})

_PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|"
    r"/(?:home|Users)/[^/\s]+/|\\\\[^\\\s]+\\[^\\\s]+)", re.I,
)
_SECRET_FALLBACK = re.compile(
    r"(?:authorization\s*:\s*(?:bearer|basic)\s+\S+|"
    r"(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+)", re.I,
)
_READ_TOKENS = {"get", "list", "search", "read", "fetch", "preview"}
_COMPOSE_TOKENS = {"draft", "compose"}
_NON_MESSAGE_SEND = {"keys", "key", "event", "signal", "command", "input"}
_COMMUNICATION_TOKENS = {
    "message", "messages", "email", "emails", "mail", "gmail", "outlook",
    "smtp", "dm", "chat", "sms", "thread", "webhook", "broadcast",
}
_BODY_EVIDENCE_KEYS = {
    "body", "data", "payload", "content", "message", "text", "prompt", "html",
}
_RECIPIENT_KEYS = {
    "to", "recipient", "recipients", "cc", "bcc", "threadId", "thread_id",
    "channel", "channel_id",
}
_COMMON_KEYS = {"subject", "reply_to", "replyTo", "metadata"}
_BODY_KEYS_BY_OPERATION = {
    "gmail_send": {"body", "text", "message", "content", "html"},
    "outlook_send": {"body", "text", "message", "content", "html"},
    "smtp_send": {"body", "text", "message", "content", "html"},
    "mass_email": {"body", "text", "message", "content", "html"},
    "broadcast_message": {"body", "text", "message", "content"},
    "external_private_send": {"body", "text", "message", "prompt", "content"},
    "webhook_send_with_payload": {"body", "data", "payload", "content", "message"},
    "post_to_social": {"body", "text", "message", "content"},
    "post_to_forum": {"body", "text", "message", "content"},
    "publish_blog_post": {"body", "text", "message", "content", "html"},
}
_ATTACHMENT_FIELD = re.compile(r"(?:attachment|file)(?:s|_ids?|ids?)?$", re.I)


@dataclass(frozen=True)
class ProvenanceProof:
    trusted: bool
    status: str
    capability_id: str | None = None


@dataclass(frozen=True)
class SendDecision:
    applicable: bool
    allowed: bool
    risk_class: str | None = None
    operation: str | None = None
    authorization_source: str | None = None
    pending_id: str | None = None
    reason: str | None = None
    envelope_digest: str | None = None
    recipient_digest: str | None = None
    content_digest: str | None = None


def authenticated_internal_provenance(payload: dict[str, Any]) -> ProvenanceProof:
    """Return the measured G0 result; caller-controlled identity is ignored."""
    del payload
    return ProvenanceProof(False, "unavailable", None)


def purge_untrusted_authority_state(state_dir: Any) -> bool:
    """Remove only flat, exact legacy authority directories without following links."""
    if state_dir is None:
        return True
    clean = True
    for dirname in LEGACY_STATE_DIRNAMES:
        root = Path(state_dir) / dirname
        try:
            info = root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
            continue
        try:
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            is_reparse = bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            )
            if is_reparse:
                # A Windows junction can report as a directory while resolving
                # outside the state root. Never enumerate or clean through it;
                # the state remains ignored and the caller records cleanup failure.
                clean = False
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                root.unlink()
                continue
            children = list(root.iterdir())
            if any(child.is_dir() and not child.is_symlink() for child in children):
                clean = False
                continue
            for child in children:
                child.unlink()
            root.rmdir()
        except OSError:
            clean = False
    for filename in LEGACY_STATE_FILENAMES:
        path = Path(state_dir) / filename
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
    return clean


def targets_untrusted_authority_state(*values: Any) -> bool:
    """Best-effort direct-write denial; authority remains ignored if bypassed."""
    folded = "\n".join(str(value) for value in values if value is not None).casefold()
    names = LEGACY_STATE_DIRNAMES | LEGACY_STATE_FILENAMES
    return any(name.casefold() in folded for name in names)


def _tokens(tool_name: str | None) -> set[str]:
    tail = (tool_name or "").rsplit("__", 1)[-1].lower()
    return {item for item in re.split(r"[^a-z0-9]+", tail) if item}


def classify_send_call(tool_name: str | None, payload: dict[str, Any]) -> tuple[str, str] | None:
    """One authoritative union of the legacy and R3 send inventories."""
    tokens = _tokens(tool_name)
    if not tokens or tokens & _READ_TOKENS:
        return None
    if "send" not in tokens and tokens & _COMPOSE_TOKENS:
        return None
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    keys = {str(key).lower() for key in tool_input} if isinstance(tool_input, dict) else set()
    has_communication_evidence = bool(
        tokens & _COMMUNICATION_TOKENS or keys & _BODY_EVIDENCE_KEYS
    )
    if "broadcast" in tokens and tokens & {"message", "email", "mail"}:
        return ("public_mass_send", "broadcast_message")
    if tokens & {"batch", "bulk", "mass"} and tokens & {"email", "emails", "mail"}:
        return ("public_mass_send", "mass_email")
    if "send" in tokens:
        if tokens & _NON_MESSAGE_SEND and not has_communication_evidence:
            return None
        if "webhook" in tokens:
            if keys & {"body", "data", "payload", "content", "message", "text"}:
                return ("external_private_send", "webhook_send_with_payload")
            return None
        if tokens & {"batch", "bulk", "mass"}:
            return ("public_mass_send", "mass_email")
        if "broadcast" in tokens:
            return ("public_mass_send", "broadcast_message")
        if "gmail" in tokens:
            return ("external_private_send", "gmail_send")
        if "outlook" in tokens:
            return ("external_private_send", "outlook_send")
        if tokens & {"email", "emails", "mail", "smtp"}:
            return ("external_private_send", "smtp_send")
        if tokens & {"message", "messages", "dm", "chat", "sms", "thread"}:
            return ("external_private_send", "external_private_send")
        if keys & _BODY_EVIDENCE_KEYS:
            return ("external_private_send", "external_private_send")
        # An explicit send verb is an outbound effect even when its adapter is
        # unknown. The closed envelope parser will reject unsupported schemas.
        return ("external_private_send", "external_private_send")
    if tokens & {"tweet", "toot"}:
        return ("public_mass_send", "post_to_social")
    if "post" in tokens and "template" not in tokens:
        if "publish" in tokens:
            return ("public_mass_send", "publish_blog_post")
        if tokens & {"message", "chat"}:
            return ("public_mass_send", "broadcast_message")
        if tokens & {"forum", "discourse", "topic", "thread"}:
            return ("public_mass_send", "post_to_forum")
        if tokens & {"blog", "article"}:
            return ("public_mass_send", "publish_blog_post")
        if tokens & {"twitter", "reddit", "linkedin", "facebook", "instagram",
                     "threads", "mastodon", "bluesky", "social"}:
            return ("public_mass_send", "post_to_social")
        # Preserve the legacy inventory's generic public-post boundary, then
        # let the closed adapter reject any schema it cannot bind completely.
        return ("public_mass_send", "post_to_social")
    if "publish" in tokens and "template" not in tokens:
        return ("public_mass_send", "publish_blog_post")
    return None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield _normalized(value)
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield _normalized(str(key))
            yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_strings(nested)


def _sensitive(value: Any) -> bool:
    for text in _walk_strings(value):
        if _PRIVATE_PATH.search(text) or _SECRET_FALLBACK.search(text):
            return True
        try:
            from secret_redaction import redact_secrets
            if redact_secrets(text) != text:
                return True
        except Exception:
            if _SECRET_FALLBACK.search(text):
                return True
    return False


def _closed_envelope(
    payload: dict[str, Any], risk: str, operation: str,
) -> tuple[dict[str, Any] | None, str, str]:
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return None, _digest({}), _digest("")
    body_keys = _BODY_KEYS_BY_OPERATION.get(operation, set())
    present_body = [key for key in body_keys if key in tool_input]
    recipient = {key: tool_input[key] for key in _RECIPIENT_KEYS if key in tool_input}
    outbound = {key: tool_input[key] for key in present_body}
    recipient_digest = _digest(recipient)
    content_digest = _digest(outbound)
    if len(present_body) != 1 or not isinstance(tool_input[present_body[0]], str):
        return None, recipient_digest, content_digest
    if any(_ATTACHMENT_FIELD.search(str(key)) for key in tool_input):
        return None, recipient_digest, content_digest
    allowed = _RECIPIENT_KEYS | body_keys | _COMMON_KEYS
    if set(tool_input) - allowed:
        return None, recipient_digest, content_digest
    if "metadata" in tool_input and tool_input["metadata"] not in ({}, None):
        return None, recipient_digest, content_digest
    for key in {"subject", "reply_to", "replyTo"} & set(tool_input):
        if not isinstance(tool_input[key], str):
            return None, recipient_digest, content_digest
    envelope = {
        "schema": "send-envelope/v2", "risk": risk, "operation": operation,
        "recipients": recipient, "outbound": outbound,
        "subject": tool_input.get("subject", ""),
        "reply_to": tool_input.get("reply_to", tool_input.get("replyTo", "")),
    }
    return envelope, recipient_digest, content_digest


def evaluate_send(state_dir: Any, payload: dict[str, Any], *, now: float | None = None) -> SendDecision:
    """Screen a send and fail closed without consulting model-writable state."""
    del state_dir, now
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    classified = classify_send_call(str(tool_name), payload)
    if classified is None:
        return SendDecision(False, True, reason="not_send")
    risk, operation = classified
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    envelope, recipient_digest, content_digest = _closed_envelope(payload, risk, operation)
    if _sensitive(tool_input):
        return SendDecision(
            True, False, risk, operation, reason="sensitive_content",
            recipient_digest=recipient_digest, content_digest=content_digest,
        )
    if envelope is None:
        return SendDecision(
            True, False, risk, operation, reason="unsupported_outbound_schema",
            recipient_digest=recipient_digest, content_digest=content_digest,
        )
    return SendDecision(
        True, False, risk, operation, reason="authorization_boundary_unavailable",
        envelope_digest=_digest(envelope), recipient_digest=recipient_digest,
        content_digest=content_digest,
    )


def grant_from_prompt(
    state_dir: Any, prompt: str, *, session_id: str, turn_id: str,
    now: float | None = None,
) -> None:
    """Compatibility entry point: local prompts cannot mint send authority."""
    del state_dir, prompt, session_id, turn_id, now
    return None


def confirm_pending_from_prompt(
    state_dir: Any, prompt: str, *, session_id: str, turn_id: str,
    now: float | None = None,
) -> bool:
    del state_dir, prompt, session_id, turn_id, now
    return False


def pending_message(decision: SendDecision) -> str:
    if decision.reason == "sensitive_content":
        return (
            "EXTERNAL SEND PAUSED\n\nSensitive content or a private path was detected. "
            "Remove it and use a non-sending workflow."
        )
    if decision.reason == "unsupported_outbound_schema":
        return (
            "EXTERNAL SEND PAUSED\n\nThis outbound tool schema is ambiguous or includes "
            "an attachment that cannot be independently validated."
        )
    return (
        "EXTERNAL SEND PAUSED\n\nThis hook runtime cannot authenticate direct user "
        "authorization against same-user executors. No local confirmation phrase "
        "can release the send in this partial build."
    )


def safe_event_details(decision: SendDecision) -> dict[str, Any]:
    return {
        "risk_class": decision.risk_class, "operation": decision.operation,
        "reason": decision.reason, "envelope_digest": decision.envelope_digest,
        "recipient_digest": decision.recipient_digest,
        "content_digest": decision.content_digest, "g0_status": "unavailable",
    }
