"""Verify a signed entitlement lease. Stdlib only, verification only.

## What this defends against, stated accurately (advisor ruling, 2026-07-20)

**Casual tampering and lease sharing. Not a determined local user.**

Before this, entitlement lived in an unsigned JSON file, so the bypass was
"change one word to `active`". That instruction fits in a forum comment and
propagates; it ends a pricing model. A signed lease removes the one-line bypass
and makes a lease non-shareable between accounts.

It does **not** stop someone with write access to the plugin. Explicitly out of
scope, by name:

  * patching this file or bypassing the caller;
  * replacing the vendored public key with one they control;
  * **rolling the system clock back, or freezing it, to keep a lease inside its
    signed window.**

None of these needs the issuer's private key. That is accepted. The population
willing to patch source at $25/mo is small, and more importantly their method
does not spread the way a JSON edit does.

**On the clock specifically, because silence there reads as a claim.** The
signature bounds the *signed interval* to three days. It cannot bound *real
elapsed time* — that would need trusted time, a monotonic source, or persisted
rollback state, none of which exist here. A local user who holds their clock
inside the signed window keeps the lease longer than three real days. They are
already outside this threat model; patching the verifier would be easier. So
**"three days" is a bound on the interval a lease may assert, never a guarantee
about real time elapsed on a machine we do not control.**

**Do not describe this as stopping determined users.** Real enforcement would
require server-authorized value, which conflicts with the client-side privacy
positioning — a product decision, not a crypto one.

## Why RSA and not Ed25519 (advisor ratified 2026-07-20)

The wrapper is stdlib-only: `pyproject.toml` declares `dependencies = []` and
the public README says "stdlib only — no httpx, no MCP SDK on the wrapper side".
The plugin must install on whatever Python a customer already has, so a compiled
wheel like `cryptography` would turn an unusual platform into an install failure
at the moment someone is trying to pay. Hand-rolling Ed25519 means ~150 lines of
curve arithmetic; RSA PKCS#1 v1.5 verification is `pow(sig, 65537, n)` plus a
byte comparison, with Python's own bignum doing the work. Codex independently
confirmed interoperability with `cryptography`'s PKCS1v15/SHA-256 signer.

## The three defects Codex found here, and what closes them

1. **`e=1` accepted a forged signature.** The first version validated modulus
   *length* and nothing else. With `e = 1`, `pow(s, 1, n) == s`, so the encoded
   block is its own valid signature and anyone can mint a lease with a key they
   invented. That is not a weak check, it is the absence of a signature check.
   Now: the exponent must be exactly 65537 and the key is validated before any
   lease processing.
2. **Identity binding was optional.** `expected_user_id`/`expected_namespace`
   defaulted to `None` and were checked only when supplied, so the default call
   accepted another customer's lease — and the happy-path tests demonstrated
   that shape. They are now required arguments. An optional security parameter
   is a fail-open with a nicer name.
3. **The operator-locked 3-day lifetime was not enforced.** `issued_at` was
   signed and never read; only `expires_at > now` was checked, so a ten-year
   lease verified. The lifetime is now a client-side invariant, which means an
   issuer bug cannot silently mint non-revocable entitlement.

**Verification only.** No private key material reaches a customer's machine, and
every failure path here resolves to "not entitled".
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# SHA-256 DigestInfo prefix (RFC 8017 §9.2).
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

# The ONLY public exponent this verifier accepts. Not a default — a requirement.
REQUIRED_PUBLIC_EXPONENT = 65537

_MIN_MODULUS_BITS = 2048
_MAX_MODULUS_BITS = 8192

# Operator-locked, 2026-07-20. The lease governs exactly one thing: how long a
# customer keeps working while allostat.ai is unreachable. There is no offline
# use case — Claude Code and Codex both require a network — so this is sized to
# our own outage, not to a travelling developer.
MAX_LEASE_LIFETIME = timedelta(days=3)

# Tolerance for a client clock running behind the issuer's. Deliberately narrow:
# wide skew tolerance is how a "3 day" lease quietly becomes longer.
MAX_CLOCK_SKEW = timedelta(minutes=5)

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

_ENTITLED_STATES = frozenset({"active", "trial"})


class LeaseError(Exception):
    """Any reason a lease is not usable. Callers treat this as NOT ENTITLED."""


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """The exact bytes that get signed.

    Signer and verifier must agree byte-for-byte, so this is deliberately rigid:
    sorted keys, no insignificant whitespace, UTF-8. Drift between the two sides
    surfaces as a signature mismatch — a denial — rather than as a quiet
    acceptance of the wrong thing.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def validate_public_key(public_key: Any) -> tuple[int, int]:
    """Return a validated `(n, e)`, or raise.

    Called before ANY lease processing. The vendored public key is the trust
    root, so a malformed build artifact or a key-publication error must fail
    closed here rather than silently degrade verification into identity
    encoding — which is exactly what `e = 1` does.
    """
    if not isinstance(public_key, tuple) or len(public_key) != 2:
        raise LeaseError("public key must be an (n, e) pair")
    n, e = public_key

    if not isinstance(n, int) or isinstance(n, bool):
        raise LeaseError("modulus is not an integer")
    if not isinstance(e, int) or isinstance(e, bool):
        raise LeaseError("exponent is not an integer")

    if e != REQUIRED_PUBLIC_EXPONENT:
        raise LeaseError(
            f"public exponent must be exactly {REQUIRED_PUBLIC_EXPONENT}, got {e!r}"
        )

    if n <= 0:
        raise LeaseError("modulus must be positive")
    if n % 2 == 0:
        raise LeaseError("modulus must be odd")
    bits = n.bit_length()
    if bits < _MIN_MODULUS_BITS:
        raise LeaseError(f"modulus is {bits} bits; minimum is {_MIN_MODULUS_BITS}")
    if bits > _MAX_MODULUS_BITS:
        raise LeaseError(f"modulus is {bits} bits; maximum is {_MAX_MODULUS_BITS}")

    return n, e


