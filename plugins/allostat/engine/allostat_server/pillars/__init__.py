"""Pillar MCP tools (γ + later).

Each pillar exposes one or more MCP tools following the β per-call
context protocol (ExcerptPayload + QuestionPayload → PillarToolResponse).
Pillars are registered onto the FastMCP server in mcp_handlers.build_mcp_server.

γ ports the three critical pillars: stress-response, hypothalamic-axis,
innate-enforcer. δ/ε/ζ port the rest.

Each pillar module defines `register_tools(server: FastMCP) -> None`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import (
    hypothalamic_axis,
    innate_enforcer,
    metabolism,
    onboarding_interview,
    pattern_observer,
    recall_silos,
    stress_response,
    voice_keeper,
    volume_control,
    watchdog_server,
)


def register_all_pillars(server: FastMCP) -> None:
    """Wire every γ-onward pillar's MCP tools onto the FastMCP server.

    Call from mcp_handlers.build_mcp_server after the base tools (e.g.,
    health_check) are registered. Each pillar module owns its own
    `register_tools` so adding a new pillar is one import + one call.
    """
    innate_enforcer.register_tools(server)
    stress_response.register_tools(server)
    hypothalamic_axis.register_tools(server)
    pattern_observer.register_tools(server)
    recall_silos.register_tools(server)
    voice_keeper.register_tools(server)
    onboarding_interview.register_tools(server)
    volume_control.register_tools(server)
    metabolism.register_tools(server)
    watchdog_server.register_tools(server)
