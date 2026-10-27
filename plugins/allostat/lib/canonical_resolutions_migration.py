"""Migration: relocate canonical_value from silo entries to canonical_resolutions.

Phase 2 (2026-05-28). On first SessionStart after the wrapper carrying this
module ships, walk every existing `<state_dir>/silos/<class>.jsonl` file.
For each entry that carries `resolution.canonical_value` and/or
`fingerprint.raw_excerpt` (pre-Phase-2 shape), extract the canonical text,
write it to `canonical_resolutions.jsonl`, and rewrite the silo entry in
its post-Phase-2 shape (hash + provenance only).

Idempotent. If an entry is already in post-Phase-2 shape (no canonical_value,
no raw_excerpt), it stays untouched. The migration runs every SessionStart
but does nothing on second + subsequent runs.

Marker file: `<state_dir>/silos/.phase2_migrated` written after the first
successful pass so subsequent runs short-circuit without re-walking. The
marker contains the migration's run timestamp + counts for observability.

Failure semantics: any per-file error (OSError, JSON parse error) is caught,
logged via the returned report, and the rest of the migration continues.
The migration NEVER deletes or corrupts operator data — it only adds the
canonical_resolutions entries and rewrites in place via atomic tmp+replace.

Not run on silos that don't exist. If an operator has no silos directory
(the common case pre-PATCH-194 inscribe activity), migration is a no-op.
"""
from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import canonical_resolutions


MIGRATION_MARKER_NAME = ".phase2_migrated"


@dataclass
class MigrationReport:
    """Outcome of one migration pass. Useful for tests + observation hooks."""

    silos_dir_present: bool = False
    already_migrated: bool = False
    files_walked: int = 0
    entries_inspected: int = 0
    entries_migrated: int = 0
    canonical_bindings_written: int = 0
    files_rewritten: int = 0
    errors: list[str] = field(default_factory=list)


