"""Allostat MEMORY.md per-entry cap enforcement — PATCH-137 / v0.5.0 Slice 1.

Enforces the global rule documented in operator's CLAUDE.md `auto memory`
section: each MEMORY.md entry should be one line, under ~150 characters.
v2.3 documented the discipline; v2.4 v0.5.0 ENFORCES it via pre-tool-use
hook interception of Write/Edit on MEMORY.md paths.

Per advisor brief 2026-05-20 §1.1: framed honestly as NEW functionality
(not a v2.3 port — v2.3 only WARNED on >200-line files, never blocked).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


ENTRY_CAP_CHARS = 150
ENTRY_LINE_PATTERN = re.compile(r"^- \[.+\]\(.+\)")  # MEMORY.md index entry shape


@dataclass
class CapViolation:
    line_number: int
    entry_text: str
    char_count: int
    truncated_proposal: str


def find_cap_violations(memory_md_text: str) -> list[CapViolation]:
    """Scan MEMORY.md text for entries exceeding ENTRY_CAP_CHARS.

    Returns list of violations (empty if all entries within cap).
    """
    violations: list[CapViolation] = []
    for i, line in enumerate(memory_md_text.splitlines(), start=1):
        stripped = line.rstrip()
        if not ENTRY_LINE_PATTERN.match(stripped):
            continue
        if len(stripped) > ENTRY_CAP_CHARS:
            truncated = stripped[:ENTRY_CAP_CHARS].rstrip() + "..."
            violations.append(CapViolation(
                line_number=i,
                entry_text=stripped,
                char_count=len(stripped),
                truncated_proposal=truncated,
            ))
    return violations


def format_violation_message(violations: list[CapViolation]) -> str:
    if not violations:
        return ""
    lines = [
        f"MEMORY.md entry cap rejection ({len(violations)} entries over "
        f"{ENTRY_CAP_CHARS}-char limit):",
        "",
    ]
    for v in violations[:5]:
        lines.append(
            f"  Line {v.line_number} ({v.char_count} chars):"
        )
        lines.append(f"    Original:  {v.entry_text[:200]}{'...' if len(v.entry_text) > 200 else ''}")
        lines.append(f"    Truncated: {v.truncated_proposal}")
        lines.append("")
    if len(violations) > 5:
        lines.append(f"  ... and {len(violations) - 5} more entries over cap.")
    lines.append(
        "Per Allostat v1.0.0 Phase 1 Slice 1: MEMORY.md is an INDEX, "
        f"not memory content. Each entry must be ≤{ENTRY_CAP_CHARS} chars. "
        "Move the long content into a frontmatter description field on the "
        "underlying .md file; keep the MEMORY.md hook short."
    )
    return "\n".join(lines)


def is_memory_md_path(file_path: str) -> bool:
    """Return True if file_path is a MEMORY.md file (anywhere on disk)."""
    if not file_path:
        return False
    return file_path.replace("\\", "/").lower().endswith("/memory.md")
