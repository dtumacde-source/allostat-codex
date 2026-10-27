#!/usr/bin/env python3
"""Small stdlib-only MCP stdio server for controlled topical-memory upserts."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import topical_memory_records


TOOL = "allostat_memory_upsert_v1"


def _project_root() -> Path:
    from local_state import resolve_project_root

    root = resolve_project_root(Path.cwd())
    return Path(root or Path.cwd()).resolve()


def _paths(project: Path) -> tuple[Path, Path]:
    from local_state import resolve_state_dir
    from session_handoff import resolve_memory_root

    return resolve_memory_root(project), resolve_state_dir(project)


def invoke(arguments: dict, *, request_id: str | int | None = None, harness: str = "unknown") -> dict:
    del request_id
    if (
        not isinstance(arguments, dict)
        or "record" not in arguments
        or not set(arguments).issubset({"record", "cue_takeover_expected_sha256"})
        or not isinstance(arguments.get("record"), dict)
    ):
        raise topical_memory_records.ValidationError("record is required")
    record = topical_memory_records.validate_envelope(arguments["record"])
    project = _project_root()
    memory, _ = _paths(project)
    provenance = {
        "provenance_trust": "model_asserted",
        "memory_reporter": "local-updater-unverified-harness",
        "harness": harness if harness in {"codex", "claude"} else "unknown",
        "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return topical_memory_records.upsert_record(
        memory, record, provenance,
        cue_takeover_expected_sha256=arguments.get("cue_takeover_expected_sha256"),
    )


def _tool_definition() -> dict:
    return {
        "name": TOOL,
        "description": "Upsert one typed topical-memory record with model-asserted provenance.",
        "inputSchema": {
            "type": "object", "additionalProperties": False, "required": ["record"],
            "properties": {
                "record": topical_memory_records.tool_record_schema(),
                "cue_takeover_expected_sha256": {
                    "type": "string", "pattern": "^[0-9a-f]{64}$",
                    "description": "Explicitly adopt an existing curated cue only when this SHA-256 matches its current text.",
                },
            },
        },
    }


def _reply(request: dict, *, harness: str = "unknown") -> dict | None:
    method = request.get("method")
    req_id = request.get("id")
    if method == "notifications/initialized": return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
            "serverInfo": {"name": "allostat-memory-local", "version": "1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [_tool_definition()]}}
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != TOOL:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown tool"}}
        try:
            result = invoke(params.get("arguments") or {}, request_id=req_id, harness=harness)
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
                "structuredContent": result,
            }}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": f"memory upsert refused: {type(exc).__name__}"}],
            }}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method not found"}}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    harness = "unknown"
    if len(args) == 2 and args[0] == "--harness" and args[1] in {"codex", "claude"}:
        harness = args[1]
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = _reply(request, harness=harness)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid request"}}
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
