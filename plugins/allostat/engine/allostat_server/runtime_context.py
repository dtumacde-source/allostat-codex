"""Execution context shared by hosted and local engine adapters.

Local execution is scoped to a call (or a stdio process), never a global
subscription override. Decision functions and their inputs stay unchanged.
"""
from __future__ import annotations

import contextvars
import json
import sys
from datetime import datetime

local_execution: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "allostat_local_execution", default=False
)
current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "allostat_current_request_id", default=None
)
local_dedup_state: contextvars.ContextVar[dict[tuple[str, str, str], datetime] | None] = contextvars.ContextVar(
    "allostat_local_dedup_state", default=None
)


def _emit_log_line(payload: dict, stream=None) -> None:
    """Keep diagnostics off the hook/MCP stdout protocol in local execution."""
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    out = stream if stream is not None else (
        sys.stderr if local_execution.get() else sys.stdout
    )
    print(line, file=out, flush=True)
