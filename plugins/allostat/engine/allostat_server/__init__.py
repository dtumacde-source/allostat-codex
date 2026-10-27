"""Allostat v2.40 hosted MCP server.

The v2.40 hosted-MCP build that anchors the v2.4-v2.5 patent-filing cycle.
Companion to the Claude Code plugin (v2.3 reference state at the
`allostat` repo, tag `v2.3-reference-state-20260513`).

Package layout:
    main.py            uvicorn entry point + CLI subcommands
    auth.py            Bearer token middleware
    mcp_handlers.py    MCP tool surface (Phase 1: health_check only)
    rate_limit.py      token bucket (30/sec) + daily counter (5000/day)
    db.py              SQLite schema + queries
    logging.py         structured per-request logger
"""
from __future__ import annotations

__version__ = "0.2.9"
__build_label__ = "v2.40-architecture-c"
__build_started__ = "2026-05-13"