def run(state_dir: Path) -> MigrationReport:
    """Run the Phase 2 silo migration once for this state_dir.

    Returns a MigrationReport describing what happened. Never raises; any
    failure is recorded in `report.errors` and the rest of the migration
    continues.
    """
    report = MigrationReport()
    silos_dir = state_dir / "silos"
    if not silos_dir.exists() or not silos_dir.is_dir():
        return report
    report.silos_dir_present = True

    marker = silos_dir / MIGRATION_MARKER_NAME
    if marker.exists():
        report.already_migrated = True
        return report

    # Walk every <class>.jsonl file in the silos dir. Skip the canonical
    # resolutions file itself + the migration marker + anything not .jsonl.
    skip_names = {
        "canonical_resolutions.jsonl",
        MIGRATION_MARKER_NAME,
    }
    for silo_path in sorted(silos_dir.iterdir()):
        if not silo_path.is_file():
            continue
        if silo_path.name in skip_names:
            continue
        if silo_path.suffix != ".jsonl":
            continue
        try:
            _migrate_one_file(state_dir, silo_path, report)
        except Exception as e:  # broad: never let one file kill the run
            report.errors.append(f"{silo_path.name}:{type(e).__name__}:{e}")

    # Write marker even if zero files were touched — captures "we ran" so
    # subsequent sessions short-circuit.
    try:
        marker.write_text(
            json.dumps(
                {
                    "migrated_at": _utc_iso_now(),
                    "files_walked": report.files_walked,
                    "entries_inspected": report.entries_inspected,
                    "entries_migrated": report.entries_migrated,
                    "canonical_bindings_written": report.canonical_bindings_written,
                    "files_rewritten": report.files_rewritten,
                    "errors": report.errors,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        report.errors.append(f"marker:{type(e).__name__}:{e}")

    return report


def _silo_lock(silo_path: Path):
    """The shared JSONL lock for `silo_path`, or a no-op in a degraded install.

    Same contract and same sidecar (`<file>.lock`) as
    `local_state.append_observation`, `client_state_writer._append_jsonl` and
    `silo_base.write_entry`, so the migration and the ordinary writers exclude
    each other. Best-effort by design — a wrapper that cannot import
    local_state still migrates rather than refusing to start.
    """
    try:
        from local_state import jsonl_lock

        return jsonl_lock(silo_path)
    except ImportError:
        return contextlib.nullcontext()


def _migrate_one_file(
    state_dir: Path,
    silo_path: Path,
    report: MigrationReport,
) -> None:
    report.files_walked += 1

    # Determine silo_class from filename (drift.jsonl -> drift).
    silo_class = silo_path.stem

    # M-04 class (audit 2026-07-21): the read..replace below is held under the
    # silo's JSONL lock. It was not, and the comment on the tmp filename below
    # shows the authors had already reasoned about two concurrent SessionStart
    # migrations — they closed the tmp-name collision (H-16) and left the window
    # against ORDINARY silo writers open. Any entry appended between the read
    # and the `tmp_path.replace(silo_path)` was silently discarded, because the
    # replace promotes a snapshot taken before it. Locking one side buys
    # nothing, so this lands together with the `silo_base.write_entry` lock.
    #
    # Lock ordering: this holds the SILO lock and, via `_migrate_entry_inplace`
    # -> `inscribe_payload`, acquires the CANONICAL lock inside it. That is the
    # only nesting in the codebase and it is one-directional — no writer takes
    # the canonical lock and then a silo lock — so there is no cycle. The two
    # paths are different files, so `jsonl_lock`'s non-reentrancy is not an
    # issue either.
    with _silo_lock(silo_path):
        # Read all entries; remember which were rewritten.
        rewrites_needed = False
        new_entries: list[dict[str, Any]] = []
        try:
            with silo_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    report.entries_inspected += 1
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        # Corrupt line — preserve as-is; don't lose the bytes.
                        new_entries.append({"__raw_line__": line})
                        continue

                    rewrote = _migrate_entry_inplace(
                        state_dir, silo_class, entry, report,
                    )
                    if rewrote:
                        rewrites_needed = True
                    new_entries.append(entry)
        except OSError as e:
            report.errors.append(f"{silo_path.name}:read:{e.__class__.__name__}:{e}")
            return

        if not rewrites_needed:
            return

        # Atomic rewrite via tmp + replace. The tmp filename MUST be
        # process-unique (H-16): two concurrent SessionStart migrations run
        # against the same state_dir with no lock (the marker check is a plain
        # check-then-act). A deterministic `<silo>.jsonl.phase2tmp` shared name
        # lets their writes interleave into one file, and whichever os.replace()
        # runs last promotes corrupted/half-written bytes over the silo.
        tmp_path = _phase2_tmp_path(silo_path)
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                for entry in new_entries:
                    if "__raw_line__" in entry:
                        f.write(entry["__raw_line__"] + "\n")
                    else:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            tmp_path.replace(silo_path)
            report.files_rewritten += 1
        except OSError as e:
            report.errors.append(f"{silo_path.name}:write:{e.__class__.__name__}:{e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                # Best-effort cleanup of the tmp file after a failed rewrite.
                # If the unlink itself fails (file locked, permission), leaving
                # the tmp file behind is harmless — it carries a process-unique
                # .phase2tmp name so it collides with nothing and is inert.
                pass


def _migrate_entry_inplace(
    state_dir: Path,
    silo_class: str,
    entry: dict[str, Any],
    report: MigrationReport,
) -> bool:
    """Strip non-whitelist canonical fields; binding -> canonical_resolutions.

    Whitelist semantics (Phase 2 post-audit 2026-05-28):
      - resolution: keep only `canonical_source`; relocate everything else
        (e.g., canonical_value [drift], canonical_sample [voice],
        canonical_fix + lesson [workflow], chosen_option + rationale
        [customization], recovery_action + ending_confidence [confidence_recovery]).
      - fingerprint: keep only `class_name` + `key_tokens`; relocate raw_excerpt.

    Write-then-strip (RB-2 / audit C-02, 2026-07): the canonical fields are
    only removed from `entry` after `inscribe_payload` reports the payload
    was actually persisted to canonical_resolutions.jsonl. If the write is
    skipped (e.g. no usable fingerprint) or fails (returns False), the
    entry is left completely unchanged and is NOT counted as migrated —
    stripping first was silent data loss, since a skipped/failed write
    meant the operator's canonical text existed nowhere.

    Returns True if the entry was modified (caller will rewrite the file).
    """
    # Import here to avoid circular import at module load.
    try:
        from silo_base import (
            FINGERPRINT_WIRE_KEYS,
            RESOLUTION_WIRE_KEYS,
        )
    except ImportError:
        # silo_base unavailable — fall back to legacy two-field strip
        RESOLUTION_WIRE_KEYS = frozenset({"canonical_source"})
        FINGERPRINT_WIRE_KEYS = frozenset({"class_name", "key_tokens"})

    payload: dict[str, Any] = {}
    raw_excerpt = ""
    would_modify = False

    # Determine which resolution fields would be relocated, without
    # mutating `entry` yet.
    resolution = entry.get("resolution")
    resolution_pop_keys: list[Any] = []
    if isinstance(resolution, dict):
        resolution_pop_keys = [
            k for k in resolution.keys() if k not in RESOLUTION_WIRE_KEYS
        ]
        for k in resolution_pop_keys:
            payload[k] = resolution[k]
            would_modify = True

    # Determine which fingerprint fields would be relocated.
    fingerprint = entry.get("fingerprint")
    fingerprint_pop_keys: list[Any] = []
    if isinstance(fingerprint, dict):
        fingerprint_pop_keys = [
            k for k in fingerprint.keys() if k not in FINGERPRINT_WIRE_KEYS
        ]
        for k in fingerprint_pop_keys:
            v = fingerprint[k]
            would_modify = True
            # raw_excerpt has a dedicated slot in canonical_resolutions
            # (it's the human-readable surface); other fingerprint fields
            # we don't expect post-Phase-2 — collect into payload as a
            # forensic safety net.
            if k == "raw_excerpt" and isinstance(v, str):
                raw_excerpt = v
            else:
                payload[f"fingerprint_extra_{k}"] = v

    if not would_modify:
        return False

    # Compute the fingerprint hash for binding. Reads only class_name +
    # key_tokens, which are whitelisted and therefore never popped, so this
    # is safe to compute before any mutation.
    fingerprint_hash = _hash_from_entry(entry)

    if not (fingerprint_hash and (payload or raw_excerpt)):
        # Nothing safe to write (e.g. no usable fingerprint). Leave the
        # entry entirely untouched and don't count it as migrated —
        # stripping here would destroy the operator's canonical text with
        # no copy surviving anywhere.
        return False

    ok = canonical_resolutions.inscribe_payload(
        state_dir,
        fingerprint_hash=fingerprint_hash,
        payload=payload,
        silo_class=silo_class,
        session_id=str(entry.get("session_id") or ""),
        raw_excerpt=raw_excerpt,
        now_iso=str(entry.get("timestamp") or "") or None,
    )
    if not ok:
        # Write failed. Leave the entry entirely untouched and don't count
        # it as migrated — the fields we'd strip have no surviving copy.
        return False

    report.canonical_bindings_written += 1

    # Write succeeded: now — and only now — actually strip the fields.
    for k in resolution_pop_keys:
        resolution.pop(k, None)  # type: ignore[union-attr]
    for k in fingerprint_pop_keys:
        fingerprint.pop(k, None)  # type: ignore[union-attr]

    report.entries_migrated += 1
    return True


def _phase2_tmp_path(silo_path: Path) -> Path:
    """Process-unique sibling tmp path for the atomic silo rewrite (H-16).

    Mirrors the pid+uuid tmp convention already used by
    `local_state.atomic_write_text` and `innate_consent.consume_atomically`.
    The tmp stays a sibling of `silo_path` (same directory / filesystem) so
    the subsequent `os.replace()` remains atomic, while the pid + uuid4 token
    guarantees two concurrent, unsynchronized migrations never write to the
    same tmp file and clobber each other's output.
    """
    unique = f"{os.getpid()}.{uuid.uuid4().hex}"
    return silo_path.with_name(f"{silo_path.name}.{unique}.phase2tmp")


def _hash_from_entry(entry: dict[str, Any]) -> str:
    """Compute fingerprint hash from entry; mirrors silo_base + recall_silos."""
    fp = entry.get("fingerprint")
    if not isinstance(fp, dict):
        return ""
    class_name = fp.get("class_name")
    tokens = fp.get("key_tokens", [])
    if not class_name or not isinstance(tokens, list):
        return ""
    import hashlib

    seed = f"{class_name}::{':'.join(sorted(str(t) for t in tokens))}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
