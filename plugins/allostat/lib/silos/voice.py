"""Voice silo — recurring voice-drift patterns + canonical voice samples.

Ported from the v2.3 plugin's lib/silos/voice.py into the v2.4 wrapper.
Second silo class (after drift). Follows the same shape as drift.py.

Signal class: a voice violation pattern keeps recurring (operator
strikes the same kind of phrasing repeatedly). Resolution: a canonical
sample of how the operator's voice should sound for this class.
On future generation, recall this silo before producing similar prose.

Token construction MUST mirror server-side
`recall_silos.compute_class_tokens` for class_name="voice" exactly:

    return [violation_type, sample_text[:60].lower()]

Drift between wrapper + server breaks recall — keep in lockstep.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from silo_base import SiloEntry, SiloFingerprint, query_silo, write_entry

# Success markers for this class. Two of them, and the distinction is the
# whole point (2026-08-07, benchmark finding).
#
# The voice keeper detected 17 violations in one bench session, surfaced NONE
# of them to the operator, and wrote all 17 to the silo marked
# `voice_corrected` — a success marker for a correction that never happened.
# Anything learning downstream was being trained on a false success signal,
# which gets more confidently wrong the longer it runs.
#
# So: DETECTION and CORRECTION are different facts and carry different
# markers. The auto-detection path can no longer produce the corrected marker
# at all — not by convention, but because it has no function that emits it
# (`record_voice_correction` demands evidence that the correction was actually
# surfaced and acknowledged in-session, and refuses without it).
VOICE_DETECTED = "voice_detected"
VOICE_CORRECTED = "voice_corrected"


@dataclass
class VoiceFingerprint:
    """Inputs that identify a voice-drift pattern."""

    violation_type: str  # e.g., "ai_slop", "hedge", "sycophancy"
    sample_text: str  # the offending snippet (first 60 chars used for token)


@dataclass
class VoiceResolution:
    """Operator-acknowledged canonical sample for a corrected voice case."""

    canonical_sample: str  # the right way to phrase it
    canonical_source: str  # voice-reference file or memory ref


def _to_silo_fp(fp: VoiceFingerprint) -> SiloFingerprint:
    """Build the SiloFingerprint for a voice case.

    Token construction MUST mirror server's compute_class_tokens("voice")
    exactly: [violation_type, sample_text[:60].lower()].
    """
    return SiloFingerprint(
        class_name="voice",
        key_tokens=[fp.violation_type, fp.sample_text[:60].lower()],
        # Detection evidence stays in local key_tokens. It is not canonical
        # prose and must never be relocated into canonical_resolutions.
        raw_excerpt="",
    )


def _write(
    silo_path: Path,
    fingerprint: VoiceFingerprint,
    resolution: VoiceResolution | None,
    session_id: str,
    success_marker: str,
) -> None:
    """Shared inscribe. The marker is the caller's declaration of what
    actually happened; the two public entry points below are the only
    things allowed to choose it."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = SiloEntry(
        fingerprint=_to_silo_fp(fingerprint),
        resolution=(
            {
                "canonical_sample": resolution.canonical_sample,
                "canonical_source": resolution.canonical_source,
            }
            if resolution is not None
            else {}
        ),
        success_marker=success_marker,
        timestamp=now,
        session_id=session_id,
        fire_count=1,
        last_fire=now,
    )
    write_entry(silo_path, entry)


def record_voice_detection(
    silo_path: Path,
    fingerprint: VoiceFingerprint,
    resolution: VoiceResolution | str | None = None,
    session_id: str = "",
) -> None:
    """Inscribe a DETECTED voice-drift case — nothing was corrected.

    This is the automatic path: the voice keeper scanned a turn, matched a
    marker, and recorded the case so future encounters fingerprint-match
    against accumulated history. Nothing was shown to the operator and
    nothing was agreed, so the row says exactly that (`voice_detected`).

    Repeat detections of the same case coalesce onto one row with a rising
    `fire_count` (silo_base.write_entry) — 18 rows sharing one fingerprint
    is not evidence of 18 cases, it is one case seen 18 times.

    Args:
        silo_path: Path to voice.jsonl (use silo_base.resolve_silo_path).
        fingerprint: The voice case being recorded.
        resolution: Deprecated and ignored. Detections cannot establish a
            canonical voice sample; retained only for old caller compatibility.
        session_id: The session in which the detection landed.
    """
    if isinstance(resolution, str) and not session_id:
        session_id = resolution
    _write(silo_path, fingerprint, None, session_id, VOICE_DETECTED)


def record_voice_correction(
    silo_path: Path,
    fingerprint: VoiceFingerprint,
    resolution: VoiceResolution,
    session_id: str,
    *,
    surfaced_ref: str,
) -> None:
    """Inscribe a voice case that WAS surfaced and acknowledged.

    `surfaced_ref` is the evidence that the correction actually reached the
    operator — the nudge/observation reference for the turn where it was
    shown. Without it there is no correction to record, so a blank ref is a
    ValueError rather than a silently-optimistic row.

    Nothing in the wrapper calls this yet: the surfacing half of the loop is
    not built (that is what makes "it learns your corrections" untrue today).
    It exists so the corrected marker has exactly one gated way in, instead of
    being the default any detection could reach.

    Raises:
        ValueError: if surfaced_ref is empty or whitespace.
    """
    if not (surfaced_ref or "").strip():
        raise ValueError(
            "record_voice_correction requires surfaced_ref — a correction that "
            "was never surfaced to the operator is not a correction. Use "
            "record_voice_detection for automatic detections."
        )
    _write(silo_path, fingerprint, resolution, session_id, VOICE_CORRECTED)


def record_voice_resolution(
    silo_path: Path,
    fingerprint: VoiceFingerprint,
    resolution: VoiceResolution,
    session_id: str,
) -> None:
    """Back-compat shim — routes to `record_voice_detection`.

    Kept so an older call site cannot fail loudly, but deliberately routed to
    the DETECTION marker and its supplied resolution is discarded. A caller
    that did not prove a correction was surfaced cannot establish canonical
    voice. Prefer the explicit functions above.
    """
    record_voice_detection(silo_path, fingerprint, resolution, session_id)


def query_voice(
    silo_path: Path,
    fingerprint: VoiceFingerprint,
    top_n: int = 5,
) -> list[SiloEntry]:
    """Local wrapper-side query against the voice silo."""
    return query_silo(silo_path, _to_silo_fp(fingerprint), top_n=top_n)
