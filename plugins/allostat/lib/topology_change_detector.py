"""Topology-change detector — Phase 3.3c.

Producer for the `topology_change_detected` observation event that
volume-control's `detect_topology_change_unrecorded` consumes. Pre-Phase-3.3c
this producer didn't exist; volume-control's topology branch was
structurally unable to fire.

Strategy: classify each Bash command invoked through PreToolUse against
a curated list of high-precision topology-shaped commands (git mv, git
rebase, git filter-*, mkdir -p with multi-level path, rmdir). When a
match fires, append a `topology_change_detected` observation with the
matched command (truncated) and the matched pattern's `kind` tag.

Precision-over-recall design: each pattern is chosen to fire only on
commands that are unambiguously topology operations. Routine file edits,
in-place file moves (single-level mv), and dev-cycle git pushes do NOT
fire. Operator can add custom patterns via the
`ALLOSTAT_TOPOLOGY_PATTERNS` env var (semicolon-separated regex list).

Server-side `_detect_topology_change_unrecorded` reads
`event="topology_change_detected"` observations and walks forward looking
for a `migration_recorded` event to silence the nudge.
"""
from __future__ import annotations

import os
import re
from typing import Any


_BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # git-tracked moves — explicit topology operation.
    ("git_mv", re.compile(
        r"(?:^|[;&|\n])\s*git\s+mv\b",
        re.IGNORECASE,
    )),
    # git rebase history rewrites.
    ("git_rebase_root", re.compile(
        r"(?:^|[;&|\n])\s*git\s+rebase\b[^|;&]*--root\b",
        re.IGNORECASE,
    )),
    ("git_rebase_interactive", re.compile(
        r"(?:^|[;&|\n])\s*git\s+rebase\b[^|;&]*(-i\b|--interactive\b)",
        re.IGNORECASE,
    )),
    # git filter-branch / filter-repo — explicit history rewrite.
    ("git_filter_branch", re.compile(
        r"(?:^|[;&|\n])\s*git\s+filter-branch\b",
        re.IGNORECASE,
    )),
    ("git_filter_repo", re.compile(
        r"(?:^|[;&|\n])\s*git\s+filter-repo\b",
        re.IGNORECASE,
    )),
    # Multi-level directory creation — suggests project structure setup.
    # Requires at least one path separator inside the path argument.
    ("mkdir_p_multilevel", re.compile(
        r"(?:^|[;&|\n])\s*mkdir\s+-p\s+\S*[\\/]\S",
        re.IGNORECASE,
    )),
    # rmdir of a path — explicit directory removal.
    ("rmdir", re.compile(
        r"(?:^|[;&|\n])\s*rmdir\b",
        re.IGNORECASE,
    )),
    # rm with -r/-R flag (recursive — directory removal). Pattern requires
    # AT LEAST ONE r/R in the flags; -f alone is just forced single-file
    # rm (.DS_Store cleanup etc.), not a topology op.
    ("rm_recursive", re.compile(
        r"(?:^|[;&|\n])\s*rm\s+-[fR]*[rR][fR]*\b",
        re.IGNORECASE,
    )),
    # find ... -exec mv|rename — mass file relocation.
    ("find_exec_mv", re.compile(
        r"\bfind\b[^|;&]*-exec\s+(mv|rename)\b",
        re.IGNORECASE,
    )),
)


def _custom_patterns_from_env() -> tuple[tuple[str, re.Pattern], ...]:
    """Read ALLOSTAT_TOPOLOGY_PATTERNS — semicolon-separated regex list."""
    raw = os.environ.get("ALLOSTAT_TOPOLOGY_PATTERNS", "")
    if not raw:
        return ()
    out: list[tuple[str, re.Pattern]] = []
    for i, pat_str in enumerate(raw.split(";")):
        pat_str = pat_str.strip()
        if not pat_str:
            continue
        try:
            out.append((f"operator_custom_{i}", re.compile(pat_str, re.IGNORECASE)))
        except re.error:
            continue
    return tuple(out)


def classify_command(command: str | None) -> tuple[str, str] | None:
    """Classify a Bash command. Returns (kind, matched_pattern_str) on
    match; None if no pattern fires."""
    if not command or not isinstance(command, str):
        return None
    patterns = _BUILTIN_PATTERNS + _custom_patterns_from_env()
    for kind, pat in patterns:
        if pat.search(command):
            return (kind, pat.pattern)
    return None


def _scan_enabled() -> bool:
    """Honor ALLOSTAT_TOPOLOGY_DETECTOR env var. Defaults to enabled.

    Operator can set `0|false|no|off` to disable.
    """
    raw = os.environ.get("ALLOSTAT_TOPOLOGY_DETECTOR", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def detect_and_emit(
    state_dir,  # Path | None — caller guards None outside
    tool_name: str,
    command: str | None,
) -> dict[str, Any] | None:
    """Classify the command + emit `topology_change_detected` observation
    if it matches a topology pattern. Returns the emitted details dict,
    or None when nothing fires.

    Best-effort: any append failure is swallowed silently rather than
    breaking the PreToolUse hook.
    """
    if not _scan_enabled():
        return None
    if (tool_name or "").lower() != "bash":
        return None
    match = classify_command(command)
    if match is None:
        return None
    # F1 (2026-05-29): a topology command targeting temp/scratch/build paths is
    # dev-cycle noise, not a project migration — suppress.
    from migration_provenance import is_denylisted, auto_acknowledge  # noqa: E402
    from secret_redaction import redact_secrets  # noqa: E402
    if is_denylisted(command):
        return None
    kind, pattern_str = match
    details = {
        "kind": kind,
        # Redact secrets before persisting to observations.jsonl (ultraswarm
        # topology leak, 2026-07-07).
        "command_excerpt": redact_secrets(command or "")[:300],
        "matched_pattern": pattern_str,
    }
    try:
        from local_state import append_observation  # noqa: E402
        append_observation(state_dir, "topology_change_detected", details=details)
    except Exception:
        return None
    # F2 (2026-05-29): regulator self-acknowledges so volume-control doesn't
    # nag for a manual /allostat record-migration. Audit observation above is
    # still written; only the manual-click demand goes away.
    auto_acknowledge(state_dir, "topology", kind, command)
    return details
