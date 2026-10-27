"""Rules library as MCP resources (θ).

Plan §4 row θ: "10 innate guardrails + learned rules format + imprinted
rules format + question-bank for onboarding/calibration. All as server-
side MCP resources fetched by the wrapper."

This module wires the canonical rule data as MCP resources the wrapper
plugin can fetch:

  allostat://rules/innate                — innate rules index (YAML list)
  allostat://rules/innate/<rule_id>      — individual innate rule (YAML)
  allostat://rules/learned/schema        — learned-rules schema (YAML)
  allostat://rules/imprinted/schema      — imprinted-rules schema (YAML)
  allostat://question-bank/calibration   — calibration question bank
                                            (already shipped at ε)

The wrapper at η fetches these once at session-start and caches them
client-side. New rules don't require wrapper updates — the server is
the source of truth.

Privacy posture: the resource bodies are STATIC (rule definitions, not
operator state). The server emits the same YAML to every authenticated
wrapper. No per-user data is exposed via resources.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP


# ============================================================================
# Path resolution
# ============================================================================


def _bundled_rules_root() -> Path | None:
    """Locate the bundled `rules/` directory shipped with the server.

    Honors ALLOSTAT_RULES_ROOT env var (test fixtures override) or
    walks up from this file looking for the canonical repo layout.
    """
    if override := os.environ.get("ALLOSTAT_RULES_ROOT"):
        path = Path(override)
        return path if path.is_dir() else None
    here = Path(__file__).resolve()
    for parent in (
        here.parent.parent.parent,
        here.parent.parent.parent.parent,
    ):
        candidate = parent / "rules"
        if candidate.is_dir():
            return candidate
    return None


def _innate_rules_dir() -> Path | None:
    """Path to the bundled innate rules directory (honors
    ALLOSTAT_INNATE_RULES_DIR override)."""
    if override := os.environ.get("ALLOSTAT_INNATE_RULES_DIR"):
        path = Path(override)
        return path if path.is_dir() else None
    root = _bundled_rules_root()
    if root is None:
        return None
    candidate = root / "innate"
    return candidate if candidate.is_dir() else None


# ============================================================================
# Resource body builders
# ============================================================================


def list_innate_rules() -> list[dict[str, Any]]:
    """Walk the innate rules directory, return rule metadata index.

    Each entry: {id, name, severity, tier, description, trigger_signals,
    file}.

    Skips LEGACY backups (matches `_LEGACY_` substring in filename).
    Sorted by filename for stable ordering.
    """
    rules_dir = _innate_rules_dir()
    if rules_dir is None:
        return []

    rules: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        if "_LEGACY_" in path.name:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        trigger = data.get("trigger", {})
        match_any = trigger.get("match_any", []) if isinstance(trigger, dict) else []
        trigger_signals = sorted({
            entry.get("signal")
            for entry in (match_any if isinstance(match_any, list) else [])
            if isinstance(entry, dict) and entry.get("signal")
        })
        rules.append(
            {
                "id": data.get("id") or path.stem,
                "name": data.get("name", ""),
                "severity": data.get("severity", "unknown"),
                "tier": data.get("tier", "innate"),
                "description": data.get("description", ""),
                "trigger_signals": trigger_signals,
                "file": path.name,
            }
        )
    return rules


def read_innate_rule_yaml(rule_id: str) -> str | None:
    """Return the raw YAML body of one innate rule, by id.

    Looks up by rule_id (matched against rule's `id` field), then falls
    back to filename stem matching for resilience.
    """
    rules_dir = _innate_rules_dir()
    if rules_dir is None:
        return None
    # Direct filename match by stem.
    for path in sorted(rules_dir.glob("*.yaml")):
        if "_LEGACY_" in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Match by id field first.
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and data.get("id") == rule_id:
                return text
        except yaml.YAMLError:
            pass
        # Fallback: filename stem.
        if path.stem == rule_id or path.stem.endswith(f"-{rule_id}"):
            return text
    return None


# Learned-rule schema (YAML). Mirrors plugin convention.
LEARNED_RULE_SCHEMA: str = """\
# Learned rule — schema reference (θ MCP resource)
#
# Learned rules accumulate across operator sessions via the pattern-observer
# pillar's N=4 cross-session detection. The pattern-observer surfaces a proposal; on
# operator approval (a/b/c via /allostat-promote) the wrapper writes a YAML
# file matching this schema into .allostat/rules/learned/.
#
# v2.4 hosted MCP: the WRAPPER writes these files. The server returns
# proposals (no per-user state stored server-side).

schema_version: 1
id: <auto-generated UUID or operator-edited slug>
name: <operator-facing short label>
tier: project        # project | umbrella | user
proposed_at: <ISO-8601 UTC timestamp when promoted>
proposed_by: pattern-observer-n5    # or pattern-observer-n2 (fast-promotion)
fingerprint:
  class_: correction | manual_action | recovery | edit | override
  subject: <normalized subject token string>
  direction: <avoid_X | prefer_X | add_X | remove_X | silent_apply>
suggested_wording: |
  Default behavior: avoid <X> when working on <subject>.
operator_amplified_wording: |
  <optional operator-edited wording — overrides suggested_wording when present>
occurrences:
  - timestamp: <ISO-8601>
    subject_excerpt: <short>
    direction: <as above>
sessions_spanning: <int>
status: active        # active | retired | superseded
deprecation_counter: 0    # if > 0, decrements each session-start; reaches 0 → moves to retired/
"""


# Imprinted-rule schema (YAML). Mirrors plugin convention.
IMPRINTED_RULE_SCHEMA: str = """\
# Imprinted rule — schema reference (θ MCP resource)
#
# Imprinted rules are explicit operator preferences captured during
# /allostat-init or /allostat-calibrate. They map 1:1 to calibration
# questions (the bank at allostat://question-bank/calibration).
#
# v2.4 hosted MCP: wrapper writes these files to .allostat/imprinted/
# after the operator confirms calibration answers.

schema_version: 1
id: <calibration-question id, e.g., calib-q1>
domain: <e.g., deliverable_polish, project_planning, voice_consistency>
tier: user            # imprinted is always user-tier
question_id: <matching calibration-questions.yaml entry id>
choice: <a | b | c | d>           # operator's selected option
choice_label: <human-readable label of the selected option>
imprinted_at: <ISO-8601 UTC timestamp>
amplification: |
  <optional free-text operator amplification on the choice>
rule_text: |
  Default behavior derived from operator's choice — used by the
  hypothalamic-axis at session-start as part of operator's static state.
last_amplified: <ISO-8601 when operator most recently re-confirmed>
"""


# ============================================================================
# Register resources on FastMCP
# ============================================================================


def register_resources(server: FastMCP) -> None:
    """Register all rules-library resources on `server`."""

    @server.resource(
        "allostat://rules/innate",
        name="Innate rules index",
        description=(
            "JSON index of every innate guardrail rule the server hosts. "
            "Each entry carries id, name, severity, tier, description, "
            "trigger signals, source file. Wrapper fetches once per "
            "session and uses the index to know which rules exist + which "
            "signals trigger them. Individual rule bodies at "
            "allostat://rules/innate/<rule_id>."
        ),
        mime_type="application/json",
    )
    def innate_rules_index() -> str:
        rules = list_innate_rules()
        return json.dumps({"rules": rules, "count": len(rules)}, indent=2)

    @server.resource(
        "allostat://rules/learned/schema",
        name="Learned rules schema",
        description=(
            "YAML schema reference for learned rules. The wrapper writes "
            "rule files matching this schema to .allostat/rules/learned/ "
            "after operator-approved promotions from pattern-observer."
        ),
        mime_type="application/yaml",
    )
    def learned_rule_schema() -> str:
        return LEARNED_RULE_SCHEMA

    @server.resource(
        "allostat://rules/imprinted/schema",
        name="Imprinted rules schema",
        description=(
            "YAML schema reference for imprinted rules. Wrapper writes "
            "imprinted rule files to .allostat/imprinted/ after the "
            "operator confirms a calibration answer."
        ),
        mime_type="application/yaml",
    )
    def imprinted_rule_schema() -> str:
        return IMPRINTED_RULE_SCHEMA
