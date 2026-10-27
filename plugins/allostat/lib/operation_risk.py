"""Closed R3 risk classification, separate from authorization state."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class RiskClass(str, Enum):
    ORDINARY = "ordinary"
    TRUSTED_INTERNAL_COORDINATION = "trusted_internal_coordination"
    EXTERNAL_PRIVATE_SEND = "external_private_send"
    PUBLIC_OR_MASS_SEND = "public_or_mass_send"
    DESTRUCTIVE = "destructive"
    PERMISSION_OR_FINANCIAL = "permission_or_financial"


_PRIVATE_SEND_OPS = frozenset({
    "external_private_send", "smtp_send", "gmail_send", "outlook_send",
})
_PUBLIC_SEND_OPS = frozenset({
    "broadcast_message", "post_to_social", "post_to_forum",
    "publish_blog_post", "mass_email", "webhook_send_with_payload",
})
_PERMISSION_OPS = frozenset({
    "oauth_authorize", "sso_complete", "terms_of_service_accept",
    "privacy_policy_accept", "cookie_consent", "sharing_permission_grant",
    "api_key_grant", "webhook_endpoint_register", "stripe_charge",
    "payment_complete", "subscription_activate", "account_setting_change",
    "password_set", "2fa_setup",
})


@dataclass(frozen=True)
class OperationRisk:
    risk_class: RiskClass
    semantic_operation: str | None
    trusted_capability_id: str | None = None
    protection_flags: frozenset[str] = frozenset()

    def with_flags(self, flags: Iterable[str]) -> "OperationRisk":
        return OperationRisk(
            self.risk_class,
            self.semantic_operation,
            self.trusted_capability_id,
            self.protection_flags | frozenset(str(flag) for flag in flags),
        )


def classify_operation_risk(
    semantic_operation: str | None,
    *,
    authenticated_internal: bool = False,
    capability_id: str | None = None,
) -> OperationRisk:
    """Classify risk without consulting or consuming authorization.

    `authenticated_internal` may be true only after the host-authenticated G0
    proof.  Current Codex hooks do not provide that proof, so callers pass
    false and lexical namespaces can never reach the internal class.
    """
    operation = str(semantic_operation) if semantic_operation else None
    if operation in _PRIVATE_SEND_OPS:
        if authenticated_internal and capability_id:
            return OperationRisk(
                RiskClass.TRUSTED_INTERNAL_COORDINATION, operation, capability_id
            )
        return OperationRisk(RiskClass.EXTERNAL_PRIVATE_SEND, operation)
    if operation in _PUBLIC_SEND_OPS:
        return OperationRisk(RiskClass.PUBLIC_OR_MASS_SEND, operation)
    if operation in _PERMISSION_OPS:
        return OperationRisk(RiskClass.PERMISSION_OR_FINANCIAL, operation)
    if operation == "destructive":
        return OperationRisk(RiskClass.DESTRUCTIVE, operation)
    return OperationRisk(RiskClass.ORDINARY, operation)
