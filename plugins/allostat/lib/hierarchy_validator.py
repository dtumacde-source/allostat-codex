"""Allostat hierarchy validator — PATCH-139 / v1.0.0 Slice 8.

Tier integrity audit on operator's memory tree. Walks files, classifies
via tier_inference, surfaces mismatches between filename-inferred tier
and explicit frontmatter `tier:` field.

Simplified v2.4 port (the v2.3 5-tier model with separate plugin-innate
+ operator-innate + user + umbrella + project tiers doesn't apply
directly; v2.4 uses user/project/reference/outside-matrix).

Ported from the v2.3 plugin's hierarchy_validator.py into the v2.4 wrapper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import sys

_LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(_LIB))

import tier_inference  # noqa: E402
from session_handoff import _iter_files_safe  # noqa: E402


@dataclass
class TierMismatch:
    file_name: str
    inferred_tier: str
    frontmatter_tier: str | None
    reason: str


def parse_frontmatter_tier(text: str) -> str | None:
    """Extract `tier:` field from YAML frontmatter; None if absent."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fm = text[4:end]
    m = re.search(r"^tier:\s*(.+)$", fm, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")
    return None


def audit_tier_integrity(memory_root: Path, known_projects: list[str] | None = None) -> dict:
    """Walk memory tree, classify every .md, surface tier-mismatches.

    Returns:
        {
          "total": int,
          "mismatches": [TierMismatch...],
          "no_frontmatter_count": int,
          "audit_passed": bool,
        }
    """
    report = {
        "total": 0,
        "mismatches": [],
        "no_frontmatter_count": 0,
        "audit_passed": True,
    }
    if not memory_root.exists():
        return report

    skip_dirs = {"_LEGACY", "_PURGE", "_RETIRED", "_processed"}
    # H-20: use the junction/symlink-safe walk. pathlib.rglob FOLLOWS directory
    # junctions/symlinks on py3.11/3.12, so a circular junction inside the memory
    # tree (the Downloads-junction failure pattern) would loop forever here. The
    # shared _iter_files_safe helper skips reparse-point dirs and caps depth.
    for p in _iter_files_safe(memory_root, ".md"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.name in ("MEMORY.md", "README.md"):
            continue
        report["total"] += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm_tier = parse_frontmatter_tier(text)
        if fm_tier is None:
            report["no_frontmatter_count"] += 1
            continue
        inferred = tier_inference.infer_tier(p.name, known_projects=known_projects)
        if inferred.tier != fm_tier:
            report["mismatches"].append(TierMismatch(
                file_name=p.name,
                inferred_tier=inferred.tier,
                frontmatter_tier=fm_tier,
                reason=f"filename suggests '{inferred.tier}', frontmatter declares '{fm_tier}'",
            ))
            report["audit_passed"] = False
    return report


def format_audit_report(report: dict) -> str:
    """Operator-facing report for /allostat-tend --audit-tiers."""
    lines = [
        f"=== Hierarchy validator — tier integrity audit ===",
        f"",
        f"Total files audited: {report['total']}",
        f"Files without frontmatter tier: {report['no_frontmatter_count']}",
        f"Mismatches: {len(report['mismatches'])}",
        f"Audit: {'PASS' if report['audit_passed'] else 'FAIL'}",
    ]
    if report["mismatches"]:
        lines.append("")
        lines.append("Tier mismatches detected:")
        for m in report["mismatches"][:20]:
            lines.append(f"  {m.file_name}")
            lines.append(f"    {m.reason}")
        if len(report["mismatches"]) > 20:
            lines.append(f"  ... + {len(report['mismatches']) - 20} more")
    return "\n".join(lines)
