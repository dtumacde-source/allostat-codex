"""Workflow silo — workflow gotchas + canonical fixes per phase.

Ported from the v2.3 plugin's lib/silos/workflow.py into the v2.4
wrapper. Fourth silo class.

Signal class: a specific workflow phase has a recurring gotcha (e.g.,
"deploy phase: forgot to retarget symlinks before restart"). Resolution:
the canonical fix + lesson. On the next encounter of the same
workflow-phase pair, recall this silo before re-executing.

Token construction MUST mirror server-side
`recall_silos.compute_class_tokens` for class_name="workflow" exactly:

    return [workflow_file, phase, gotcha[:60].lower()]
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from silo_base import SiloEntry, SiloFingerprint, query_silo, write_entry


@dataclass
class WorkflowFingerprint:
    """Inputs that identify a workflow gotcha."""

    workflow_file: str  # e.g., "deploy.sh", "promote_staging_to_prod.sh"
    phase: str  # e.g., "pre-deploy backup", "atomic-swap"
    gotcha: str  # short description of the gotcha


@dataclass
class WorkflowResolution:
    """Canonical fix + lesson for the workflow gotcha."""

    canonical_fix: str
    lesson: str  # one-line takeaway


def _to_silo_fp(fp: WorkflowFingerprint) -> SiloFingerprint:
    """Build the SiloFingerprint for a workflow case.

    Token construction MUST mirror server's compute_class_tokens(
    "workflow") exactly: [workflow_file, phase, gotcha[:60].lower()].
    """
    return SiloFingerprint(
        class_name="workflow",
        key_tokens=[fp.workflow_file, fp.phase, fp.gotcha[:60].lower()],
        raw_excerpt=fp.gotcha,
    )


def record_workflow_gotcha(
    silo_path: Path,
    fingerprint: WorkflowFingerprint,
    resolution: WorkflowResolution,
    session_id: str,
) -> None:
    """Inscribe a workflow gotcha + canonical fix to the silo."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = SiloEntry(
        fingerprint=_to_silo_fp(fingerprint),
        resolution={
            "canonical_fix": resolution.canonical_fix,
            "lesson": resolution.lesson,
        },
        success_marker="gotcha_recorded",
        timestamp=now,
        session_id=session_id,
        fire_count=1,
        last_fire=now,
    )
    write_entry(silo_path, entry)


def query_workflow(
    silo_path: Path,
    fingerprint: WorkflowFingerprint,
    top_n: int = 5,
) -> list[SiloEntry]:
    """Local wrapper-side query against the workflow silo."""
    return query_silo(silo_path, _to_silo_fp(fingerprint), top_n=top_n)