def _b64u_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except (binascii.Error, ValueError) as exc:
        raise LeaseError(f"malformed base64url: {exc}") from exc


def verify_signature(
    payload_bytes: bytes, signature: bytes, public_key: tuple[int, int]
) -> bool:
    """RSASSA-PKCS1-v1_5 verify with SHA-256. True only on a full block match.

    Returns False rather than raising on a structural problem, so every failure
    path lands on "not verified". Invalid KEY material raises, because that is a
    build/deployment fault rather than a bad lease and must not be mistaken for
    an ordinary rejection.
    """
    n, e = validate_public_key(public_key)

    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False

    s = int.from_bytes(signature, "big")
    if not (0 <= s < n):
        return False

    em = pow(s, e, n).to_bytes(k, "big")

    # Rebuild what a correct block must be, then require EXACT equality.
    # Scanning `em` for the DigestInfo instead is the Bleichenbacher-2006
    # forgery class.
    digest = hashlib.sha256(payload_bytes).digest()
    tail = _SHA256_DIGEST_INFO_PREFIX + digest
    ps_len = k - len(tail) - 3
    if ps_len < 8:  # RFC 8017 requires at least 8 padding bytes
        return False
    expected = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + tail

    return hmac.compare_digest(em, expected)


def parse_public_key(spec: str) -> tuple[int, int]:
    """Accept the vendored public key as `n:e` hex, validated.

    Deliberately not PEM/DER: parsing ASN.1 with the standard library means
    hand-rolling a parser, which is more attack surface than two integers are
    worth.
    """
    if not isinstance(spec, str):
        raise LeaseError("public key spec must be a string")
    try:
        n_hex, e_hex = spec.strip().split(":", 1)
        candidate = (int(n_hex, 16), int(e_hex, 16))
    except (ValueError, AttributeError) as exc:
        raise LeaseError(f"malformed public key: {exc}") from exc
    return validate_public_key(candidate)


