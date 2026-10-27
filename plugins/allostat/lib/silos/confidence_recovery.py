"""Confidence-recovery silo — topics + actions that restored confidence.

Ported from the v2.3 plugin's lib/silos/confidence_recovery.py into the
v2.4 wrapper. Fifth and final silo class.

Signal class: regulator detected a low-confidence state on a specific
topic; some action restored confidence to a normal level. Resolution:
the recovery action + ending confidence. On future low-confidence on
the same topic, recall this silo before re-trying random recoveries.

Token construction MUST mirror server-side
`recall_silos.compute_class_tokens` for class_name="confidence_recovery"
exactly:

    return [topic[:60].lower(), f"start_{rounded}"]

where `rounded` is the starting_confidence rounded to 1 decimal place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from silo_base import SiloEntry, SiloFingerprint, query_silo, write_entry


@dataclass
class ConfidenceRecoveryFingerprint:
    """Inputs that identify a confidence-recovery case."""

    topic: str  # e.g., "deploy via staging promotion"
    starting_confidence: float  # 0.0..1.0 — rounded to 1 decimal for fingerprint


@dataclass
class ConfidenceRecoveryResolution:
    """The recovery action + ending confidence."""

    recovery_action: str
    ending_confidence: float  # 0.0..1.0 — typically > starting


def _to_silo_fp(fp: ConfidenceRecoveryFingerprint) -> SiloFingerprint:
    """Build the SiloFingerprint for a confidence-recovery case.

    Token construction MUST mirror server's compute_class_tokens(
    "confidence_recovery") exactly: [topic[:60].lower(), f"start_{rounded}"].
    """
    rounded = round(float(fp.starting_confidence), 1)
    return SiloFingerprint(
        class_name="confidence_recovery",
        key_tokens=[fp.topic[:60].lower(), f"start_{rounded}"],
        raw_excerpt=f"{fp.topic} (confidence start {rounded})",
    )


def record_confidence_recovery(
    silo_path: Path,
    fingerprint: ConfidenceRecoveryFingerprint,
    resolution: ConfidenceRecoveryResolution,
    session_id: str,
) -> None:
    """Inscribe a confidence-recovery case to the silo."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = SiloEntry(
        fingerprint=_to_silo_fp(fingerprint),
        resolution={
            "recovery_action": resolution.recovery_action,
            "ending_confidence": resolution.ending_confidence,
        },
        success_marker="confidence_recovered",
        timestamp=now,
        session_id=session_id,
        fire_count=1,
        last_fire=now,
    )
    write_entry(silo_path, entry)


def query_confidence_recovery(
    silo_path: Path,
    fingerprint: ConfidenceRecoveryFingerprint,
    top_n: int = 5,
) -> list[SiloEntry]:
    """Local wrapper-side query against the confidence-recovery silo."""
    return query_silo(silo_path, _to_silo_fp(fingerprint), top_n=top_n)
