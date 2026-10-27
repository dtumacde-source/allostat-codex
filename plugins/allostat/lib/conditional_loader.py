"""Allostat conditional content loader — PATCH-138 / v0.6.0 Slice 3.

Three-tier conditional content load at SessionStart:
  Tier 1 (always-on)  - universal essentials (file location rules, handoff
                        thresholds, innate rules)
  Tier 2 (project-cond) - per-project architecture/sandbox content loaded
                          when cwd-matches OR first-prompt names project
                          OR explicit /allostat-load <project> command
  Tier 3 (trigger-cond) - single-trigger content (context7, walkthrough,
                          recall-full) — explicit per-item trigger only

Ported from the v2.3 plugin's conditional_loader.py into the v2.4 wrapper.

PROJECT_REGISTRY is customer-configurable via:
  ~/.claude/.allostat/project_registry.json (merges with defaults)
Defaults are empty by design — customer populates via the override JSON
as they discover their own projects. The only built-in entry is
"allostat" itself (self-referential — Allostat detecting Allostat-work
cwd is legitimate function). Non-matching cwds fall back gracefully
(customer can use /allostat-load <project> to force or populate
project_registry.json with their own project layout).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------- defaults (operator-overridable via JSON file) ----------

_DEFAULT_PROJECT_REGISTRY: dict[str, dict] = {
    "allostat": {
        "slugs": ["allostat"],
        "cwd_substrings": [
            ".claude/plugins/cache/local/allostat",
            ".claude/plugins/marketplaces/local/plugins/allostat",
            "allostat-dev",
            "allostat-wrapper",
            "allostat-mcp",
            "allostat-installer",
            "allostat-contracts",
        ],
    },
}


# Tier 2 content per project — Bucket A dedup 2026-05-24:
# `~/.claude/allostat_architecture.md` and `~/.claude/memory_architecture.md`
# removed from default Tier 2 injection. Those files moved to bundle's
# data/appendix_seed/ with topic frontmatter and load topic-conditional
# via `appendix_system.py` — fires only when prompt topics match
# (architecture/pillars/regulator for allostat_architecture; memory/
# memory-tree/mcs/continuity for memory_architecture). Closes the
# ~15KB-per-Allostat-cwd-session bloat from double-injection.
_DEFAULT_TIER2_CONTENT_PER_PROJECT: dict[str, list[str]] = {}


_DEFAULT_TIER3_TRIGGERS: dict[str, dict] = {
    "context7": {
        "slugs": ["context7", "library doc", "sdk doc", "api doc"],
        "content_path": None,
    },
    "legacy_archive": {
        "slugs": ["show legacy", "legacy archive"],
        "content_path": "~/.claude/allostat/_legacy_index.md",
    },
    "walkthrough": {
        "slugs": ["walkthrough", "walk through"],
        "content_path": None,
    },
    "recall_full": {
        "slugs": ["/allostat recall full"],
        "content_path": None,
    },
}


EXPLICIT_COMMAND_PATTERN = re.compile(
    r"/(?:allostat\s+activate|load|allostat-load)\s+([\w_\-]+)",
    re.IGNORECASE,
)


# ---------- registry loader ----------

def _load_registry() -> tuple[dict, dict, dict]:
    """Return (project_registry, tier2_content_per_project, tier3_triggers).
    Reads override JSON if present at ~/.claude/.allostat/project_registry.json.
    """
    project_registry = dict(_DEFAULT_PROJECT_REGISTRY)
    tier2_per_project = dict(_DEFAULT_TIER2_CONTENT_PER_PROJECT)
    tier3_triggers = dict(_DEFAULT_TIER3_TRIGGERS)

    override_path = Path.home() / ".claude" / ".allostat" / "project_registry.json"
    if override_path.exists():
        try:
            data = json.loads(override_path.read_text(encoding="utf-8"))
            if isinstance(data.get("project_registry"), dict):
                project_registry.update(data["project_registry"])
            if isinstance(data.get("tier2_content_per_project"), dict):
                tier2_per_project.update(data["tier2_content_per_project"])
            if isinstance(data.get("tier3_triggers"), dict):
                tier3_triggers.update(data["tier3_triggers"])
        except (json.JSONDecodeError, OSError):
            pass

    return project_registry, tier2_per_project, tier3_triggers


# ---------- detection ----------

def detect_project_from_cwd(cwd: str, project_registry: dict | None = None) -> str | None:
    if project_registry is None:
        project_registry, _, _ = _load_registry()
    if not cwd:
        return None
    cwd_lower = cwd.lower().replace("\\", "/")
    for name, spec in project_registry.items():
        for substr in spec.get("cwd_substrings", []):
            if substr.lower() in cwd_lower:
                return name
    return None


def detect_project_from_prompt(prompt: str, project_registry: dict | None = None) -> str | None:
    if project_registry is None:
        project_registry, _, _ = _load_registry()
    if not prompt:
        return None
    prompt_lower = prompt.lower()
    for name, spec in project_registry.items():
        for slug in spec.get("slugs", []):
            if slug.lower() in prompt_lower:
                return name
    return None


def detect_explicit_command(prompt: str, project_registry: dict | None = None) -> str | None:
    if project_registry is None:
        project_registry, _, _ = _load_registry()
    if not prompt:
        return None
    m = EXPLICIT_COMMAND_PATTERN.search(prompt)
    if not m:
        return None
    candidate = m.group(1).lower().strip()
    for name, spec in project_registry.items():
        if name == candidate:
            return name
        if candidate in [s.lower() for s in spec.get("slugs", [])]:
            return name
    return None


def detect_tier3_triggers(prompt: str, tier3_triggers: dict | None = None) -> list[str]:
    if tier3_triggers is None:
        _, _, tier3_triggers = _load_registry()
    if not prompt:
        return []
    prompt_lower = prompt.lower()
    fired = []
    for name, spec in tier3_triggers.items():
        for slug in spec.get("slugs", []):
            if slug.lower() in prompt_lower:
                fired.append(name)
                break
    return fired


# ---------- resolution ----------

@dataclass
class TierResolution:
    matched_project: str | None = None
    tier2_files: list[Path] = field(default_factory=list)
    tier3_triggers: list[str] = field(default_factory=list)
    tier3_files: list[Path] = field(default_factory=list)
    trigger_source: str = ""


def resolve_tier_includes(cwd: str, first_prompt: str = "") -> TierResolution:
    """Compute the Tier-2/Tier-3 inclusion set for this session.

    Precedence (per Locked Decision #2 in v2.3):
      1. explicit slash command in first_prompt → highest priority
      2. cwd matches a registered project
      3. first_prompt mentions a registered project's slug
    """
    project_registry, tier2_per_project, tier3_triggers = _load_registry()

    project = detect_explicit_command(first_prompt, project_registry)
    source = "explicit_command" if project else ""
    if not project:
        project = detect_project_from_cwd(cwd, project_registry)
        source = "cwd" if project else source
    if not project:
        project = detect_project_from_prompt(first_prompt, project_registry)
        source = "prompt" if project else source

    tier2_files: list[Path] = []
    if project:
        for raw_path in tier2_per_project.get(project, []):
            expanded = Path(os.path.expanduser(raw_path))
            if expanded.exists():
                tier2_files.append(expanded)

    tier3_fired = detect_tier3_triggers(first_prompt, tier3_triggers)
    tier3_files: list[Path] = []
    for trigger_name in tier3_fired:
        path_str = tier3_triggers.get(trigger_name, {}).get("content_path")
        if path_str:
            expanded = Path(os.path.expanduser(path_str))
            if expanded.exists():
                tier3_files.append(expanded)

    return TierResolution(
        matched_project=project,
        tier2_files=tier2_files,
        tier3_triggers=tier3_fired,
        tier3_files=tier3_files,
        trigger_source=source,
    )


def render_tier_includes(resolution: TierResolution, max_chars: int = 100_000) -> str:
    """Read Tier 2 + Tier 3 files and return concatenated content suitable
    for injection into additionalContext at SessionStart. Bounded by max_chars
    to prevent runaway context inflation.
    """
    blocks: list[str] = []
    for f in resolution.tier2_files + resolution.tier3_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"\n\n=== Allostat conditional content: {f.name} (project={resolution.matched_project}, source={resolution.trigger_source}) ===\n\n"
        blocks.append(header + text)
    combined = "".join(blocks)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
    return combined
