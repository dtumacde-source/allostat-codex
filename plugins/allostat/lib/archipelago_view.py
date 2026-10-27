"""Allostat archipelago multi-project overview — PATCH-139 / v1.0.0 Slice 9.

Reads ~/.claude/.allostat/archipelago.json (registry of standalone +
umbrella + linked projects); renders a combined status view.

Used by /allostat-status --all.

Ported from the v2.3 plugin's archipelago_view module for v2.4 wrapper.
"""
from __future__ import annotations

import json
from pathlib import Path


ARCHIPELAGO_FILE_PARTS = (".claude", ".allostat", "archipelago.json")
PROJECTS_ROOT_PARTS = (".claude", "projects")

STATE_MARKER = {
    "healthy": "OK",
    "thriving": "EXCELLENT",
    "hungry": "IDLE",
    "sick": "DEGRADED",
    "stressed": "STRESSED",
}


def read_archipelago(home: Path | None = None) -> dict:
    if home is None:
        home = Path.home()
    path = home.joinpath(*ARCHIPELAGO_FILE_PARTS)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_project_summary(home: Path, project_folder: str) -> dict | None:
    project_dir = home.joinpath(*PROJECTS_ROOT_PARTS, project_folder)
    state_path = project_dir / ".allostat" / "state.json"
    config_path = project_dir / ".allostat" / "config.json"
    if not state_path.exists():
        return None

    summary: dict = {
        "project_folder": project_folder,
        "display_name": project_folder,
    }

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        summary["state_marker"] = state.get("stateMarker", "healthy")
        summary["rule_count"] = state.get("ruleCount", {})
    except (json.JSONDecodeError, OSError):
        return None

    try:
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            summary["display_name"] = config.get("project_name", project_folder)
            summary["linked_to"] = config.get("linkedTo", [])
            summary["last_calibration"] = config.get("calibrationCompleted")
    except (json.JSONDecodeError, OSError):
        pass

    return summary


def render_archipelago_view(home: Path | None = None) -> str:
    if home is None:
        home = Path.home()
    archipelago = read_archipelago(home)
    if not archipelago:
        return "No archipelago data found. Run /allostat-init in a project to start."

    project_folders: list[str] = []
    for proj in archipelago.get("standaloneProjects", []):
        project_folders.append(proj.get("path", ""))
    for proj in archipelago.get("linkedProjects", []):
        project_folders.append(proj.get("path", ""))
    for umbrella, info in archipelago.get("umbrellas", {}).items():
        for member in info.get("members", []):
            if member not in project_folders:
                project_folders.append(member)
    project_folders = [p for p in project_folders if p]
    if not project_folders:
        return "No projects registered."

    summaries = []
    for folder in project_folders:
        summary = load_project_summary(home, folder)
        if summary:
            summaries.append(summary)

    standalone_summaries = []
    umbrella_summaries: dict[str, list] = {}
    for s in summaries:
        linked = s.get("linked_to") or []
        if linked:
            for u in linked:
                umbrella_summaries.setdefault(u, []).append(s)
        else:
            standalone_summaries.append(s)

    lines = ["", "Allostat archipelago", "=" * 60, ""]
    if standalone_summaries:
        lines.append("Standalone projects:")
        for s in standalone_summaries:
            lines.append("  " + _format_line(s))
        lines.append("")
    for umbrella_name, projects in umbrella_summaries.items():
        info = archipelago.get("umbrellas", {}).get(umbrella_name, {})
        display = info.get("displayName", umbrella_name)
        lines.append(f"Umbrella: {display}")
        for s in projects:
            lines.append("  " + _format_line(s))
        planned = info.get("verticalsPlanned", [])
        if planned:
            lines.append(f"  (planned: {', '.join(planned)})")
        lines.append("")

    total_innate = sum(s.get("rule_count", {}).get("innate", 0) for s in summaries)
    total_imprinted = sum(s.get("rule_count", {}).get("imprinted", 0) for s in summaries)
    total_learned = sum(s.get("rule_count", {}).get("learned", 0) for s in summaries)
    lines.append("-" * 60)
    lines.append(
        f"Total — {len(summaries)} project(s) · "
        f"{total_innate}I · {total_imprinted}P · {total_learned}L"
    )
    if archipelago.get("umbrellas"):
        names = ", ".join(archipelago["umbrellas"].keys())
        lines.append(f"Umbrellas: {len(archipelago['umbrellas'])} ({names})")
    return "\n".join(lines)


def _format_line(s: dict) -> str:
    state = STATE_MARKER.get(s.get("state_marker", "healthy"), "OK")
    counts = s.get("rule_count", {})
    rule_summary = (
        f"{counts.get('innate', 0)}I "
        f"{counts.get('imprinted', 0)}P "
        f"{counts.get('learned', 0)}L"
    )
    name = s["display_name"]
    return f"{name:<48} [{state}] {rule_summary}"
