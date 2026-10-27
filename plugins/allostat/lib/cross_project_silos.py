"""Cross-project recall silos — wrapper-side sweep (ι).

Plan §4 row ι: server is uninvolved in silo storage. Cross-project
promotion logic (umbrella tier — the same problem recurring across
multiple projects should share one antibody) is handled by a periodic
local sweep.

The wrapper walks every project's `.allostat/silos/` directory under
`~/.claude/projects/<encoded>/`, identifies same-fingerprint entries
that recur across N≥2 projects, and surfaces umbrella-promotion
proposals to the operator.

Operator approves via `/allostat-promote --umbrella <name>`; the wrapper
writes the promoted rule to the umbrella-tier YAML location.

Server is consulted only for fingerprint-hash computation (via
`recall_silos_query.compute_fingerprint_hash`) so the wrapper doesn't
duplicate the hash function — drift between client and server is
prevented.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CROSS_PROJECT_THRESHOLD = 2
DEFAULT_SILOS_BASE = Path.home() / ".claude" / "projects"


def _claude_projects_root() -> Path:
    """Locate the Claude Code projects root.

    Honors ALLOSTAT_CLAUDE_PROJECTS_ROOT for test fixtures; defaults to
    `~/.claude/projects/` which is the harness-canonical location.
    """
    if override := os.environ.get("ALLOSTAT_CLAUDE_PROJECTS_ROOT"):
        return Path(override)
    return DEFAULT_SILOS_BASE


@dataclass
class CrossProjectMatch:
    """A fingerprint that recurs across N≥threshold projects."""

    class_name: str
    fingerprint_hash: str
    sorted_tokens: tuple[str, ...]
    projects: list[str] = field(default_factory=list)
    total_fire_count: int = 0
    sample_entry: dict[str, Any] | None = None


def _compute_hash_local(class_name: str, key_tokens: list[str]) -> str:
    """Local fingerprint-hash computation.

    Matches the server's `compute_fingerprint_hash` exactly so this
    sweep can run without a server round-trip per silo entry. Lockstep
    discipline applies: server's compute_fingerprint_hash and this
    function must stay equivalent.
    """
    seed = f"{class_name}::{':'.join(sorted(key_tokens))}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _project_slug(project_dir: Path) -> str:
    """Convert a Claude Code-encoded project folder name into a
    human-readable project slug.

    Folder names like `C--Users-<user>-<project>` get returned as-is
    (no canonical reverse-mapping is reliable; the wrapper carries a
    map in operator-configured state if needed).
    """
    return project_dir.name


def list_silo_files_for_class(
    projects_root: Path,
    class_name: str,
) -> list[tuple[str, Path]]:
    """Return [(project_slug, silo_path), ...] for every project that
    has a `<class>.jsonl` silo file."""
    out: list[tuple[str, Path]] = []
    if not projects_root.is_dir():
        return out
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        silo_path = project_dir / ".allostat" / "silos" / f"{class_name}.jsonl"
        if silo_path.is_file():
            out.append((_project_slug(project_dir), silo_path))
    return out


def _iter_silo_entries(silo_path: Path):
    """Yield each parsed entry from a silo JSONL. Skip malformed lines."""
    try:
        with silo_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def sweep_class(
    class_name: str,
    *,
    projects_root: Path | None = None,
    threshold: int = DEFAULT_CROSS_PROJECT_THRESHOLD,
) -> list[CrossProjectMatch]:
    """Walk every project's `<class>.jsonl` silo + return cross-project
    matches.

    A "match" is a (class_name, fingerprint_hash) that appears in ≥
    threshold projects (default 2).
    """
    if projects_root is None:
        projects_root = _claude_projects_root()

    project_files = list_silo_files_for_class(projects_root, class_name)
    if not project_files:
        return []

    # fingerprint_hash → {project_slug → list of entries}
    by_hash: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    # fingerprint_hash → sorted_tokens (taken from first entry seen)
    tokens_by_hash: dict[str, tuple[str, ...]] = {}

    for project_slug, silo_path in project_files:
        for entry in _iter_silo_entries(silo_path):
            fp = entry.get("fingerprint") or {}
            if not isinstance(fp, dict):
                continue
            if fp.get("class_name") != class_name:
                continue
            tokens = fp.get("key_tokens", [])
            if not isinstance(tokens, list):
                continue
            tokens_str = [str(t) for t in tokens]
            h = _compute_hash_local(class_name, tokens_str)
            by_hash[h][project_slug].append(entry)
            if h not in tokens_by_hash:
                tokens_by_hash[h] = tuple(sorted(tokens_str))

    matches: list[CrossProjectMatch] = []
    for h, projects in by_hash.items():
        if len(projects) < threshold:
            continue
        # Aggregate fire counts.
        total_fc = 0
        sample = None
        for proj_entries in projects.values():
            for entry in proj_entries:
                total_fc += int(entry.get("fire_count", 0) or 0)
                if sample is None:
                    sample = entry
        matches.append(
            CrossProjectMatch(
                class_name=class_name,
                fingerprint_hash=h,
                sorted_tokens=tokens_by_hash[h],
                projects=sorted(projects.keys()),
                total_fire_count=total_fc,
                sample_entry=sample,
            )
        )
    # Rank by project-count desc, then fire_count desc.
    matches.sort(
        key=lambda m: (len(m.projects), m.total_fire_count),
        reverse=True,
    )
    return matches


def sweep_all_classes(
    *,
    projects_root: Path | None = None,
    threshold: int = DEFAULT_CROSS_PROJECT_THRESHOLD,
) -> dict[str, list[CrossProjectMatch]]:
    """Run sweep_class for each of the 5 canonical silo classes.

    Returns {class_name: [CrossProjectMatch, ...]}.
    """
    if projects_root is None:
        projects_root = _claude_projects_root()

    canonical_classes = (
        "drift", "voice", "customization", "workflow", "confidence_recovery"
    )
    out: dict[str, list[CrossProjectMatch]] = {}
    for class_name in canonical_classes:
        out[class_name] = sweep_class(
            class_name,
            projects_root=projects_root,
            threshold=threshold,
        )
    return out


def format_matches_surface(
    matches_by_class: dict[str, list[CrossProjectMatch]],
) -> str:
    """Render the sweep result as an operator-facing surface block."""
    total_matches = sum(len(m) for m in matches_by_class.values())
    if total_matches == 0:
        return "Cross-project silo sweep: no umbrella-promotion candidates."

    lines = [
        f"Cross-project silo sweep — {total_matches} umbrella candidate(s):"
    ]
    for class_name, matches in matches_by_class.items():
        if not matches:
            continue
        lines.append(f"\n  class={class_name}")
        for m in matches[:5]:  # cap surface
            tokens_text = ":".join(m.sorted_tokens)[:80]
            lines.append(
                f"    • [{m.fingerprint_hash}] {tokens_text}"
            )
            lines.append(
                f"      Projects: {', '.join(m.projects)}  "
                f"total_fires={m.total_fire_count}"
            )
        if len(matches) > 5:
            lines.append(f"    (+{len(matches) - 5} more in {class_name})")
    lines.append(
        "\nReview with /allostat-promote --umbrella <name>."
    )
    return "\n".join(lines)
