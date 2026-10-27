"""Consolidated server tunables — single home for scattered numeric constants.

Pre-release item 35 (2026-06-10): the cascade-dedup window, the
subscription-state cache TTL, and the install-flow rate-limit knobs used
to live as module-level literals inside their consuming modules
(dispatch_tools.py, subscription_state_middleware.py, install_flow.py).
Consolidated here so operators can tune them via ALLOSTAT_* environment
variables without code edits.

Discipline:

- Values are read ONCE at import. Changing the env var requires a server
  restart — same lifecycle as ALLOSTAT_ENV / ALLOSTAT_DB_PATH.
- Defaults are bit-identical to the pre-consolidation literals (value AND
  type) — the unit suite pins current behavior (tests/test_config.py).
- A malformed override (e.g., ALLOSTAT_DEDUP_WINDOW_SECONDS=abc) raises
  ValueError at import: fail fast at startup beats silently running with
  an unintended default.
"""
from __future__ import annotations

import hashlib
import math
import os
import urllib.parse


def _env_int(name: str, default: int) -> int:
    """Read an int env override; unset/blank returns the default unchanged."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """Read a float env override; unset/blank returns the default unchanged."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_str(name: str, default: str) -> str:
    """Read a string env override; unset/whitespace-only returns the default.

    Track 2 billing (S1): sibling of _env_int/_env_float for the string-valued
    ALLOSTAT_* knobs (URLs, version stamps, price-display copy). A blank or
    whitespace-only value is treated as unset — same posture as the numeric
    readers — so a stray `ALLOSTAT_BILLING_SUCCESS_URL=` line can't blank a
    URL to the empty string. The returned value is NOT stripped (a legitimate
    display string like "$25.00 / month" keeps its internal spacing).
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env override; unset/blank returns the default.

    Track 2 billing (S1): backs the billing enablement flag. Truthy tokens
    are {1, true, yes, on} (case-insensitive); anything else (including an
    unrecognized value like "banana") is False. Blank/unset returns default.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_TOKENS


# PATCH-149 cascade dedup (dispatch_tools.py) — suppression window for
# same-(session_id, pillar, body_fingerprint) decisions. 5 minutes is the
# v2.3 standard.
DEDUP_WINDOW_SECONDS: int = _env_int("ALLOSTAT_DEDUP_WINDOW_SECONDS", 300)

# Subscription-state per-bearer cache TTL (subscription_state_middleware.py).
# Short enough that re-sub flips take effect within ~1 minute.
SUB_STATE_CACHE_TTL_SECONDS: float = _env_float(
    "ALLOSTAT_SUB_STATE_CACHE_TTL_SECONDS", 60.0
)
SUB_STATE_FAULT_GRACE_MAX_SECONDS: float = 300.0
SUB_STATE_FAULT_RETRY_MAX_SECONDS: float = 5.0


def _sub_state_fault_cache_window() -> tuple[float, float]:
    """Return the bounded (grace, retry) entitlement fault window.

    These values are read at use time because the middleware historically
    allowed runtime overrides. Invalid values fail closed instead of turning
    the last-known ACTIVE cache into an unbounded authorization source.
    """
    try:
        grace = _env_float(
            "ALLOSTAT_SUB_STATE_FAULT_GRACE_SECONDS",
            SUB_STATE_FAULT_GRACE_MAX_SECONDS,
        )
        retry = _env_float(
            "ALLOSTAT_SUB_STATE_FAULT_RETRY_TTL",
            SUB_STATE_FAULT_RETRY_MAX_SECONDS,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("subscription fault cache window is not numeric") from exc
    if (
        not math.isfinite(grace)
        or not 0.0 <= grace <= SUB_STATE_FAULT_GRACE_MAX_SECONDS
        or not math.isfinite(retry)
        or not 0.0 <= retry <= SUB_STATE_FAULT_RETRY_MAX_SECONDS
        or retry > grace
    ):
        raise ValueError(
            "subscription fault cache window must use finite nonnegative "
            "values, retry must not exceed grace, grace must not exceed "
            f"{SUB_STATE_FAULT_GRACE_MAX_SECONDS:g}s, and retry must not "
            f"exceed {SUB_STATE_FAULT_RETRY_MAX_SECONDS:g}s"
        )
    return grace, retry

# /install/resolve per-IP limiter (install_flow.py). Brute-forcing a
# 16-char Base32 code is infeasible regardless; cheap defense-in-depth.
RESOLVE_LIMIT_MAX_REQUESTS: int = _env_int("ALLOSTAT_RESOLVE_LIMIT_MAX_REQUESTS", 5)
RESOLVE_LIMIT_WINDOW_SECONDS: float = _env_float(
    "ALLOSTAT_RESOLVE_LIMIT_WINDOW_SECONDS", 60.0
)

# /admin/* shared per-IP limiter (install_flow.py). Generous for legit
# admin use (operations are manual + rare).
ADMIN_LIMIT_MAX_REQUESTS: int = _env_int("ALLOSTAT_ADMIN_LIMIT_MAX_REQUESTS", 10)
ADMIN_LIMIT_WINDOW_SECONDS: float = _env_float(
    "ALLOSTAT_ADMIN_LIMIT_WINDOW_SECONDS", 60.0
)

# /install/refresh per-BEARER limiter (install_flow.py). A few checks/hour
# is plenty for a manual `/allostat update now` + headroom.
REFRESH_LIMIT_MAX_REQUESTS: int = _env_int("ALLOSTAT_REFRESH_LIMIT_MAX_REQUESTS", 6)
REFRESH_LIMIT_WINDOW_SECONDS: float = _env_float(
    "ALLOSTAT_REFRESH_LIMIT_WINDOW_SECONDS", 3600.0
)


# ---------------------------------------------------------------------------
# Track 2 — paid self-serve checkout (Stripe). Build plan §4.1
# (design/20260716_track2_paid_checkout_build_plan.md). Read-once-at-import
# like every knob above; changing an env var requires a server restart.
#
# SECRETS: STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET carry NO repo default
# (empty string). Real values live ONLY in the VPS systemd EnvironmentFile
# (/etc/default/allostat-server) — never in the repo (compliance C8). The
# production fail-loud gate below turns a missing secret into a startup
# RuntimeError when billing is enabled, instead of silently provisioning.
# ---------------------------------------------------------------------------

# Server-only Stripe credentials + exact catalog identity. Price/product carry
# empty defaults; production+billing-enabled startup raises if any required
# Stripe value is unset (validate_billing_env_or_raise).
STRIPE_SECRET_KEY: str = os.environ.get("ALLOSTAT_STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.environ.get("ALLOSTAT_STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID: str = os.environ.get("ALLOSTAT_STRIPE_PRICE_ID", "")
STRIPE_PRODUCT_ID: str = os.environ.get("ALLOSTAT_STRIPE_PRODUCT_ID", "")

# Redirect targets. Default to allostat.ai (operator-locked pricing-page host).
#
# P2-3: the default MUST carry Stripe's literal `{CHECKOUT_SESSION_ID}`
# placeholder (and a `session_id=` query param) — Stripe substitutes the
# real completed-session id into that placeholder when it redirects the
# browser back here, and `welcome.html`'s masked-email call to
# `POST /account/checkout-email` reads that id straight off the URL
# (`?session_id=...`). A success URL missing the placeholder is not a
# cosmetic gap: every live checkout would land on a welcome page that can
# never resolve a session id, so the masked-email confirmation silently
# never fires. `validate_billing_env_or_raise` below re-checks the LIVE
# value at production startup and fails closed if it's ever misconfigured
# without the placeholder.
BILLING_SUCCESS_URL: str = _env_str(
    "ALLOSTAT_BILLING_SUCCESS_URL",
    "https://allostat.ai/welcome?session_id={CHECKOUT_SESSION_ID}",
)
BILLING_CANCEL_URL: str = _env_str(
    "ALLOSTAT_BILLING_CANCEL_URL", "https://allostat.ai/pricing"
)
BILLING_PORTAL_RETURN_URL: str = _env_str(
    "ALLOSTAT_BILLING_PORTAL_RETURN_URL", "https://allostat.ai/pricing"
)
# Exact browser origin of the published pricing page. No default: production
# billing must spell the canonical value in its root-only EnvironmentFile.
BILLING_BROWSER_ORIGIN: str = os.environ.get(
    "ALLOSTAT_BILLING_BROWSER_ORIGIN", ""
)

# past_due grace window — the SERVER-SIDE ceiling on dunning access, independent
# of any Stripe dashboard setting. A past_due subscription entitles only until
# current_period_end + PASTDUE_GRACE_DAYS; beyond that it reads 'lapsed' at the
# request gate (db.lookup_subscription_state) and reconciles to 'lapsed' on the
# next webhook event (billing._reconcile_user_subscription_state). Anchored on
# current_period_end (the paid-through date) so the effective ceiling is roughly
# one unpaid period + this many days — bounded, and it closes the "past_due
# forever" free-access path even if Stripe is configured to never cancel.
PASTDUE_GRACE_DAYS: int = _env_int("ALLOSTAT_PASTDUE_GRACE_DAYS", 7)

# Consent-record version stamps (FTC/CA-ARL). POLICY_VERSION = terms+privacy
# version in force; OFFER_VERSION = pricing-page/offer version. Both are
# written verbatim into every consent_records row (build plan §2.2).
POLICY_VERSION: str = _env_str("ALLOSTAT_POLICY_VERSION", "2026-07-16")
OFFER_VERSION: str = _env_str("ALLOSTAT_OFFER_VERSION", "launch-25mo-v1")

# Exact price string shown to the user + stored in the consent record. M-09's
# offer validator rejects startup/issuance if it disagrees with the numeric
# currency/amount/interval catalog below.
PRICE_AMOUNT_DISPLAY: str = _env_str("ALLOSTAT_PRICE_AMOUNT_DISPLAY", "$25.00 / month")

# Server-authoritative public offer.  The pricing page renders this exact
# catalog/terms snapshot and checkout accepts only a short-lived signed token
# over the same canonical JSON.  Signing keys are a kid -> unpadded base64url
# JSON mapping so rotation can retain old verification keys briefly without
# changing the active signer.
BILLING_CATALOG_VERSION: str = _env_str(
    "ALLOSTAT_BILLING_CATALOG_VERSION", "allostat-monthly-v1"
)
BILLING_CURRENCY: str = _env_str("ALLOSTAT_BILLING_CURRENCY", "usd")
BILLING_UNIT_AMOUNT: int = _env_int("ALLOSTAT_BILLING_UNIT_AMOUNT", 2500)
BILLING_INTERVAL: str = _env_str("ALLOSTAT_BILLING_INTERVAL", "month")
BILLING_INTERVAL_COUNT: int = _env_int(
    "ALLOSTAT_BILLING_INTERVAL_COUNT", 1
)
BILLING_OFFER_TTL_SECONDS: int = _env_int(
    "ALLOSTAT_BILLING_OFFER_TTL_SECONDS", 600
)
BILLING_OFFER_ACTIVE_KID: str = os.environ.get(
    "ALLOSTAT_BILLING_OFFER_ACTIVE_KID", ""
)
BILLING_OFFER_KEYS_JSON: str = os.environ.get(
    "ALLOSTAT_BILLING_OFFER_KEYS_JSON", ""
)

# Per-IP rate limiters for the self-authenticating billing endpoints
# (/billing/checkout S6, /billing/portal S7). They sit in PUBLIC_BYPASS_PATHS
# so the global RateLimitMiddleware never sees them; these knobs back the
# billing.py _IpRateLimiter instances (same pattern as the install-flow
# limiters above). Defaults are generous for a human clicking "subscribe".
BILLING_CHECKOUT_LIMIT_MAX_REQUESTS: int = _env_int(
    "ALLOSTAT_BILLING_CHECKOUT_LIMIT_MAX_REQUESTS", 10
)
BILLING_CHECKOUT_LIMIT_WINDOW_SECONDS: float = _env_float(
    "ALLOSTAT_BILLING_CHECKOUT_LIMIT_WINDOW_SECONDS", 60.0
)
BILLING_PORTAL_LIMIT_MAX_REQUESTS: int = _env_int(
    "ALLOSTAT_BILLING_PORTAL_LIMIT_MAX_REQUESTS", 10
)
BILLING_PORTAL_LIMIT_WINDOW_SECONDS: float = _env_float(
    "ALLOSTAT_BILLING_PORTAL_LIMIT_WINDOW_SECONDS", 60.0
)

# Billing enablement flag. OFF by default so an existing production server
# (which has no Stripe env) never trips the fail-loud gate below — the gate is
# inert until the operator explicitly opts in with ALLOSTAT_BILLING_ENABLED=1.
# This preserves zero behavior change for every non-billing deploy.
BILLING_ENABLED: bool = _env_bool("ALLOSTAT_BILLING_ENABLED", False)


def billing_enabled_from_environment() -> bool:
    """Return the live billing flag using the same boolean grammar as startup."""
    return _env_bool("ALLOSTAT_BILLING_ENABLED", False)


# Stripe credentials and catalog ids the checkout/webhook/portal handlers
# cannot function without. Read live (not the import-time constants) by the
# validator so a test — or a restart — sees the current environment.
_BILLING_REQUIRED_ENV_VARS = (
    "ALLOSTAT_STRIPE_SECRET_KEY",
    "ALLOSTAT_STRIPE_WEBHOOK_SECRET",
    "ALLOSTAT_STRIPE_PRICE_ID",
    "ALLOSTAT_STRIPE_PRODUCT_ID",
    "ALLOSTAT_BILLING_CATALOG_VERSION",
    "ALLOSTAT_BILLING_CURRENCY",
    "ALLOSTAT_BILLING_UNIT_AMOUNT",
    "ALLOSTAT_BILLING_INTERVAL",
    "ALLOSTAT_BILLING_INTERVAL_COUNT",
    "ALLOSTAT_BILLING_OFFER_TTL_SECONDS",
    "ALLOSTAT_BILLING_OFFER_ACTIVE_KID",
    "ALLOSTAT_BILLING_OFFER_KEYS_JSON",
)
_BILLING_REQUIRED_PRODUCTION_ENV = _BILLING_REQUIRED_ENV_VARS


def validate_billing_env_or_raise() -> None:
    """Fail loud on unsafe Stripe configuration when billing is enabled.

    Called from main.build_app() — the same real startup path and posture as
    install_flow.validate_bundle_env_or_raise() (PATCH-116) and the promote
    pre-flight required-env gate.

    Every enabled environment requires a nonblank import-time webhook secret:
    an empty secret is a usable, publicly known HMAC key. Production applies
    the stronger gate for every required Stripe variable, reading os.environ
    live so tests and the real startup process see the current environment.
    """
    # B-01: a missing webhook secret is not merely an unavailable integration.
    # Stripe's HMAC verifier accepts any byte string as a key, including the
    # empty default, so an enabled route with no configured secret has a
    # publicly known signing key. Refuse that state in every environment. The
    # production-only block below retains the stronger all-Stripe-vars gate.
    if BILLING_ENABLED and not STRIPE_WEBHOOK_SECRET.strip():
        raise RuntimeError(
            "Billing is enabled but ALLOSTAT_STRIPE_WEBHOOK_SECRET is missing "
            "or blank. Configure a non-empty Stripe webhook signing secret "
            "before exposing billing routes."
        )

    if os.environ.get("ALLOSTAT_ENV") != "production":
        return
    if not _env_bool("ALLOSTAT_BILLING_ENABLED", False):
        return
    missing = [
        name
        for name in _BILLING_REQUIRED_ENV_VARS
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Billing is enabled (ALLOSTAT_BILLING_ENABLED) in production but "
            "required Stripe env vars are missing or blank: "
            + ", ".join(missing)
            + ". Set them in the systemd EnvironmentFile "
            "(/etc/default/allostat-server) — never in the repo — or disable "
            "billing (unset ALLOSTAT_BILLING_ENABLED)."
        )

    # Sol re-audit P2-2: the configured success URL used to be validated by
    # SUBSTRING only ("{CHECKOUT_SESSION_ID}" in url and "session_id=" in
    # url), so `https://offsite.example/collect?session_id={CHECKOUT_SESSION_ID}`
    # PASSED -- Stripe would then redirect the browser (carrying the
    # completed checkout session id) to an attacker-controlled off-site host.
    # Parse with urlsplit and assert the EXACT canonical shape instead: only
    # `https://allostat.ai/welcome?session_id={CHECKOUT_SESSION_ID}` (no
    # userinfo, no non-default port, no extra/missing query, no fragment) may
    # pass. Read the LIVE env value (not the import-time constant) so a
    # misconfigured restart is caught rather than a stale in-process default
    # masking it.
    #
    # The query comparison is a direct string equality against
    # urlsplit(...).query -- urlsplit never percent-encodes or decodes, so
    # the literal `{CHECKOUT_SESSION_ID}` placeholder round-trips unchanged
    # and this is comparing the RAW configured query string, not a
    # re-encoded one (parse_qs/urlencode are intentionally not used here,
    # since either would mangle the literal braces).
    _EXPECTED_SUCCESS_URL = (
        "https://allostat.ai/welcome?session_id={CHECKOUT_SESSION_ID}"
    )
    success_url = _env_str("ALLOSTAT_BILLING_SUCCESS_URL", BILLING_SUCCESS_URL)
    success_url_problems: list[str] = []
    try:
        parsed_success_url = urllib.parse.urlsplit(success_url)
        # Validate all components, including .port (which can raise ValueError
        # for malformed or out-of-range ports).
        if parsed_success_url.scheme != "https":
            success_url_problems.append(
                f"scheme must be 'https' (got {parsed_success_url.scheme!r})"
            )
        if parsed_success_url.username is not None or parsed_success_url.password is not None:
            success_url_problems.append("must not carry userinfo/credentials")
        if parsed_success_url.hostname != "allostat.ai":
            success_url_problems.append(
                f"host must be exactly 'allostat.ai' (got {parsed_success_url.hostname!r})"
            )
        # .port raises ValueError for non-numeric or out-of-range ports
        if parsed_success_url.port not in (None, 443):
            success_url_problems.append(
                f"must not specify a non-default port (got {parsed_success_url.port!r})"
            )
        if parsed_success_url.path != "/welcome":
            success_url_problems.append(
                f"path must be exactly '/welcome' (got {parsed_success_url.path!r})"
            )
        if parsed_success_url.query != "session_id={CHECKOUT_SESSION_ID}":
            success_url_problems.append(
                "query must be exactly 'session_id={CHECKOUT_SESSION_ID}' "
                f"(got {parsed_success_url.query!r})"
            )
        if parsed_success_url.fragment:
            success_url_problems.append(
                f"must not carry a fragment (got {parsed_success_url.fragment!r})"
            )
    except ValueError as exc:
        # Preserve port-specific error details if the ValueError is about the port
        error_msg = (
            "Production billing success URL (ALLOSTAT_BILLING_SUCCESS_URL="
            + repr(success_url)
            + ") has invalid port; expected exactly "
            + repr(_EXPECTED_SUCCESS_URL)
        )
        raise RuntimeError(error_msg) from exc
    if success_url_problems:
        raise RuntimeError(
            "Production billing success URL (ALLOSTAT_BILLING_SUCCESS_URL="
            + repr(success_url)
            + ") is not the exact canonical shape "
            + repr(_EXPECTED_SUCCESS_URL)
            + " -- "
            + "; ".join(success_url_problems)
            + ". Stripe redirects the browser to this EXACT URL after "
            "checkout, carrying the completed session id -- anything other "
            "than the canonical allostat.ai/welcome shape risks leaking "
            "that session id off-site or landing on a page that can never "
            "resolve it."
        )

    # Billing is an entitlement boundary.  In production, a healthy database
    # read must therefore be authoritative on every request; only the separate
    # bounded DB-fault path may consult the last validated value.  Requiring
    # the live environment to spell an exact finite zero avoids a default,
    # NaN, negative, or positive TTL silently granting stale access.
    raw_ttl = os.environ.get("ALLOSTAT_SUB_STATE_CACHE_TTL_SECONDS")
    try:
        ttl = float(raw_ttl) if raw_ttl is not None else math.nan
    except (TypeError, ValueError):
        ttl = math.nan
    if not math.isfinite(ttl) or ttl != 0.0:
        raise RuntimeError(
            "Production billing requires explicit "
            "ALLOSTAT_SUB_STATE_CACHE_TTL_SECONDS=0 so healthy entitlement "
            "reads are authoritative; bounded grace remains DB-errors-only."
        )

    try:
        _sub_state_fault_cache_window()
    except ValueError as exc:
        raise RuntimeError(
            "ALLOSTAT_SUB_STATE_FAULT_GRACE_SECONDS and "
            "ALLOSTAT_SUB_STATE_FAULT_RETRY_TTL must define a finite, "
            "nonnegative bounded fault cache window with retry <= grace"
        ) from exc

    # Validate the signing keyring and remaining catalog semantics using the
    # same strict parser as the endpoint.
    from .billing_offer import validate_offer_configuration

    validate_offer_configuration(from_environment=True)


def validate_billing_config_or_raise(*, from_environment: bool = True) -> None:
    """Validate the complete production billing runtime, including B-06.

    ``from_environment`` is retained to make the call site explicit; all
    production gates intentionally read the live environment so startup cannot
    validate stale import-time credentials.
    """
    del from_environment
    validate_billing_env_or_raise()
    if os.environ.get("ALLOSTAT_ENV") != "production":
        return
    if not _env_bool("ALLOSTAT_BILLING_ENABLED", False):
        return
    from .billing_cors import validate_browser_origin

    try:
        validate_browser_origin(
            os.environ.get("ALLOSTAT_BILLING_BROWSER_ORIGIN", "")
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "Production billing browser origin is missing or invalid: "
            "set ALLOSTAT_BILLING_BROWSER_ORIGIN=https://allostat.ai"
        ) from exc
    from .billing_delivery import validate_delivery_environment

    validate_delivery_environment(production=True)


# ---------------------------------------------------------------------------
# Passwordless account subsystem (tokens, sessions, rate limits).
# Read-once-at-import like all knobs above; changing an env var requires
# a server restart.
# ---------------------------------------------------------------------------

# Email verification link TTL (how long the user has to click the verify link
# after requesting passwordless login).
ACCOUNT_VERIFY_TTL_SECONDS: int = _env_int("ALLOSTAT_ACCOUNT_VERIFY_TTL_SECONDS", 259200)

# Login token TTL (short-lived token issued after email verification, used
# to create a session).
ACCOUNT_LOGIN_TTL_SECONDS: int = _env_int("ALLOSTAT_ACCOUNT_LOGIN_TTL_SECONDS", 900)

# Session TTL (how long a session cookie remains valid before re-login).
ACCOUNT_SESSION_TTL_SECONDS: int = _env_int("ALLOSTAT_ACCOUNT_SESSION_TTL_SECONDS", 2592000)

# Absolute session lifetime (Task 6, P2-4): a hard cap measured from the
# session's created_at, independent of the idle/sliding window above. Without
# this, a copied/stolen session cookie replayed periodically slides
# expires_at forward forever (web_auth.resolve_session's idle-timeout
# behavior) and never dies. resolve_session enforces BOTH: the idle window
# still governs day-to-day inactivity, but no session -- however actively
# used -- survives past created_at + this value. Default 90 days. Must be
# >= ACCOUNT_SESSION_TTL_SECONDS (an absolute cap shorter than the idle
# window would make the idle window meaningless).
ACCOUNT_SESSION_ABSOLUTE_MAX_SECONDS: int = _env_int(
    "ALLOSTAT_ACCOUNT_SESSION_ABSOLUTE_MAX_SECONDS", 7776000
)
if ACCOUNT_SESSION_ABSOLUTE_MAX_SECONDS < ACCOUNT_SESSION_TTL_SECONDS:
    raise ValueError(
        "ALLOSTAT_ACCOUNT_SESSION_ABSOLUTE_MAX_SECONDS "
        f"({ACCOUNT_SESSION_ABSOLUTE_MAX_SECONDS}) must be >= "
        "ALLOSTAT_ACCOUNT_SESSION_TTL_SECONDS "
        f"({ACCOUNT_SESSION_TTL_SECONDS}); an absolute cap shorter than the "
        "idle window would make the idle window meaningless."
    )

# Rate limit for login link requests (per IP). Max requests per window.
ACCOUNT_LINK_RATELIMIT_MAX: int = _env_int("ALLOSTAT_ACCOUNT_LINK_RATELIMIT_MAX", 5)

# Rate limit window (in seconds) for login link requests.
ACCOUNT_LINK_RATELIMIT_WINDOW_SECONDS: int = _env_int(
    "ALLOSTAT_ACCOUNT_LINK_RATELIMIT_WINDOW_SECONDS", 3600
)

# Task 4 (P1-3): retention window for the periodic `auth_attempts` prune
# (db.prune_stale_auth_attempts) — comfortably longer than the rate-limit
# window above so a bucket that is still being actively rolled is never
# pruned out from under a live limiter. Default 24h.
AUTH_ATTEMPTS_PRUNE_RETENTION_SECONDS: int = _env_int(
    "ALLOSTAT_AUTH_ATTEMPTS_PRUNE_RETENTION_SECONDS", 86400
)

# Base URL for passwordless account links (verification, login endpoints).
#
# C-2 (final review): every use of this knob builds a TOKENFUL link
# (`{SITE_BASE_URL}/account/verify?token=...`, `.../account/login?token=...`)
# that the server itself must resolve when the recipient clicks it — i.e. this
# is the APP origin where the `/account/*` routes actually live
# (mcp.allostat.ai), NOT the marketing/pricing origin (allostat.ai) the
# billing CORS policy (billing_cors.py) governs. The two origins are distinct
# and must not be conflated: a marketing-origin default here 404s every
# emailed link. The marketing origin for browser-facing redirects is sourced
# separately, from `billing_cors.resolve_browser_origin()`.
SITE_BASE_URL: str = _env_str("ALLOSTAT_SITE_BASE_URL", "https://mcp.allostat.ai")

# Deterministic per-message link-token HMAC secret (Task 3, P1-2). Retrying a
# login/verify delivery must re-derive the IDENTICAL raw token so an
# ambiguous provider outcome (timeout after accept, crash before our
# completion is recorded) never invalidates a link the customer already
# received -- see web_auth.mint_deterministic_link_token, which derives
# every raw token as HMAC(secret, f"{namespace}:{purpose}:{message_id}").
# No repo default is safe to ship for production (see
# validate_link_token_secret_or_raise below), but local/test runs default to
# a well-known non-secret placeholder so the dev server and the dual-backend
# test suite work without extra setup -- production must override this via
# the systemd EnvironmentFile.
# Sol re-audit P2-1: named module constant (not a repeated literal) so the
# startup gate below can compare against the EXACT default without re-typing
# it -- a placeholder that ever drifts out of sync with the gate would let a
# misconfigured production/staging deploy silently pass.
LINK_TOKEN_SECRET_PLACEHOLDER = "local-dev-test-only-link-token-secret"

LINK_TOKEN_SECRET: str = os.environ.get(
    "ALLOSTAT_LINK_TOKEN_SECRET", LINK_TOKEN_SECRET_PLACEHOLDER
)

# Sol re-audit P2-1: minimum acceptable secret length. HMAC-SHA256 keys
# shorter than this are brute-forceable well within a forgeable-link
# timeframe; 32 chars gives a reasonable floor for an operator-typed value
# (a generated secrets.token_urlsafe(32) is comfortably above it).
_LINK_TOKEN_SECRET_MIN_LENGTH = 32


# ---------------------------------------------------------------------------
# Per-env base URL validation (Task 2, P1-1 half 2).
#
# Every tokenful account link (verify/login) is built from SITE_BASE_URL and
# emailed to a real recipient, who then clicks it -- unlike every other
# namespace guard in this subsystem (web_auth.py's table filtering,
# lookup_user_id_by_email's C4 check), which only bites once a REQUEST
# reaches this server. A staging deploy that inherits (or is misconfigured
# with) the production SITE_BASE_URL default would mint a staging login/
# verify link that resolves to mcp.allostat.ai -- the PRODUCTION server --
# before any per-request namespace check ever runs. This validator closes
# that gap by fixing the expected host per namespace and failing closed on
# any mismatch.
#
# 'local' (dev/test) carries no fixed expected host -- those processes may
# point SITE_BASE_URL anywhere (a bare default, localhost, a throwaway test
# value) without tripping this gate; only 'production' and 'staging' have a
# durable, single correct answer worth enforcing.
# ---------------------------------------------------------------------------
_SITE_BASE_URL_EXPECTED_HOST: dict[str, str] = {
    "production": "mcp.allostat.ai",
    "staging": "mcp-staging.allostat.ai",
}


def validated_site_base_url(namespace: str) -> str:
    """Return SITE_BASE_URL, validated against the expected app host for
    `namespace`. Fails closed (RuntimeError) on a production/staging
    namespace whose SITE_BASE_URL host doesn't match the fixed expected
    host for that namespace, or whose scheme isn't https. A namespace with
    no fixed expected host (e.g. 'local') is returned unchecked.

    Callers (billing_delivery.py's render-time mint paths) must pass the
    SAME namespace they mint/scope the rest of the delivery to -- this is
    what stops a staging worker from ever emailing a production-hostname
    link (or vice versa).
    """
    expected_host = _SITE_BASE_URL_EXPECTED_HOST.get(namespace)
    if expected_host is None:
        return SITE_BASE_URL
    problems: list[str] = []
    try:
        parsed = urllib.parse.urlsplit(SITE_BASE_URL)
        # Sol re-audit P2-2: this used to check ONLY scheme=="https" and
        # hostname==expected_host, so e.g.
        # `https://mcp.allostat.ai:444/unexpected/path?x=1#fragment` PASSED --
        # then got concatenated with `/account/login|verify` into a broken,
        # noncanonical tokenful link. Assert every component: no userinfo, no
        # non-default port, an empty (or bare "/") path, no query, no fragment.
        # A bare trailing "/" is accepted and normalizes to "no path" -- both are
        # the same origin and either is safe to concatenate `/account/*` onto.
        if parsed.scheme != "https":
            problems.append(f"scheme must be 'https' (got {parsed.scheme!r})")
        if parsed.username is not None or parsed.password is not None:
            problems.append("must not carry userinfo/credentials")
        if parsed.hostname != expected_host:
            problems.append(
                f"host must be exactly {expected_host!r} (got {parsed.hostname!r})"
            )
        # .port raises ValueError for non-numeric or out-of-range ports
        if parsed.port not in (None, 443):
            problems.append(
                f"must not specify a non-default port (got {parsed.port!r})"
            )
        if parsed.path not in ("", "/"):
            problems.append(
                f"path must be empty or exactly '/' (got {parsed.path!r})"
            )
        if parsed.query:
            problems.append(f"must not carry a query string (got {parsed.query!r})")
        if parsed.fragment:
            problems.append(f"must not carry a fragment (got {parsed.fragment!r})")
    except ValueError as exc:
        # Preserve port-specific error details if the ValueError is about the port
        raise RuntimeError(
            f"ALLOSTAT_SITE_BASE_URL has invalid port for namespace={namespace!r}: "
            f"{SITE_BASE_URL!r}"
        ) from exc
    if problems:
        raise RuntimeError(
            f"ALLOSTAT_SITE_BASE_URL must be the exact canonical origin "
            f"'https://{expected_host}' when ALLOSTAT_ENV={namespace!r} (got "
            f"{SITE_BASE_URL!r}): "
            + "; ".join(problems)
            + ". A mismatch would mint a tokenful account link (verify/"
            "login) pointing at the wrong environment's server, or at a "
            "broken/noncanonical URL once /account/verify|login is "
            "concatenated onto it."
        )
    return SITE_BASE_URL


def validate_site_base_url_or_raise() -> None:
    """Production startup gate (main.build_app, alongside
    validate_billing_config_or_raise): fail loud at boot if SITE_BASE_URL
    doesn't resolve to the production app host. Reads ALLOSTAT_ENV live, same
    posture as every other startup gate in this module.

    Only production is wired into startup, per this task's scope -- staging's
    equivalent check runs via validated_site_base_url(...) at the call sites
    that actually build a tokenful link (billing_delivery.py), scoped to the
    live ALLOSTAT_ENV at send time. Every other namespace ('local', tests) is
    unchecked here, matching validated_site_base_url's own scope.
    """
    if os.environ.get("ALLOSTAT_ENV", "local") != "production":
        return
    validated_site_base_url("production")


def validate_link_token_secret_or_raise() -> None:
    """Production AND staging startup gate (main.build_app / the delivery
    worker CLI's _cmd_delivery_worker, alongside validate_site_base_url_or_
    raise): fail loud at boot if ALLOSTAT_LINK_TOKEN_SECRET is missing,
    blank, the well-known local/test placeholder, or trivially short. Same
    posture/placement as every other startup gate in this module -- inert
    outside production/staging, so every local/test run (including this test
    suite, which relies on the fixed placeholder default above) is
    unaffected. Both callers share this single function so the web and
    worker startup paths can never diverge.

    The deterministic mint (web_auth.mint_deterministic_link_token) derives
    every login/verify link's raw token as HMAC(secret, "namespace:purpose:
    message_id"). message_id is NOT secret (it doubles as the provider
    Idempotency-Key and can appear in provider-side logs/dashboards), so any
    of the following in production/staging would let anyone who learns a
    message_id forge a valid login/verify link -- this gate closes that hole
    the same way validate_billing_env_or_raise closes the blank Stripe
    webhook secret hole:
      - blank/missing (Sol P2-1: staging previously wasn't checked at all,
        so an unset staging secret silently fell back to the placeholder);
      - the EXACT built-in local/test placeholder (Sol P2-1's reproduction:
        a production deploy set to this well-known, repo-shipped literal
        previously passed the gate because the old check only asked "is it
        non-blank?", not "is it actually secret?");
      - trivially short -- a short HMAC key is brute-forceable well within a
        forgeable-link timeframe even if it isn't the literal placeholder.
    """
    env = os.environ.get("ALLOSTAT_ENV", "local")
    if env not in ("production", "staging"):
        return
    secret = os.environ.get("ALLOSTAT_LINK_TOKEN_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            f"ALLOSTAT_LINK_TOKEN_SECRET is missing or blank in {env}. "
            "Set a long random secret in the systemd EnvironmentFile "
            "(/etc/default/allostat-server) -- never in the repo -- before "
            "exposing the passwordless account routes."
        )
    if secret == LINK_TOKEN_SECRET_PLACEHOLDER:
        raise RuntimeError(
            f"ALLOSTAT_LINK_TOKEN_SECRET is set to the well-known local/"
            f"test placeholder in {env}. That value ships in the repo "
            "source and is guessable by anyone -- set a distinct, long "
            "random secret in the systemd EnvironmentFile "
            "(/etc/default/allostat-server) before exposing the "
            "passwordless account routes."
        )
    if len(secret) < _LINK_TOKEN_SECRET_MIN_LENGTH:
        raise RuntimeError(
            f"ALLOSTAT_LINK_TOKEN_SECRET is too short ({len(secret)} chars) "
            f"in {env} -- must be at least {_LINK_TOKEN_SECRET_MIN_LENGTH} "
            "characters. Set a long random secret (e.g. "
            "secrets.token_urlsafe(32)) in the systemd EnvironmentFile "
            "(/etc/default/allostat-server) before exposing the "
            "passwordless account routes."
        )


# ---------------------------------------------------------------------------
# Task 5 (2026-07-19, P1-2 drain-protocol -- Sol re-audit blocker):
# validate_link_secret_rotation_or_raise.
#
# Separate concern from validate_link_token_secret_or_raise above: that gate
# checks the ACTIVE secret's own strength (non-blank, non-placeholder, long
# enough). This gate checks a ROTATION of an otherwise-valid secret against
# in-flight deliveries.
#
# THE HAZARD: web_auth.mint_deterministic_link_token derives every raw
# login/verify token as HMAC(LINK_TOKEN_SECRET, "namespace:purpose:
# message_id") (Task 3, P1-2) so a RETRY of the SAME outbox row re-derives
# the IDENTICAL token -- but only as long as the secret itself hasn't
# changed. ALLOSTAT_LINK_TOKEN_SECRET is a stable env var, not a per-boot
# value, so this only fires on a DELIBERATE rotation: if an operator
# rotates it while a login_link_delivery_outbox or billing_delivery_outbox
# row is still pending, the next claim/retry of that row re-derives a
# DIFFERENT token under the new secret, and the provider's stable
# Idempotency-Key means the resend dedupes -- the customer's
# ALREADY-DELIVERED email is left pointing at a token that no longer
# verifies. This gate makes that transition impossible: the process simply
# refuses to boot under the new secret until the queue is drained.
#
# OPERATOR ROTATE PROCEDURE (safe order):
#   1. Stop intake of new deliveries (or simply let the existing queue run
#      down -- no new /account/request-link or checkout completions are
#      required to stop for this to work, since fp comparison only cares
#      about rows that exist at the moment of restart).
#   2. Let the delivery worker drain BOTH outboxes to a terminal state
#      (sent/dead) -- confirm via `db.count_pending_deliveries` or the M-02
#      operator alert surface.
#   3. Set the new ALLOSTAT_LINK_TOKEN_SECRET in the systemd EnvironmentFile.
#   4. Restart (web + worker). With the queue empty, this gate treats the
#      change as a SAFE rotation: it updates the stored fingerprint and
#      proceeds. Skipping step 2 (restarting into a non-empty queue) is
#      exactly the case this gate refuses -- it raises instead of booting.
#
# Namespace-scoped: fp is stored per db.current_namespace() key, so
# production and staging rotate independently (rotating one never touches
# the other's stored fingerprint or pending count).
#
# BOOTSTRAP ALSO FAILS CLOSED (Sol re-audit followup, 2026-07-19): the very
# first time this gate runs for a namespace there is by definition no
# stored fingerprint yet -- but that is NOT proof the pre-existing outboxes
# are empty, or that any rows in them were minted under the secret about to
# become the baseline. Deploying this gate onto an already-running install
# (server_kv is new, the outboxes are not) is exactly that shape. So a
# first boot with ANY non-terminal row in either outbox also raises instead
# of silently baselining -- only an empty queue may be adopted as the
# starting fingerprint, first boot or not.
# ---------------------------------------------------------------------------


def validate_link_secret_rotation_or_raise(conn) -> None:
    """Production AND staging startup gate (main.build_app / the delivery
    worker CLI's _cmd_delivery_worker), alongside validate_link_token_secret_
    or_raise -- but a DISTINCT check (see the module comment directly
    above): this one detects an unsafe SECRET ROTATION, not secret
    strength. Inert outside production/staging (same posture as every
    other startup gate in this module), so local/test runs never touch
    `conn` or `server_kv` at all.

    fp is a NON-secret, NON-reversible fingerprint
    (sha256(secret).hexdigest()[:16]) -- never the secret itself -- so a
    compromised DB dump can't recover ALLOSTAT_LINK_TOKEN_SECRET from it.
    """
    env = os.environ.get("ALLOSTAT_ENV", "local")
    if env not in ("production", "staging"):
        return
    from . import db  # local import: db.py has no reverse dependency on

    # config.py, but keeping the coupling lazy/one-directional here matches
    # this module's existing posture of staying import-light at module load.

    # Robustness (P1-2 followup): if the gate's own persistence table isn't
    # present, the schema hasn't been initialized yet (init-db hasn't run).
    # Skip cleanly rather than crash on a raw missing-table error -- a
    # not-yet-migrated DB has no pending deliveries to protect, and in every
    # real deploy init-db runs before serve, so this table exists and the
    # gate below runs in full. (build_app must not abort with an opaque
    # OperationalError from this gate, pre-empting the other startup gates.)
    if not db.server_kv_table_exists(conn):
        return

    namespace = db.current_namespace()
    secret = os.environ.get("ALLOSTAT_LINK_TOKEN_SECRET", "").strip()
    fp = hashlib.sha256(secret.encode()).hexdigest()[:16]

    stored = db.get_link_secret_fingerprint(conn, namespace=namespace)
    if stored == fp:
        return  # unchanged -- not a rotation.

    # Either this is a genuine first boot for this namespace (stored is
    # None) or a detected rotation (stored != fp). Both cases hinge on the
    # SAME question: could any in-flight row have been minted under a
    # secret other than the one about to become the baseline? A first boot
    # is NOT proof the answer is no -- "no fingerprint stored" only means
    # this gate has never run here before; it says nothing about whether
    # server_kv is new because the schema just landed on a long-lived DB
    # whose outboxes are NOT empty (first deploy of the gate onto an
    # existing install). So count pending in both cases and fail CLOSED
    # (Sol re-audit P1-2 followup) rather than treat "no fingerprint" as
    # license to silently baseline over pre-existing queued work.
    pending = db.count_pending_deliveries(conn, namespace=namespace)
    if pending > 0:
        noun, verb = ("delivery", "is") if pending == 1 else ("deliveries", "are")
        if stored is None:
            raise RuntimeError(
                f"no link secret fingerprint is stored yet for "
                f"namespace={namespace!r}, and {pending} {noun} {verb} "
                "already pending -- refusing to establish a baseline for "
                "ALLOSTAT_LINK_TOKEN_SECRET. A missing fingerprint is not "
                "proof these rows were minted under the current secret (the "
                "gate may simply be deploying for the first time onto an "
                "existing install); baselining now could let a retry "
                "re-derive a DIFFERENT token than what was already emailed. "
                "Drain the delivery queue (both outboxes reach a terminal "
                "sent/dead state) before this namespace may establish its "
                "baseline. See "
                "config.validate_link_secret_rotation_or_raise's docstring "
                "for the safe rotate procedure."
            )
        raise RuntimeError(
            f"link secret changed while {pending} {noun} {verb} still "
            f"pending in namespace={namespace!r} -- drain the delivery "
            "queue before rotating ALLOSTAT_LINK_TOKEN_SECRET. A retry of "
            "any pending row would re-derive a DIFFERENT token under the "
            "new secret while the provider's idempotency key dedupes the "
            "resend, leaving an already-emailed link pointing at a dead "
            "token. See config.validate_link_secret_rotation_or_raise's "
            "docstring for the safe rotate procedure."
        )
    # Queue is empty -- safe to (re)establish fp as the baseline, whether
    # this is a genuine first boot or a rotation. Nothing pending could have
    # its emailed link invalidated.
    db.set_link_secret_fingerprint(conn, namespace=namespace, fp=fp)
