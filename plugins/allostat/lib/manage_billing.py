#!/usr/bin/env python3
"""Open the authenticated Allostat customer billing portal.

The installed wrapper bearer is resolved through ``MCPClientConfig.from_env``.
It is attached only to an in-process ``Authorization`` header, and the request
uses the wrapper's existing per-call no-redirect opener. The returned portal
destination is validated before the system browser sees it.

This module is stdlib-only and deliberately accepts no command-line arguments:
credentials and portal URLs must never cross argv or stdout/stderr.
"""
from __future__ import annotations

import json
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit, urlunsplit

# The installed dispatcher deliberately launches operation scripts with
# ``python -P`` so neither the working directory nor an ambient script path can
# supply imports.  Resolve only this shipped sibling directory before importing
# the shared client; no plugin-root environment variable or user path is trusted.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from mcp_client import (  # noqa: E402
    MCPClientConfig,
    USER_AGENT,
    _is_loopback_http,
    _urlopen_no_redirect,
)


MAX_RESPONSE_BYTES = 64 * 1024


class ManageBillingError(RuntimeError):
    """Safe, user-displayable failure for the Manage Billing flow."""


def _invalid_api_endpoint() -> ManageBillingError:
    return ManageBillingError(
        "The configured Allostat server endpoint is not safe for billing access."
    )


def _portal_api_url(endpoint: str) -> str:
    """Derive ``/billing/portal`` without changing the endpoint authority.

    HTTPS is required except for the MCP client's existing exact-loopback HTTP
    development allowance. Userinfo, query/fragment components, whitespace,
    and backslashes are refused before the bearer-carrying request is built.
    """
    if not isinstance(endpoint, str) or not endpoint:
        raise _invalid_api_endpoint()
    if endpoint != endpoint.strip() or "\\" in endpoint:
        raise _invalid_api_endpoint()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in endpoint):
        raise _invalid_api_endpoint()

    if endpoint.startswith("https://"):
        pass
    elif endpoint.startswith("http://") and _is_loopback_http(endpoint):
        pass
    else:
        raise _invalid_api_endpoint()

    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        # Accessing .port raises for malformed/non-numeric authorities.
        parsed.port
    except (TypeError, ValueError):
        raise _invalid_api_endpoint() from None

    if (
        not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
    ):
        raise _invalid_api_endpoint()

    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[:-4]
    portal_path = f"{path}/billing/portal" if path else "/billing/portal"
    return urlunsplit((parsed.scheme, parsed.netloc, portal_path, "", ""))


def _invalid_portal_destination() -> ManageBillingError:
    return ManageBillingError(
        "The billing service returned an unsafe portal destination; nothing was opened."
    )


def _validate_portal_destination(value: Any) -> str:
    """Return an exact, unambiguous HTTPS URL or fail closed.

    The B-06 contract requires an HTTPS destination but does not freeze a
    provider hostname. This therefore validates the transport and authority
    rather than inventing a Stripe-host allowlist. Explicit ports, userinfo,
    fragments, encoded authority text, and parser-confusing characters are
    rejected. The original validated string is returned unchanged.
    """
    if not isinstance(value, str) or not value.startswith("https://"):
        raise _invalid_portal_destination()
    if value != value.strip() or "\\" in value:
        raise _invalid_portal_destination()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise _invalid_portal_destination()

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise _invalid_portal_destination() from None

    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or "%" in parsed.netloc
        or hostname.endswith(".")
        or parsed.netloc.casefold() != hostname.casefold()
    ):
        raise _invalid_portal_destination()
    return value


def _error_for_status(status: int) -> ManageBillingError:
    messages = {
        401: "Your saved Allostat credential was rejected. Re-activate Allostat and try again.",
        404: "No billing account is available for this subscription.",
        409: (
            "Allostat could not identify one unambiguous billing account. "
            "Contact support for help."
        ),
        429: "Billing access is temporarily rate-limited. Try again shortly.",
    }
    if status in messages:
        return ManageBillingError(messages[status])
    if status >= 500:
        return ManageBillingError("Billing is temporarily unavailable. Try again later.")
    return ManageBillingError("The billing request was rejected; nothing was opened.")


def _fetch_portal_destination(
    config: MCPClientConfig,
    *,
    urlopen: Callable[..., Any],
) -> str:
    bearer = (config.bearer_token or "").strip()
    if not bearer:
        raise ManageBillingError(
            "No installed Allostat credential was found. Activate Allostat and try again."
        )

    request = urllib_request.Request(
        _portal_api_url(config.endpoint),
        data=b"",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            status = int(getattr(response, "status", 0))
            if status != 200:
                raise _error_for_status(status)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except ManageBillingError:
        raise
    except urllib_error.HTTPError as exc:
        raise _error_for_status(int(exc.code)) from None
    except (urllib_error.URLError, TimeoutError, OSError):
        raise ManageBillingError(
            "Billing is temporarily unavailable. Check your connection and try again."
        ) from None
    except Exception:  # noqa: BLE001 - never surface transport/provider details
        raise ManageBillingError(
            "Billing is temporarily unavailable. Check your connection and try again."
        ) from None

    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
        raise ManageBillingError("The billing service returned an invalid response.")
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ManageBillingError("The billing service returned an invalid response.") from None
    if not isinstance(payload, dict):
        raise ManageBillingError("The billing service returned an invalid response.")
    return _validate_portal_destination(payload.get("url"))


def open_billing_portal(
    config: MCPClientConfig | None = None,
    *,
    harness: str = "claude",
    urlopen: Callable[..., Any] | None = None,
    browser_open: Callable[[str], Any] | None = None,
) -> None:
    """Request and open the portal, or raise a safe ``ManageBillingError``."""
    resolved = (
        config
        if config is not None
        else MCPClientConfig.from_env(harness=harness)
    )
    opener = urlopen if urlopen is not None else _urlopen_no_redirect
    destination = _fetch_portal_destination(resolved, urlopen=opener)
    launch = browser_open if browser_open is not None else webbrowser.open_new_tab
    try:
        opened = launch(destination)
    except Exception:  # noqa: BLE001 - never echo browser/provider details
        raise ManageBillingError(
            "The secure billing portal could not be opened in your browser."
        ) from None
    if opened is not True:
        raise ManageBillingError(
            "The secure billing portal could not be opened in your browser."
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="allostat-manage-billing")
    parser.add_argument("--harness", choices=("claude", "codex"), default="claude")
    args, extra = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if extra:
        print(
            "Manage Billing does not accept arguments beyond the installed harness selection.",
            file=sys.stderr,
        )
        return 2
    try:
        open_billing_portal(harness=args.harness)
    except ManageBillingError as exc:
        print(f"Allostat: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - fail closed without secret-bearing traceback
        print("Allostat could not open billing safely; nothing was opened.", file=sys.stderr)
        return 1
    print("Opened the secure Allostat billing portal in your browser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
