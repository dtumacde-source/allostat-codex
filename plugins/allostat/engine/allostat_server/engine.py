"""The existing tool/resource surface, independent of the hosted service.

Both transports register these exact functions. There is no second copy of
the regulator, classifiers, schemas, rules, or response normalization.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __build_label__, __version__
from ._time import utc_now_iso
from .dispatch_tools import register_dispatch_tools
from .pillars import register_all_pillars
from .pillars.rules_library import register_resources


def register_engine(server: FastMCP) -> None:
    register_all_pillars(server)
    register_resources(server)
    register_dispatch_tools(server)


def engine_health(api_key_prefix: str) -> dict[str, Any]:
    from .pillars.innate_enforcer import dropped_rules

    try:
        drops = dropped_rules()
    except Exception:
        drops = [{"id": "(unknown)", "reason": "dropped-rules probe itself failed"}]
    return {
        "server_version": __version__,
        "build_label": __build_label__,
        "server_time_utc": utc_now_iso(),
        "api_key_prefix": api_key_prefix,
        "innate_ruleset_whole": not drops,
        "innate_dropped_rules": drops,
    }


def build_local_server() -> FastMCP:
    """Construct a stdio/in-process engine; never opens a listener or database."""
    server = FastMCP("allostat")
    register_engine(server)

    @server.tool(name="health_check", description="Verify the local Allostat engine and rules.")
    def health_check() -> dict[str, Any]:
        return engine_health("local")

    return server
