"""Client-side pattern-observer proposal generation — data-min Option A.

Runs the pattern-learning detection LOCALLY on the operator's own observations and
emits the exact `pending_proposals.jsonl` records the server's pattern_observer
pillar emits — so the learned-rule loop keeps working without shipping operator
language over the wire. Mirrors the wire-privacy pattern of `drift_recurrence_local`
and `voice_keeper_local`.

Migration-safe: records are deduped downstream by `hash` (proposal_review), and the
client hash is byte-identical to the server's (`pattern_fingerprint_local` is
LOCKSTEP), so this runs in PARALLEL with the server path without surfacing duplicate
proposals. Once the wire tail is stripped of operator language (later slice), the
server path fingerprints nothing and only this local path remains.

Record shapes are LOCKSTEP with server pattern_observer._detect_proposals_response
(kind="n5_proposal") and _detect_in_session_overrides_response
(kind="in_session_override").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pattern_fingerprint_local import (
    DEFAULT_N_THRESHOLD,
    detect_in_session_overrides,
    detect_proposals,
)

PENDING_PROPOSALS_FILE = "pending_proposals.jsonl"


def _existing_proposal_hashes(state_dir: Path) -> set[str]:
    """Return the fingerprint hashes already present in pending_proposals.jsonl.

    This is the write-time dedup key — the SAME key proposal_review dedups by
    on read (record["hash"], falling back to proposal.hash). Reading it here lets
    generate_and_persist skip appending a proposal whose equivalent already sits
    in the store, so the append-only log stops accumulating physical duplicates
    (M-01). Malformed/missing lines are skipped; a corrupt log must not block the
    dedup, only degrade it toward the reader-side backstop.
    """
    path = state_dir / PENDING_PROPOSALS_FILE
    if not path.is_file():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    hashes: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        inner = record.get("proposal")
        h = record.get("hash") or (inner.get("hash") if isinstance(inner, dict) else None)
        if h:
            hashes.add(str(h))
    return hashes


def build_proposal_records(
    observations: list[dict[str, Any]],
    *,
    now_iso: str,
    current_session_id: str | None = None,
    n4_threshold: int = DEFAULT_N_THRESHOLD,
    n2_threshold: int = 2,
) -> list[dict[str, Any]]:
    """Return the pending_proposals.jsonl records for `observations`.

    N=4 cross-session proposals (kind="n5_proposal") + N=2 in-session override
    proposals (kind="in_session_override"). `now_iso` is the record timestamp
    (injected so the caller controls the clock and tests stay deterministic).
    Each record is keyed by the fingerprint `hash` — the dedup key the terminal
    proposal_review reader uses.
    """
    records: list[dict[str, Any]] = []

    for p in detect_proposals(observations, n_threshold=n4_threshold):
        records.append({
            "timestamp": now_iso,
            "kind": "n5_proposal",
            "n_threshold": n4_threshold,
            "hash": p["hash"],
            "proposal": p,
        })

    for p in detect_in_session_overrides(
        observations,
        current_session_id=current_session_id,
        n_threshold=n2_threshold,
    ):
        records.append({
            "timestamp": now_iso,
            "kind": "in_session_override",
            "n_threshold": n2_threshold,
            "current_session_id": current_session_id,
            "hash": p["hash"],
            "proposal": p,
        })

    return records


def generate_and_persist(
    state_dir: Path,
    *,
    current_session_id: str | None = None,
    now_iso: str | None = None,
    window: int = 200,
) -> int:
    """Read the operator's local observations, generate pattern proposals, and
    append them to `pending_proposals.jsonl`. Returns the count applied.

    This is the client-side replacement for the server pattern_observer path: it
    runs on the FULL local observations (never the scrubbed wire copy), so it keeps
    working once operator language is stripped from the wire. Best-effort — the
    hook caller wraps it so a failure never breaks the turn. Lazy imports keep the
    pure record builder (build_proposal_records) importable without I/O deps.
    """
    from local_state import (
        OBSERVATIONS_KEEP_RECENT_ENTRIES,
        OBSERVATIONS_ROTATE_THRESHOLD_BYTES,
        jsonl_lock,
        read_recent_observations,
        rotate_jsonl_if_oversize,
    )

    if now_iso is None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    records = build_proposal_records(
        read_recent_observations(state_dir, window=window),
        now_iso=now_iso,
        current_session_id=current_session_id,
    )
    if not records:
        return 0

    # M-01 (2026-07-15, atomicity fix 2026-07-16): dedup at WRITE time.
    # pending_proposals.jsonl is append-only and proposal generation re-runs
    # every prompt, so absent this guard the SAME fingerprint is re-appended on
    # every turn and the store grows unbounded with physical duplicates — even
    # though the reader collapses them.
    #
    # The check (read existing hashes) and the append MUST be ONE atomic critical
    # section. The prior version read existing hashes, then handed the append to
    # apply_writes, which locked only the append — so two concurrent callers both
    # read an empty store, both passed the dedup, and both appended the SAME
    # fingerprint (two physical rows, one unique hash). We now hold the JSONL lock
    # across the whole read-dedup-append so a second caller blocks until the first
    # has appended and then sees its records (deduping them away). Batch-internal
    # dedup (two records sharing a hash) is folded into the same pass. Append is
    # done directly here (not via apply_writes) because apply_writes re-takes this
    # same non-reentrant lock per write, which would self-deadlock under it.
    state_dir.mkdir(parents=True, exist_ok=True)
    pending_path = state_dir / PENDING_PROPOSALS_FILE
    applied = 0
    with jsonl_lock(pending_path):
        seen: set[str] = set(_existing_proposal_hashes(state_dir))
        with pending_path.open("a", encoding="utf-8") as f:
            for r in records:
                h = str(r.get("hash") or "")
                if h and h in seen:
                    continue
                if h:
                    seen.add(h)
                f.write(json.dumps(r, default=str) + "\n")
                applied += 1

    # Rotate outside the lock (rotate re-takes the same lock; never nest it).
    # Best-effort — matches the append path in client_state_writer.
    if applied:
        try:
            rotate_jsonl_if_oversize(
                pending_path,
                OBSERVATIONS_ROTATE_THRESHOLD_BYTES,
                OBSERVATIONS_KEEP_RECENT_ENTRIES,
            )
        except (OSError, ImportError):
            pass
    return applied
