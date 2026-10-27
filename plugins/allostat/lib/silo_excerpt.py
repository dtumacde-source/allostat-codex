"""Compose excerpt.retrievals from operator-local silo files.

PATCH-194 (2026-05-23) — Phase 3.4b drift silo retrieval. Closes the
v2.5 silo inscribe→retrieve gap: PATCH-192 ported the per-class inscribe
primitives; PATCH-194 wires the wrapper to read inscribed entries from
`<state_dir>/silos/<class>.jsonl` and pass them as `excerpt.retrievals`
on each dispatch call so the server's `recall_silos` pillar can match
queries against them.

Privacy moat: silo files live on the operator's local filesystem; the
wrapper sends them as excerpt slices on each call. The server's
`recall_silos.query_entries` filters by fingerprint hash and returns only
matches — non-matching entries are RAM-only on the server per
Architecture C.

Future cascade: PATCH-194 ships drift first. PATCH-195/196/197/198
mechanically replicate the dispatcher emit + this reader for voice /
customization / workflow / confidence_recovery. This module already
handles all 5 silo classes when their files exist, so the cascade is
read-side ready.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SILO_CLASSES: tuple[str, ...] = (
    "drift",
    "voice",
    "customization",
    "workflow",
    "confidence_recovery",
)

# Cap on entries per silo class. Defends against operator silos growing
# unbounded; keeps excerpt.retrievals slim (target ≤500 KB per call per
# pillar_protocol.ExcerptPayload docstring). Most-recent N entries win;
# silo files are append-only so file order == time order.
MAX_ENTRIES_PER_CLASS = 100


def read_silo_entries_for_excerpt(
    state_dir: Path,
    classes: tuple[str, ...] = SILO_CLASSES,
    max_per_class: int = MAX_ENTRIES_PER_CLASS,
) -> list[dict[str, Any]]:
    """Read silo entries from `<state_dir>/silos/<class>.jsonl` files.

    Returns a flat list of dicts ready to ship as `excerpt.retrievals`.
    Per-class: read every JSONL line, skip blanks + malformed lines + entries
    with `decayed=True`, then keep the most recent `max_per_class` entries.
    Caller's responsibility to handle empty result (means no silos inscribed
    yet, which is the v2.5-pre-PATCH-194 baseline for every operator).

    Robust to: missing silos/ directory, missing per-class files, malformed
    JSONL lines, IOError on individual files (other files still read).

    Phase 2 privacy invariant (2026-05-28): each returned entry is passed
    through `_strip_canonical_for_wire` to guarantee `resolution.canonical_value`
    and `fingerprint.raw_excerpt` never appear in the wire payload. The
    storage layer (silo_base + migration) is responsible for stripping at
    write-time; this strip is belt-and-suspenders for any entry that slipped
    through from a pre-Phase-2 inscribe or a future caller that bypasses
    the storage layer.
    """
    entries: list[dict[str, Any]] = []
    silos_dir = state_dir / "silos"
    if not silos_dir.is_dir():
        return entries

    for class_name in classes:
        path = silos_dir / f"{class_name}.jsonl"
        if not path.is_file():
            continue
        class_entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("decayed"):
                        continue
                    class_entries.append(_strip_canonical_for_wire(entry))
        except OSError:
            continue
        if len(class_entries) > max_per_class:
            class_entries = class_entries[-max_per_class:]
        entries.extend(class_entries)

    return entries


def _strip_canonical_for_wire(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of entry with non-whitelist fields removed.

    Phase 2 (2026-05-28, post-audit) — flipped from deny-list to whitelist
    so future silo classes inherit the privacy invariant by construction.

    Delegates to `silo_base.strip_entry_for_wire`, which is the canonical
    implementation shared with `silo_base.write_entry` (storage-time strip)
    and `client_state_writer._append_jsonl` (apply-time defense-in-depth).

    Whitelist (post-audit):
      - `resolution`: keeps only `canonical_source` (reference string,
        no operator content).
      - `fingerprint`: keeps only `class_name` + `key_tokens` (identity
        surface).
      - Top-level fields (silo_class, success_marker, timestamp,
        session_id, fire_count, last_fire, decayed) pass through.

    Anything outside the whitelist is operator canonical text; it lives
    wrapper-local in `<state_dir>/silos/canonical_resolutions.jsonl`.
    """
    from silo_base import strip_entry_for_wire
    # hash_tokens=True: this is the wrapper->server WIRE path — replace raw
    # key_tokens (operator content) with their identity hash (ultraswarm
    # H-7/H-8). Disk-strip callers use the default (key_tokens preserved).
    return strip_entry_for_wire(entry, hash_tokens=True)


def compose_recall_enrichment(
    decisions: list[dict[str, Any]],
    state_dir: Path,
) -> str:
    """W5 (pillar-wiring 2026-06-05) — join recall_match hits to their
    wrapper-local canonical text.

    The recall-silos retrieve keystone (W1) makes the dispatcher emit
    `recall_match` decisions whose `details.matches` each carry a
    `fingerprint_hash`. The server surfaces hits HASH-ONLY — it cannot read the
    operator-local `<state_dir>/silos/canonical_resolutions.jsonl` (privacy moat,
    design_invariants §1). This is the wrapper-side half of the join: for each
    recall_match hash, look up the bound canonical text so Claude sees WHAT was
    resolved before, not just "N prior cases".

    Reads the dispatch response's `decisions` list (DispatchResponse.decisions).
    Returns "" when there is nothing to enrich (no recall_match decisions, or no
    bound hash) — the base recall body in additional_context still surfaces.
    Dedups repeated hashes. Best-effort: a lookup miss just omits that line.
    """
    from canonical_resolutions import lookup_canonical_value

    lines: list[str] = []
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if decision.get("action") != "recall_match":
            continue
        details = decision.get("details") or {}
        class_name = str(details.get("class_name") or "silo")
        for match in details.get("matches") or []:
            if not isinstance(match, dict):
                continue
            # Automatic voice detections are evidence, not accepted canonical
            # voice. This also makes legacy auto-bound rows inert: only an
            # acknowledged voice_corrected match may enrich with canonical text.
            if (
                class_name == "voice"
                and match.get("success_marker") != "voice_corrected"
            ):
                continue
            fp_hash = match.get("fingerprint_hash")
            if not fp_hash or str(fp_hash) in seen:
                continue
            text = lookup_canonical_value(state_dir, str(fp_hash))
            if not text:
                continue
            seen.add(str(fp_hash))
            snippet = " ".join(text[:160].split())
            lines.append(f'  · {class_name}: "{snippet}"')

    if not lines:
        return ""
    return (
        "[allostat / recall-silos] prior resolution(s) — you've been here "
        "before:\n" + "\n".join(lines)
    )
