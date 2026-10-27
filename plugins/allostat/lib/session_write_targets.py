"""Successful tool writes that can arm the same-session script guard.

PostToolUse sees whether the tool actually succeeded. Codex apply_patch keeps
its targets inside the patch envelope and shell tools keep redirect targets in
the command, so neither presents the `file_path` used by Write/Edit. This module
extracts only those successful write destinations; the tracker then filters to
executable suffixes.
"""

from __future__ import annotations

import re
import importlib.util
from functools import lru_cache
from pathlib import Path


_PATCH_FILE = re.compile(r"^\*\*\*\s+(?:Add|Update) File:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_MOVE = re.compile(r"^\*\*\*\s+Move to:\s*(.+?)\s*$", re.MULTILINE)
_PATCH_TOOL_NAMES = frozenset({
    "apply_patch", "applypatch", "exec", "exec_command", "shell",
    "shell_command", "bash",
})
_SHELL_TOOL_NAMES = frozenset({"shell", "shell_command", "bash", "exec", "exec_command"})
_EXPLICIT_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit", "notebookedit"})


def _string_values(tool_input: dict) -> list[str]:
    return [value for value in tool_input.values() if isinstance(value, str)]


def patch_targets(tool_name: str | None, tool_input: dict) -> list[str]:
    if (tool_name or "").casefold() not in _PATCH_TOOL_NAMES:
        return []
    found: list[str] = []
    for text in _string_values(tool_input):
        if "*** Begin Patch" not in text or "*** End Patch" not in text:
            continue
        found.extend(match.group(1).strip() for match in _PATCH_FILE.finditer(text))
        found.extend(match.group(1).strip() for match in _PATCH_MOVE.finditer(text))
    return found


@lru_cache(maxsize=1)
def _shell_target_parser():
    """Load the already-hardened PreToolUse write parser without executing it.

    Keeping one parser matters here: its quote, option, command-boundary, and
    Windows-path rules are the exact rules that approved the successful shell
    call which PostToolUse is now recording.
    """
    hook = Path(__file__).resolve().parent.parent / "hooks" / "pre-tool-use.py"
    spec = importlib.util.spec_from_file_location("_allostat_pre_tool_write_parser", hook)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load write-target parser from {hook}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._bash_write_targets


def shell_write_targets(tool_name: str | None, tool_input: dict) -> list[str]:
    if (tool_name or "").casefold() not in _SHELL_TOOL_NAMES:
        return []
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return []
    try:
        return list(_shell_target_parser()(command))
    except Exception:
        # Dynamic/unresolvable targets cannot be truthfully tracked after the
        # shell call. Keep PostToolUse best-effort and never break the tool.
        return []


def successful_write_targets(
    tool_name: str | None,
    tool_input: dict,
) -> list[str]:
    """Unique explicit, apply-patch, and shell-redirection destinations."""
    candidates: list[str] = []
    if (tool_name or "").casefold() in _EXPLICIT_WRITE_TOOL_NAMES:
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
                break
    candidates.extend(patch_targets(tool_name, tool_input))
    candidates.extend(shell_write_targets(tool_name, tool_input))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(Path(candidate)) if candidate else ""
        key = normalized.replace("\\", "/").casefold()
        if normalized and key not in seen:
            seen.add(key)
            unique.append(normalized)
    return unique
