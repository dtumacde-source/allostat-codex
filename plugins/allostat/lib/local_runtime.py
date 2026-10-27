"""In-process and stdio adapters for the unchanged Allostat decision engine."""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path


def _load_engine():
    plugin = Path(__file__).resolve().parent.parent
    # Isolated stdio startup (-I/-P) omits the script directory. The local engine
    # must consult the same own-package owner policy as hooks and status, not
    # silently fall back to enabled defaults because its sibling is unimportable.
    library = plugin / 'lib'
    if str(library) not in sys.path:
        sys.path.insert(0, str(library))
    import guard_settings
    if Path(guard_settings.__file__).resolve() != (library / 'guard_settings.py').resolve():
        raise RuntimeError('A different Allostat policy module is already loaded; restart the plugin')
    # Installed package first; source checkout only for development. Never
    # search cwd or a user's Python site-packages for a different engine.
    roots = (plugin / "engine",) if (plugin / "runtime.json").exists() else (
        plugin / "engine", plugin.parent / "server"
    )
    for root in roots:
        if (root / "allostat_server" / "engine.py").is_file():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            import allostat_server
            if Path(allostat_server.__file__).resolve().parent != (root / "allostat_server").resolve():
                raise RuntimeError("A different Allostat engine is already loaded; restart the plugin")
            from allostat_server.engine import build_local_server
            from allostat_server import runtime_context
            return build_local_server, runtime_context
    raise RuntimeError("The bundled Allostat engine is missing; repair the plugin installation")


class LocalClient:
    def __init__(self):
        build, self.context = _load_engine()
        self.server = build()

    def _run(self, operation, *args, request_id=None):
        import anyio
        from contextlib import nullcontext
        try:
            from guard_settings import policy_scope
        except Exception:
            policy_scope = nullcontext

        local_token = self.context.local_execution.set(True)
        request_token = self.context.current_request_id.set(request_id)
        try:
            with policy_scope():
                return anyio.run(operation, *args)
        finally:
            self.context.current_request_id.reset(request_token)
            self.context.local_execution.reset(local_token)

    def call_tool(self, name, arguments, *, request_id=None):
        result = self._run(self.server.call_tool, name, arguments, request_id=request_id)
        # FastMCP's annotated model return supplies (content, structured).
        # Use the same first text block as the existing HTTP wrapper parser.
        if isinstance(result, tuple):
            result = result[0]
        if isinstance(result, dict):
            return result
        if not result:
            return {}
        first = result[0].model_dump(mode="json")
        if first.get("type") == "text":
            try:
                return json.loads(first.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                return {"text": first.get("text", "")}
        return first

    def read_resource(self, uri, *, request_id=None):
        result = list(self._run(self.server.read_resource, uri, request_id=request_id))
        return result[0].content if result else ""


@lru_cache(maxsize=1)
def require_ready() -> None:
    """Never turn on unregulated memory injection if the engine is broken."""
    health = LocalClient().call_tool("health_check", {})
    if not health.get("innate_ruleset_whole"):
        raise RuntimeError("Local Allostat rules are incomplete; repair the plugin installation")


def main() -> int:
    build, context = _load_engine()
    token = context.local_execution.set(True)
    try:
        build().run(transport="stdio")
    finally:
        context.local_execution.reset(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
