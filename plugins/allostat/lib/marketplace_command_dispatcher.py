# allostat-dispatcher-ownership: owner=allostat kind=marketplace-command-dispatcher format=1
"""Stable installed-snapshot dispatcher for customer commands.

Command Markdown is rendered into a model-driven shell where neither
``CLAUDE_PLUGIN_ROOT`` nor ``ALLOSTAT_PLUGIN_DIR`` exists.  This stdlib-only
bootstrap lives on PATH outside replaceable marketplace snapshots, resolves
the harness's authoritative current installation for every invocation, and
dispatches only an explicit operation allowlist.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable


CLAUDE_PLUGIN_PREFIX = "allostat-mcp@"
CLAUDE_PLUGIN_NAME = "allostat-mcp"
CODEX_PLUGIN_ID = "allostat@allostat"
CODEX_PLUGIN_NAME = "allostat"
CODEX_MARKETPLACE_NAME = "allostat"
CODEX_LIST_COMMAND = ["codex", "plugin", "list", "--json"]

# operation -> (relative script, forced argv prefix).  Caller arguments are
# appended verbatim; no operation can select an arbitrary file or module.
OPERATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "health": ("lib/memory_lifecycle.py", ("archipelago",)),
    "forget": ("lib/retraction.py", ()),
    "tend": ("lib/memory_lifecycle.py", ()),
    "promote-review": ("lib/proposal_review.py", ()),
    "promote-write": ("lib/learned_rule_writer.py", ()),
    "prune": ("lib/pruning.py", ()),
    "purge": ("lib/memory_lifecycle.py", ("sweep",)),
    "recall-restore": ("lib/pruning.py", ("restore",)),
    "record-migration": ("lib/record_migration.py", ()),
    "manage-billing": ("lib/manage_billing.py", ()),
}
LOCAL_OPERATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "guards": ("lib/guard_settings.py", ()),
    # Local-mode MCP processes deliberately enter through the stable launcher.
    # The launcher owns the isolated interpreter and resolves the currently
    # installed snapshot before this allowlist selects a fixed script.
    "local-mcp": ("lib/local_runtime.py", ()),
    "memory-mcp": ("lib/topical_memory_mcp.py", ()),
    "hook-session-start": ("hooks/session-start.py", ()),
    "hook-user-prompt-submit": ("hooks/user-prompt-submit.py", ()),
    "hook-pre-tool-use": ("hooks/pre-tool-use.py", ()),
    "hook-post-tool-use": ("hooks/post-tool-use.py", ()),
    "hook-stop": ("hooks/stop.py", ()),
}
HARNESS_AWARE_OPERATIONS = frozenset({
    "forget", "manage-billing", "memory-mcp",
    "hook-session-start", "hook-user-prompt-submit", "hook-pre-tool-use",
    "hook-post-tool-use", "hook-stop",
})


class DispatchError(RuntimeError):
    """The active marketplace snapshot or requested operation is unsafe."""


def _manifest_path(root: Path, harness: str) -> Path:
    if harness == "claude":
        return root / "plugin.json"
    if harness == "codex":
        return root / ".codex-plugin" / "plugin.json"
    raise DispatchError(f"unknown harness {harness!r}")


def _validated_plugin_root(
    path: Path, *, harness: str, expected_version: str | None = None
) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise DispatchError(f"{harness} reported a relative plugin path: {path}")
    root = path.resolve()
    manifest_path = _manifest_path(root, harness)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"Allostat plugin manifest is unreadable: {manifest_path}") from exc
    expected_name = CLAUDE_PLUGIN_NAME if harness == "claude" else CODEX_PLUGIN_NAME
    if manifest.get("name") != expected_name:
        raise DispatchError(f"unexpected plugin manifest name at {manifest_path}")
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise DispatchError(f"plugin manifest has no version authority: {manifest_path}")
    if expected_version is not None and version != expected_version:
        raise DispatchError(
            f"{harness} inventory and manifest disagree on version "
            f"({expected_version!r} != {version!r})"
        )
    return root


def _claude_plugins_file(home: Path | None = None) -> Path:
    if home is None and os.environ.get("CLAUDE_CONFIG_DIR"):
        config = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
        if not config.is_absolute():
            raise DispatchError("CLAUDE_CONFIG_DIR is not absolute")
        return config / "plugins" / "installed_plugins.json"
    base = Path.home() if home is None else Path(home).expanduser()
    if not base.is_absolute():
        raise DispatchError(f"home is not absolute: {base}")
    return base / ".claude" / "plugins" / "installed_plugins.json"


def resolve_claude_plugin_root(*, home: Path | None = None) -> Path:
    registry = _claude_plugins_file(home)
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"Claude plugin inventory is unreadable: {registry}") from exc
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, dict):
        raise DispatchError("Claude returned malformed plugin inventory structure")

    candidates: list[Path] = []
    for key, raw_records in plugins.items():
        if not isinstance(key, str) or not key.startswith(CLAUDE_PLUGIN_PREFIX):
            continue
        records = raw_records if isinstance(raw_records, list) else [raw_records]
        for record in records:
            if not isinstance(record, dict):
                raise DispatchError("Claude returned malformed Allostat inventory entry")
            install_path = record.get("installPath")
            version = record.get("version")
            if not isinstance(install_path, str) or not install_path:
                raise DispatchError("Claude Allostat inventory is missing installPath authority")
            if version is not None and not isinstance(version, str):
                raise DispatchError("Claude Allostat inventory has malformed version authority")
            candidates.append(
                _validated_plugin_root(
                    Path(install_path), harness="claude", expected_version=version
                )
            )
    roots = {str(path): path for path in candidates}
    if len(roots) != 1:
        raise DispatchError(
            f"expected one active Claude Allostat installation; inventory resolved {len(roots)}"
        )
    return next(iter(roots.values()))


def _installed_codex_root(version: str, *, codex_home: Path | None = None) -> Path:
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
        / CODEX_MARKETPLACE_NAME
        / CODEX_PLUGIN_NAME
        / version
    )


def resolve_codex_plugin_root(
    *, run: Callable = subprocess.run, codex_home: Path | None = None
) -> Path:
    try:
        completed = run(CODEX_LIST_COMMAND, capture_output=True, text=True, timeout=15)
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
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list) or any(not isinstance(row, dict) for row in installed):
        raise DispatchError("Codex returned malformed plugin inventory structure")
    matches = [
        row
        for row in installed
        if row.get("pluginId") == CODEX_PLUGIN_ID
        and row.get("name") == CODEX_PLUGIN_NAME
        and row.get("marketplaceName") == CODEX_MARKETPLACE_NAME
        and row.get("installed") is True
        and row.get("enabled") is True
    ]
    if len(matches) != 1:
        raise DispatchError(
            f"expected one enabled {CODEX_PLUGIN_ID} installation; Codex reported {len(matches)}"
        )
    version = matches[0].get("version")
    if not isinstance(version, str):
        raise DispatchError("Codex's Allostat inventory entry is missing version authority")
    return _validated_plugin_root(
        _installed_codex_root(version, codex_home=codex_home),
        harness="codex",
        expected_version=version,
    )


def resolve_plugin_root(
    harness: str,
    *,
    run: Callable = subprocess.run,
    codex_home: Path | None = None,
    home: Path | None = None,
) -> Path:
    if harness == "claude":
        return resolve_claude_plugin_root(home=home)
    if harness == "codex":
        return resolve_codex_plugin_root(run=run, codex_home=codex_home)
    raise DispatchError(f"unknown harness {harness!r}")


def resolve_operation(plugin_root: Path, operation: str) -> tuple[Path, tuple[str, ...]]:
    if operation == "manage-billing" and _is_local_snapshot(plugin_root):
        raise DispatchError("Local Allostat is activation-free; billing is not a local operation")
    contract = OPERATIONS.get(operation)
    if contract is None and _is_local_snapshot(plugin_root):
        contract = LOCAL_OPERATIONS.get(operation)
    if contract is None:
        raise DispatchError(f"unknown customer operation {operation!r}")
    relative, prefix = contract
    script = plugin_root / Path(relative)
    if not script.is_file():
        raise DispatchError(f"installed Allostat operation is missing: {script}")
    return script, prefix


def _is_local_snapshot(plugin_root: Path) -> bool:
    marker = plugin_root / "runtime.json"
    if not marker.exists():
        return False
    try:
        if marker.is_symlink() or json.loads(marker.read_text(encoding="utf-8")) != {"runtime": "local"}:
            raise DispatchError("installed local runtime marker is invalid")
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError("installed local runtime marker is unreadable") from exc
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "--harness" or args[1] not in {"claude", "codex"}:
        print(
            "usage: allostat-command --harness <claude|codex> <operation> [args...]",
            file=sys.stderr,
        )
        return 2
    harness, operation, rest = args[1], args[2], args[3:]
    try:
        root = resolve_plugin_root(harness)
        python_argv = [sys.executable, "-B", "-P"] if _is_local_snapshot(root) else [sys.executable, "-P"]
        # Activation copies this file under the durable name
        # `allostat-command[.py]`.  That copy must not own the operation
        # allowlist: forward the unchanged argv to the current snapshot's
        # dispatcher, where additions arrive with marketplace upgrades.  The
        # canonical in-plugin filename executes the allowlist below.
        if Path(__file__).name != "marketplace_command_dispatcher.py":
            target = (root / "lib" / "marketplace_command_dispatcher.py").resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise DispatchError(
                    f"installed Allostat command dispatcher is missing: {target}"
                )
            try:
                completed = subprocess.run([*python_argv, str(target), *args])
            except OSError as exc:
                print(
                    f"allostat-command: could not start current dispatcher: {exc}",
                    file=sys.stderr,
                )
                return 4
            return int(completed.returncode)
        script, prefix = resolve_operation(root, operation)
    except DispatchError as exc:
        print(f"allostat-command: {exc}", file=sys.stderr)
        return 3
    try:
        harness_args = (
            ("--harness", harness) if operation in HARNESS_AWARE_OPERATIONS else ()
        )
        completed = subprocess.run(
            [*python_argv, str(script), *prefix, *harness_args, *rest]
        )
    except OSError as exc:
        print(f"allostat-command: could not start {script}: {exc}", file=sys.stderr)
        return 4
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
