"""Drift silo — stale-fact-in-context cases + canonical resolutions.

Ported from the v2.3 plugin's lib/silos/drift.py into the v2.4 wrapper.
This is the first silo class ported per advisor Path-beta (PATCH-167):
drift first, soak on prod, then voice / customization / workflow /
confidence_recovery as separate patches.

Signal class: a fact asserted in context contradicts a newer memory file.
Resolution: the canonical value + the source it came from. On re-encounter
of the same claim, recall this silo before re-asserting the stale fact.

Token construction MUST mirror server-side recall_silos.compute_class_tokens
for class_name="drift" exactly — drift between server + wrapper breaks
the recall pillar:

    return [claim[:80].lower(), claimed_in[:40]]

Origin: drift_cascade.py + project_allostat_v2_todos.md item #3 (the
percentage-vs-volume drift event 2026-05-04).
"""
from __future__ import annotations

from dataclasses import dataclass

# Lazy sys.path setup: silo_base lives in lib/ alongside this silos/ dir.
# When invoked from hooks (sys.path already has lib/), the direct import works.
# When invoked from tests via importlib, lib/ may not yet be on sys.path
# — caller responsible for that setup before importing this module.
from silo_base import SiloFingerprint


@dataclass
class DriftFingerprint:
    """Inputs that identify a drift case."""

    claim: str  # the stale assertion
    claimed_in: str  # source where claim appeared (response_text, file path)


@dataclass
class DriftResolution:
    """Canonical correction for a drift case."""

    canonical_value: str
    canonical_source: str  # file path or memory ref


def _to_silo_fp(fp: DriftFingerprint) -> SiloFingerprint:
    """Build the SiloFingerprint for a drift case.

    Token construction MUST mirror server's compute_class_tokens("drift")
    exactly: [claim[:80].lower(), claimed_in[:40]]. Drift would break recall.
    """
    return SiloFingerprint(
        class_name="drift",
        key_tokens=[fp.claim[:80].lower(), fp.claimed_in[:40]],
        raw_excerpt=fp.claim,
    )
