"""Allostat tier inference for v2.4 memory tree — PATCH-136 / v0.4.0.

Simplified port of the v2.3 plugin's tier_inference module adapted for the
v2.4 .md memory tree at ~/.claude/projects/<sanitized-cwd>/memory/. Drops
the v2.3 umbrella support (operator's v2.4 trees don't currently use it)
and the index-lookup paths (no umbrella index files in v2.4 yet).

What it does: given a .md filename, classify into a tier so /allostat-tend
can audit tier integrity + so future conditional_loader can decide which
files to surface at session start.

Tier model:
  innate         — ship-with hardcoded rules (not in operator memory)
  user           — operator-tier feedback, identity, voice, prose
  project        — project-scoped feedback or main project file
  reference      — pointers to external resources
  outside-matrix — MEMORY.md index, brainstorms, *_index.md, *_log.md, *_journal.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TierClassification:
    tier: str  # "user" | "project" | "reference" | "outside-matrix" | "innate"
    scope: str | None = None  # project name if tier=="project"
    subtype: str | None = None  # "identity" | "voice" | "index" | "journal" | "main" | "sub-rule"
    source: str = "filename"
    confidence: str = "high"  # "high" | "medium" | "low"
    flags: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        scope_part = f" (scope={self.scope})" if self.scope else ""
        return f"<{self.tier}{scope_part}, {self.subtype or 'main'}, {self.confidence}>"


# ---------- pattern recognizers ----------

OUTSIDE_MATRIX_EXACT = {
    "MEMORY.md": ("index", "high"),
    "brainstorms.md": ("journal", "high"),
    "_PURPOSE.md": ("meta", "high"),
}

OUTSIDE_MATRIX_REGEX = [
    (re.compile(r".*_portfolio\.md$", re.IGNORECASE), "meta-index", "high"),
    (re.compile(r".*_index\.md$", re.IGNORECASE), "meta-index", "high"),
    (re.compile(r".*_journal\.md$", re.IGNORECASE), "journal", "high"),
    (re.compile(r".*_log\.md$", re.IGNORECASE), "journal", "medium"),
]

VOICE_REGEX = re.compile(r".*_(prose|voice)\.md$", re.IGNORECASE)
USER_PREFIX_REGEX = re.compile(r"^user_.*\.md$", re.IGNORECASE)
FEEDBACK_FILE_REGEX = re.compile(r"^feedback_(.+)\.md$", re.IGNORECASE)
PROJECT_FILE_REGEX = re.compile(r"^project_(.+)\.md$", re.IGNORECASE)
REFERENCE_FILE_REGEX = re.compile(r"^reference_(.+)\.md$", re.IGNORECASE)


def infer_tier(
    filename,
    frontmatter: dict | None = None,
    known_projects: list[str] | None = None,
) -> TierClassification:
    """Classify a memory file into a tier using filename + optional frontmatter.

    Args:
        filename: basename (str or Path)
        frontmatter: optional dict from parsed YAML frontmatter; if contains
            "tier" field, overrides inference
        known_projects: list of project names for prefix matching on
            feedback_<project>_* and project_<name>_*

    Returns:
        TierClassification.
    """
    if hasattr(filename, "name"):
        filename = filename.name
    else:
        filename = str(filename)

    # Frontmatter override: highest authority
    if frontmatter and "tier" in frontmatter:
        return TierClassification(
            tier=frontmatter["tier"],
            scope=frontmatter.get("scope"),
            subtype=frontmatter.get("subtype", "main"),
            source="frontmatter",
            confidence="high",
        )

    if filename in OUTSIDE_MATRIX_EXACT:
        subtype, conf = OUTSIDE_MATRIX_EXACT[filename]
        return TierClassification(tier="outside-matrix", subtype=subtype, confidence=conf)

    for pattern, subtype, conf in OUTSIDE_MATRIX_REGEX:
        if pattern.match(filename):
            return TierClassification(tier="outside-matrix", subtype=subtype, confidence=conf)

    if VOICE_REGEX.match(filename):
        return TierClassification(tier="user", subtype="voice", confidence="high")

    if USER_PREFIX_REGEX.match(filename):
        return TierClassification(tier="user", subtype="identity", confidence="high")

    if REFERENCE_FILE_REGEX.match(filename):
        return TierClassification(tier="reference", subtype="main", confidence="high")

    m = FEEDBACK_FILE_REGEX.match(filename)
    if m:
        suffix = m.group(1)
        if known_projects:
            suffix_norm = _normalize(suffix)
            for project in known_projects:
                p_norm = _normalize(project)
                if suffix_norm.startswith(p_norm + "-") or suffix_norm == p_norm:
                    return TierClassification(
                        tier="project",
                        scope=project,
                        subtype="sub-rule",
                        source="filename",
                        confidence="high",
                    )
        # No project prefix match: cross-project user-tier feedback
        return TierClassification(
            tier="user",
            subtype="main",
            source="filename",
            confidence="medium",
            flags=["no_project_prefix_matched"],
        )

    m = PROJECT_FILE_REGEX.match(filename)
    if m:
        suffix = m.group(1)
        suffix_norm = _normalize(suffix)
        if known_projects:
            for project in known_projects:
                p_norm = _normalize(project)
                if suffix_norm == p_norm:
                    return TierClassification(
                        tier="project", scope=project, subtype="main",
                        source="filename", confidence="high",
                    )
                if suffix_norm.startswith(p_norm + "-"):
                    return TierClassification(
                        tier="project", scope=project, subtype="sub-rule",
                        source="filename", confidence="high",
                    )
        return TierClassification(
            tier="project",
            scope=_normalize(suffix),
            subtype="main",
            source="filename",
            confidence="medium",
            flags=["project_inferred_from_filename"],
        )

    return TierClassification(
        tier="user",
        subtype="main",
        source="filename",
        confidence="low",
        flags=["unknown_pattern_defaulted_to_user_tier"],
    )


def audit_memory_tree(memory_dir: Path, known_projects: list[str] | None = None) -> dict:
    """Walk a memory dir, classify every .md file, return audit report.

    Returns:
        {
          "total": int,
          "by_tier": {tier: count},
          "low_confidence": [list of (filename, reason)],
          "unknown_pattern": [list of filenames],
        }
    """
    report = {
        "total": 0,
        "by_tier": {},
        "low_confidence": [],
        "unknown_pattern": [],
    }
    if not memory_dir.exists():
        return report

    for rule_path in memory_dir.rglob("*.md"):
        # Skip retirement archive directories
        if "_RETIRED" in rule_path.parts:
            continue
        report["total"] += 1
        cls = infer_tier(rule_path.name, known_projects=known_projects)
        report["by_tier"][cls.tier] = report["by_tier"].get(cls.tier, 0) + 1
        if cls.confidence == "low":
            report["low_confidence"].append((rule_path.name, cls.flags))
        if "unknown_pattern_defaulted_to_user_tier" in cls.flags:
            report["unknown_pattern"].append(rule_path.name)

    return report


def _normalize(name: str) -> str:
    return re.sub(r"[-_\s]+", "-", name.lower()).strip("-")
