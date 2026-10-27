"""Customization silo — operator customization decisions + rationales.

Ported from the v2.3 plugin's lib/silos/customization.py into the v2.4
wrapper. Third silo class.

Signal class: operator made a customization decision (chose option X
from a menu of options for decision-class Y). Resolution: the chosen
option + rationale. On future invocations of the same decision-class
with the same option-set, recall this silo before re-presenting the
menu.

Token construction MUST mirror server-side
`recall_silos.compute_class_tokens` for class_name="customization"
exactly:

    return [decision_class, *sorted(str(o) for o in options)]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from silo_base import SiloEntry, SiloFingerprint, query_silo, write_entry


@dataclass
class CustomizationFingerprint:
    """Inputs that identify a customization decision."""

    decision_class: str  # e.g., "ship_pipeline", "test_strategy"
    options_seen: list[str] = field(default_factory=list)


@dataclass
class CustomizationResolution:
    """The chosen option + rationale."""

    chosen_option: str
    rationale: str


def _to_silo_fp(fp: CustomizationFingerprint) -> SiloFingerprint:
    """Build the SiloFingerprint for a customization case.

    Token construction MUST mirror server's compute_class_tokens(
    "customization") exactly: [decision_class, *sorted(options)].
    """
    options_sorted = sorted(str(o) for o in fp.options_seen)
    return SiloFingerprint(
        class_name="customization",
        key_tokens=[fp.decision_class, *options_sorted],
        raw_excerpt=f"{fp.decision_class}: {', '.join(options_sorted)}",
    )


def record_customization_decision(
    silo_path: Path,
    fingerprint: CustomizationFingerprint,
    resolution: CustomizationResolution,
    session_id: str,
) -> None:
    """Inscribe an operator customization decision to the silo."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = SiloEntry(
        fingerprint=_to_silo_fp(fingerprint),
        resolution={
            "chosen_option": resolution.chosen_option,
            "rationale": resolution.rationale,
        },
        success_marker="decision_recorded",
        timestamp=now,
        session_id=session_id,
        fire_count=1,
        last_fire=now,
    )
    write_entry(silo_path, entry)


def query_customization(
    silo_path: Path,
    fingerprint: CustomizationFingerprint,
    top_n: int = 5,
) -> list[SiloEntry]:
    """Local wrapper-side query against the customization silo."""
    return query_silo(silo_path, _to_silo_fp(fingerprint), top_n=top_n)
