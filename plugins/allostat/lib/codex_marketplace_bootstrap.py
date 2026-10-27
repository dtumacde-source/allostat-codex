# allostat-dispatcher-ownership: owner=allostat kind=codex-marketplace-dispatcher format=1
"""Upgrade-stable Codex hook bootstrap.

This is the durable file copied to ``~/.allostat/codex/bin``.  It deliberately
contains no hook or local-server route table.  It resolves and validates the
currently enabled marketplace snapshot, then delegates every argv contract to
that snapshot's dispatcher.  Route additions therefore ship in the replaceable
plugin rather than becoming trapped in this durable copy.
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
LOCATOR_PATH_PARTS = (".allostat", "codex", "plugin_locator.json")
_KNOWN_CACHED_EVENTS = frozenset(
    {"user-prompt-submit", "pre-tool-use", "stop"}
)
_TARGET = Path("lib/codex_marketplace_dispatcher.py")


class BootstrapError(RuntimeError):
    """The active marketplace dispatcher cannot be identified safely."""


def _validated_plugin_root(path: Path, *, expected_version: str | None = None) -> Path:
    if not path.is_absolute():
        raise BootstrapError(f"Codex reported a relative plugin path: {path}")
    root = path.resolve()
    manifest_path = root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"Allostat plugin manifest is unreadable: {manifest_path}") from exc
    if manifest.get("name") != PLUGIN_NAME:
        raise BootstrapError(f"unexpected plugin manifest name at {manifest_path}")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise BootstrapError(f"plugin manifest has no version authority: {manifest_path}")
    if expected_version is not None and version != expected_version:
        raise BootstrapError(
            "Codex plugin inventory and manifest disagree on version "
            f"({expected_version!r} != {version!r})"
        )
    return root


def _installed_plugin_root(version: str, *, codex_home: Path | None = None) -> Path:
    if not version or version in {".", ".."} or "/" in version or "\\" in version:
        raise BootstrapError(f"Codex reported an unsafe plugin version: {version!r}")
    if codex_home is None:
        configured = os.environ.get("CODEX_HOME")
        codex_home = Path(configured) if configured else Path.home() / ".codex"
    codex_home = Path(codex_home).expanduser()
    if not codex_home.is_absolute():
        raise BootstrapError(f"CODEX_HOME is not absolute: {codex_home}")
    return codex_home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version


def resolve_plugin_root(*, run: Callable = subprocess.run, codex_home: Path | None = None) -> Path:
    try:
        completed = run(CODEX_LIST_COMMAND, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("could not query Codex's installed plugin inventory") from exc
    if completed.returncode != 0:
        raise BootstrapError(
            "Codex plugin inventory failed: " + (completed.stderr or "unknown error").strip()
        )
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BootstrapError("Codex returned malformed plugin inventory JSON") from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list) or any(not isinstance(item, dict) for item in installed):
        raise BootstrapError("Codex returned malformed plugin inventory structure")
    matches = [
        item for item in installed
        if item.get("pluginId") == PLUGIN_ID
        and item.get("name") == PLUGIN_NAME
        and item.get("marketplaceName") == MARKETPLACE_NAME
        and item.get("installed") is True
        and item.get("enabled") is True
    ]
    if len(matches) != 1:
        raise BootstrapError(
            f"expected one enabled {PLUGIN_ID} installation; Codex reported {len(matches)}"
        )
    version = matches[0].get("version")
    if not isinstance(version, str):
        raise BootstrapError("Codex's Allostat inventory entry is missing version authority")
    return _validated_plugin_root(
        _installed_plugin_root(version, codex_home=codex_home), expected_version=version
    )


def _locator_path() -> Path:
    return Path.home().joinpath(*LOCATOR_PATH_PARTS)


def _write_locator(root: Path) -> None:
    path = _locator_path()
    try:
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        body = json.dumps({"path": str(root), "version": manifest["version"]}, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                # allostat:destructive-ok -- private mkstemp sibling owned by this write.
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BootstrapError("could not update the installed plugin locator") from exc


def _read_locator() -> Path:
    try:
        data = json.loads(_locator_path().read_text(encoding="utf-8"))
        raw_path, version = data["path"], data["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BootstrapError("cached Allostat plugin locator is unavailable") from exc
    if not isinstance(raw_path, str) or not isinstance(version, str):
        raise BootstrapError("cached Allostat plugin locator is malformed")
    return _validated_plugin_root(Path(raw_path), expected_version=version)


def current_plugin_root(route: str) -> Path:
    # Only the three long-established high-frequency hooks use the locator.
    # SessionStart and every unknown/future route query current inventory, so
    # a new route can never be held hostage by an old locator or route table.
    if route in _KNOWN_CACHED_EVENTS:
        try:
            return _read_locator()
        except BootstrapError:
            pass
    root = resolve_plugin_root()
    _write_locator(root)
    return root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("allostat-hook: missing dispatcher arguments", file=sys.stderr)
        return 2
    try:
        root = current_plugin_root(args[0])
        target = (root / _TARGET).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise BootstrapError(f"installed Allostat dispatcher is missing: {target}")
        completed = subprocess.run(
            [sys.executable, "-P", str(target), "--current-plugin-root", *args]
        )
    except BootstrapError as exc:
        print(f"allostat-hook: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"allostat-hook: could not start current dispatcher: {exc}", file=sys.stderr)
        return 4
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