def _require_aware_datetime(payload: dict[str, Any], field: str) -> datetime:
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw:
        raise LeaseError(f"lease has no {field}")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise LeaseError(f"malformed {field}: {exc}") from exc
    if value.tzinfo is None:
        # A naive timestamp is ambiguous, and the ambiguity always favours the
        # holder somewhere on the planet. Refuse rather than assume UTC.
        raise LeaseError(f"{field} must be timezone-aware")
    return value


def verify_lease(
    lease: Any,
    public_key: tuple[int, int],
    expected_user_id: str,
    expected_namespace: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the verified payload, or raise `LeaseError`.

    `expected_user_id` and `expected_namespace` are REQUIRED positional
    arguments. They were optional keyword arguments in the first version, which
    meant the shortest call — the one the tests demonstrated — accepted any
    customer's lease. There is no call shape here that skips a security check.

    Every check is a hard failure. No branch resolves to entitled on doubt.
    """
    # Key material first: a bad trust root invalidates everything downstream.
    n, e = validate_public_key(public_key)

    if not isinstance(expected_user_id, str) or not expected_user_id:
        raise LeaseError("expected_user_id is required")
    if not isinstance(expected_namespace, str) or not expected_namespace:
        raise LeaseError("expected_namespace is required")

    if not isinstance(lease, dict):
        raise LeaseError("lease is not an object")

    payload = lease.get("payload")
    signature_b64 = lease.get("signature")
    if not isinstance(payload, dict):
        raise LeaseError("lease has no payload object")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise LeaseError("lease has no signature")

    if not verify_signature(canonical_payload_bytes(payload), _b64u_decode(signature_b64), (n, e)):
        raise LeaseError("signature does not verify")

    # --- everything below runs only on a cryptographically valid lease ---

    version = payload.get("schema_version")
    # Type check before membership: `1.0 in {1}` is True in Python, and `True`
    # equals 1, so a bare `in` test accepts a float or a bool as version 1.
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise LeaseError(f"unsupported lease schema_version: {version!r}")

    entitlement = payload.get("entitlement")
    if entitlement not in _ENTITLED_STATES:
        raise LeaseError(f"lease does not grant entitlement: {entitlement!r}")

    issued_at = _require_aware_datetime(payload, "issued_at")
    expires_at = _require_aware_datetime(payload, "expires_at")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    if expires_at <= issued_at:
        raise LeaseError("lease expires before it was issued")
    if expires_at - issued_at > MAX_LEASE_LIFETIME:
        # The operator locked 3 days. Enforcing it HERE makes it an invariant
        # rather than an issuer convention: an issuer bug cannot mint an
        # effectively non-revocable lease, because this client refuses it.
        raise LeaseError(
            f"lease lifetime {expires_at - issued_at} exceeds the maximum "
            f"{MAX_LEASE_LIFETIME}"
        )
    if issued_at > current + MAX_CLOCK_SKEW:
        raise LeaseError("lease was issued in the future")
    if current >= expires_at:
        raise LeaseError(f"lease expired at {expires_at.isoformat()}")

    # Binding: a valid lease for a different user or environment is not a lease
    # for this one. Without these, one paying customer's lease entitles every
    # machine it is copied to — which is what makes it worth sharing.
    claimed_user = payload.get("user_id")
    claimed_namespace = payload.get("namespace")
    if not isinstance(claimed_user, str) or not claimed_user:
        raise LeaseError("lease has no user_id claim")
    if not isinstance(claimed_namespace, str) or not claimed_namespace:
        raise LeaseError("lease has no namespace claim")
    if not hmac.compare_digest(claimed_user, expected_user_id):
        raise LeaseError("lease is bound to a different user")
    if not hmac.compare_digest(claimed_namespace, expected_namespace):
        raise LeaseError("lease is bound to a different namespace")

    return payload
