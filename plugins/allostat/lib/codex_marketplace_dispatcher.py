# allostat-dispatcher-ownership: owner=allostat kind=codex-marketplace-dispatcher format=1
"""Stable bootstrap for Codex marketplace hooks.

`hooks.json` invokes one PATH token: ``allostat-hook``.  Activation installs
this stdlib-only module into the selected Python's user site.  The module asks
Codex itself which Allostat plugin is currently installed, validates the
reported directory against its manifest, and runs the hook from that directory.
No marketplace cache spelling or release version is baked into the launcher.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

PLUGIN_ID = "allostat@allostat"
PLUGIN_NAME = "allostat"
MARKETPLACE_NAME = "allostat"
CODEX_LIST_COMMAND = ["codex", "plugin", "list", "--json"]
EVENT_SCRIPTS = {
    "session-start": "session-start.py",
    "user-prompt-submit": "user-prompt-submit.py",
    "pre-tool-use": "pre-tool-use.py",
    "stop": "stop.py",
}
LOCAL_SERVER_SCRIPTS = {
    "memory-upsert-mcp": "../lib/topical_memory_mcp.py",
}
LOCATOR_PATH_PARTS = (".allostat", "codex", "plugin_locator.json")


class DispatchError(RuntimeError):
    """The current marketplace plugin cannot be identified safely."""


def _validated_plugin_root(path: Path, *, expected_version: str | None = None) -> Path:
    if not path.is_absolute():
        raise DispatchError(f"Codex reported a relative plugin path: {path}")
    root = path.resolve()
    manifest_path = root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"Allostat plugin manifest is unreadable: {manifest_path}") from exc
    if manifest.get("name") != PLUGIN_NAME:
        raise DispatchError(f"unexpected plugin manifest name at {manifest_path}")
    if expected_version is not None and manifest.get("version") != expected_version:
        raise DispatchError(
            "Codex plugin inventory and the installed Allostat manifest disagree "
            f"on version ({expected_version!r} != {manifest.get('version')!r})"
        )
    return root


def _installed_plugin_root(version: str, *, codex_home: Path | None = None) -> Path:
    if not version or version in {".", ".."} or "/" in version or "\\" in version:
        raise DispatchError(f"Codex reported an unsafe plugin version: {version!r}")
    if codex_home is None:
        configured = os.environ.get("CODEX_HOME")
        codex_home = Path(configured) if configured else Path.home() / ".codex"
    codex_home = Path(codex_home).expanduser()
    if not codex_home.is_absolute():
        raise DispatchError(f"CODEX_HOME is not absolute: {codex_home}")
    return (
        codex_home
        / "plugins"
        / "cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
        / version
    )


def resolve_plugin_root(
    *, run: Callable = subprocess.run, codex_home: Path | None = None
) -> Path:
    """Return the installed root for the one enabled Allostat inventory row."""
    try:
        completed = run(
            CODEX_LIST_COMMAND,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DispatchError("could not query Codex's installed plugin inventory") from exc
    if completed.returncode != 0:
        raise DispatchError(
            "Codex plugin inventory failed: " + (completed.stderr or "unknown error").strip()
        )
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DispatchError("Codex returned malformed plugin inventory JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), list):
        raise DispatchError("Codex returned malformed plugin inventory structure")
    installed = payload["installed"]
    if any(not isinstance(item, dict) for item in installed):
        raise DispatchError("Codex returned malformed plugin inventory entry")

    matches = [
        item
        for item in installed
        if item.get("pluginId") == PLUGIN_ID
        and item.get("name") == PLUGIN_NAME
        and item.get("marketplaceName") == MARKETPLACE_NAME
        and item.get("installed") is True
        and item.get("enabled") is True
    ]
    if len(matches) != 1:
        raise DispatchError(
            f"expected one enabled {PLUGIN_ID} installation; Codex reported {len(matches)}"
        )
    item = matches[0]
    version = item.get("version")
    if not isinstance(version, str):
        raise DispatchError("Codex's Allostat inventory entry is missing version authority")
    installed_root = _installed_plugin_root(version, codex_home=codex_home)
    return _validated_plugin_root(installed_root, expected_version=version)


def resolve_event_script(plugin_root: Path, event: str) -> Path:
    name = EVENT_SCRIPTS.get(event) or LOCAL_SERVER_SCRIPTS.get(event)
    if name is None:
        raise DispatchError(
            f"unknown event {event!r}; expected one of "
            f"{tuple(EVENT_SCRIPTS) + tuple(LOCAL_SERVER_SCRIPTS)}"
        )
    script = (plugin_root / "hooks" / name).resolve()
    if not script.is_relative_to(plugin_root.resolve()):
        raise DispatchError("installed Allostat event escaped the plugin root")
    if not script.is_file():
        raise DispatchError(f"installed Allostat hook is missing: {script}")
    return script


def _locator_path() -> Path:
    return Path.home().joinpath(*LOCATOR_PATH_PARTS)


def _write_locator(root: Path) -> None:
    try:
        path = _locator_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = json.loads(
            (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        body = (
            json.dumps({"path": str(root), "version": manifest["version"]}, indent=2)
            + "\n"
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                # allostat:destructive-ok tmp_name is the private file returned by mkstemp beside the locator.
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DispatchError("could not update the installed plugin locator") from exc


def _read_locator() -> Path:
    path = _locator_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_path = data["path"]
        version = data["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DispatchError("cached Allostat plugin locator is unavailable") from exc
    if not isinstance(raw_path, str) or not isinstance(version, str):
        raise DispatchError("cached Allostat plugin locator is malformed")
    return _validated_plugin_root(Path(raw_path), expected_version=version)


def current_plugin_root(event: str, *, run: Callable = subprocess.run) -> Path:
    # Marketplace upgrades are documented to require a restart.  SessionStart
    # therefore refreshes the authority once; high-frequency tool hooks use the
    # validated durable locator rather than spawning a Codex CLI per tool call.
    if event == "session-start":
        root = resolve_plugin_root(run=run)
        _write_locator(root)
        return root
    try:
        return _read_locator()
    except DispatchError:
        root = resolve_plugin_root(run=run)
        _write_locator(root)
        return root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plugin_local = bool(args and args[0] == "--current-plugin-root")
    if plugin_local:
        args = args[1:]
    if len(args) != 3 or args[1:] != ["--harness", "codex"]:
        print(
            "usage: allostat-hook <session-start|user-prompt-submit|pre-tool-use|stop|memory-upsert-mcp> "
            "--harness codex",
            file=sys.stderr,
        )
        return 2
    event = args[0]
    try:
        # The durable bootstrap already resolved the current snapshot and
        # invokes this exact file from within it.  Re-validating that immutable
        # snapshot avoids a second Codex inventory process without accepting an
        # argv-selected path.  Direct invocations retain the full locator/
        # inventory path.
        root = (
            _validated_plugin_root(Path(__file__).resolve().parent.parent)
            if plugin_local
            else current_plugin_root(event)
        )
        script = resolve_event_script(root, event)
    except DispatchError as exc:
        print(f"allostat-hook: {exc}", file=sys.stderr)
        return 3
    try:
        if event == "memory-upsert-mcp":
            # MCP stdio is a long-lived duplex stream. Reading stdin to EOF
            # before launching (correct for one-shot hooks) deadlocks it.
            completed = subprocess.run(
                [sys.executable, "-P", str(script), "--harness", "codex"],
            )
        else:
            completed = subprocess.run(
                [sys.executable, "-P", str(script), "--harness", "codex"],
                input=sys.stdin.buffer.read(),
            )
    except OSError as exc:
        print(f"allostat-hook: could not start {script}: {exc}", file=sys.stderr)
        return 4
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
