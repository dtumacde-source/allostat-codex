"""Client state writer — v0.2.0 Option Z protocol.

The hosted MCP server returns `client_state_writes` instructions inside
`DispatchResponse`. This module:
  1. Validates each write against a whitelist of target filenames
  2. Validates the mode is compatible with the target shape (.jsonl=append,
     .json=overwrite/merge)
  3. Applies the write to `<state_dir>/<target>` on the operator's local
     filesystem
  4. Maintains an audit log of every accepted/rejected write at
     `<state_dir>/server_instructed_writes.jsonl` so the operator can
     forensically inspect what the server told the wrapper to persist

Privacy posture: server NEVER stores the content. The instruction is
composed in RAM on the server, transmitted to the wrapper, applied here,
and the server's copy is released. Only the operator's local filesystem
holds the persistent state. The audit log here is local-only too.

Whitelist (mirrors the server's ClientStateWrite Pydantic Literal):
  nudge_history.jsonl          append-only audit of fired nudges
  pending_proposals.jsonl      pattern-observer proposals between sessions
  pillar_history.jsonl         per-pillar decision history
  silo_entry.jsonl             routes to silos/<class>.jsonl by content.silo_class
  epigenetic_signature.json    learned operator preferences
  prediction_calibration.json  pillar accuracy tracking
  chronic_stress_baseline.json rolling baseline for cascade detection
  drift_seen.json              stress drift-seen dedup state

The wrapper rejects writes outside the whitelist, with invalid modes, or
that fail JSON-serializability. Rejections are logged to the audit log
with a `rejection_reason` field; never silently dropped.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Whitelist of write target filenames. Must match the server-side Pydantic
# Literal in allostat_server/pillar_protocol.py::ClientStateWrite.target.
ALLOWED_TARGETS = frozenset({
    "nudge_history.jsonl",
    "pending_proposals.jsonl",
    "pillar_history.jsonl",
    "silo_entry.jsonl",
    "epigenetic_signature.json",
    "prediction_calibration.json",
    "chronic_stress_baseline.json",
    "drift_seen.json",
    # v0.2.6 PATCH-128 A4 — dusk-phase content for next session's dawn
    # surface (Stop event silently drops additionalContext per hook docs).
    "pending_dusk_surface.jsonl",
})

# For each target, which write modes are valid. JSONL = append-only;
# JSON = overwrite or merge.
TARGET_MODES: dict[str, frozenset[str]] = {
    "nudge_history.jsonl": frozenset({"append"}),
    "pending_proposals.jsonl": frozenset({"append"}),
    "pillar_history.jsonl": frozenset({"append"}),
    # "purge" (2026-08-07): the retraction path. The server can INSTRUCT a
    # purge but never performs one — silo bytes are operator-local by the
    # privacy moat, so the client removes them and the client verifies. It is
    # the only non-append mode on a JSONL target, and it exists because
    # "forget X" that leaves the silo intact is a false claim.
    "silo_entry.jsonl": frozenset({"append", "purge"}),
    "epigenetic_signature.json": frozenset({"overwrite", "merge"}),
    "prediction_calibration.json": frozenset({"overwrite", "merge"}),
    "chronic_stress_baseline.json": frozenset({"overwrite", "merge"}),
    "drift_seen.json": frozenset({"overwrite", "merge"}),
    "pending_dusk_surface.jsonl": frozenset({"append"}),
}


# Hard cap on per-call writes to defend against runaway pillars or a
# malicious server flooding the operator's filesystem. Server is rate-
# limited upstream; this is defense-in-depth.
MAX_WRITES_PER_CALL = 50

# Hard cap on per-write content size (bytes when JSON-serialized).
MAX_CONTENT_BYTES = 256 * 1024  # 256 KB


def apply_writes(
    state_dir: Path,
    writes: Iterable[dict[str, Any]],
    *,
    audit_log_path: Path | None = None,
) -> tuple[int, int]:
    """Apply server-instructed writes to the operator's local state.

    `writes` is the `client_state_writes` list from the server's
    DispatchResponse. Each item is a dict with keys: target, mode, content,
    pillar_id.

    Returns (applied_count, rejected_count). Every write — accepted or
    rejected — is recorded to the audit log. Failures during application
    (filesystem error) are also recorded as rejections so the operator
    sees the whole story.
    """
    if audit_log_path is None:
        audit_log_path = state_dir / "server_instructed_writes.jsonl"

    applied = 0
    rejected = 0
    write_list = list(writes)

    # Per-call cap defense.
    if len(write_list) > MAX_WRITES_PER_CALL:
        _audit_log(
            audit_log_path,
            {
                "action": "batch_capped",
                "received": len(write_list),
                "max_allowed": MAX_WRITES_PER_CALL,
            },
        )
        write_list = write_list[:MAX_WRITES_PER_CALL]

    for write in write_list:
        ok, reason = _apply_one(state_dir, write, audit_log_path)
        if ok:
            applied += 1
        else:
            rejected += 1
            _audit_log(
                audit_log_path,
                {
                    "action": "rejected",
                    "rejection_reason": reason,
                    "target": (write or {}).get("target") if isinstance(write, dict) else None,
                    "mode": (write or {}).get("mode") if isinstance(write, dict) else None,
                    "pillar_id": (write or {}).get("pillar_id") if isinstance(write, dict) else None,
                },
            )
    return applied, rejected


def _apply_one(
    state_dir: Path,
    write: dict[str, Any],
    audit_log_path: Path,
) -> tuple[bool, str | None]:
    """Validate and apply one write. Returns (success, rejection_reason)."""
    if not isinstance(write, dict):
        return False, "write_not_dict"

    target = write.get("target")
    mode = write.get("mode")
    content = write.get("content")
    pillar_id = write.get("pillar_id")

    if target not in ALLOWED_TARGETS:
        return False, f"target_not_whitelisted:{target!r}"
    allowed_modes = TARGET_MODES.get(target, frozenset())
    if mode not in allowed_modes:
        return False, f"mode_not_valid_for_target:{mode!r}_not_in_{sorted(allowed_modes)!r}"
    if not isinstance(content, dict):
        return False, f"content_not_dict:{type(content).__name__}"

    # Defense: serialize content to validate JSON + size before writing.
    try:
        content_bytes = json.dumps(content, default=str).encode("utf-8")
    except (TypeError, ValueError) as e:
        return False, f"content_not_json_serializable:{type(e).__name__}"
    if len(content_bytes) > MAX_CONTENT_BYTES:
        return False, f"content_too_large:{len(content_bytes)}_bytes"

    # Resolve target path. silo_entry.jsonl routes to silos/<class>.jsonl.
    target_path = _resolve_target_path(state_dir, target, content)
    if target_path is None:
        return False, "silo_entry_missing_silo_class"

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            _append_jsonl(target_path, content)
        elif mode == "overwrite":
            _write_json(target_path, content)
        elif mode == "merge":
            _merge_json(target_path, content)
        elif mode == "purge":
            ok, reason = _purge_silo(target_path, content)
            if not ok:
                return False, reason
        else:
            # Defensive: should be unreachable given the allowed_modes check.
            return False, f"mode_handler_missing:{mode!r}"
    except OSError as e:
        return False, f"filesystem_error:{e.__class__.__name__}:{e}"

    _audit_log(
        audit_log_path,
        {
            "action": "applied",
            "target": target,
            "mode": mode,
            "pillar_id": pillar_id,
            "resolved_path": str(target_path.relative_to(state_dir)) if state_dir in target_path.parents or state_dir == target_path.parent else str(target_path),
            "content_bytes": len(content_bytes),
        },
    )
    return True, None


def _resolve_target_path(
    state_dir: Path, target: str, content: dict[str, Any]
) -> Path | None:
    """Resolve the on-disk path for a write target.

    Most targets land directly inside state_dir. The special-case
    `silo_entry.jsonl` routes to `<state_dir>/silos/<silo_class>.jsonl`
    based on `content.silo_class`. Returns None when silo_class is missing
    or invalid (caller treats as rejection).
    """
    if target == "silo_entry.jsonl":
        silo_class = content.get("silo_class")
        if not isinstance(silo_class, str) or not silo_class:
            return None
        # Defensive: silo_class must be a simple identifier to prevent any
        # path traversal vector. Whitelist alphanumeric + underscore + hyphen.
        if not all(c.isalnum() or c in ("_", "-") for c in silo_class):
            return None
        return state_dir / "silos" / f"{silo_class}.jsonl"
    return state_dir / target


def _strip_canonical_from_silo_content(content: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 storage-level strip + canonical_resolutions bind (apply path).

    Two-step (matches silo_base.write_entry):
      1. Extract non-whitelist fields, bind to canonical_resolutions.
      2. Strip to wire-shape; return for disk-write.

    This is the apply-side defense for server-dispatched ClientStateWrite
    targets routed to `<state_dir>/silos/<class>.jsonl`. Companion to:
      - silo_base.write_entry (wrapper-direct inscribe path)
      - silo_excerpt._strip_canonical_for_wire (read-time strip)
      - canonical_resolutions_migration (one-time pre-Phase-2 cleanup)

    Whitelist semantics post-audit (2026-05-28): only `canonical_source`
    survives in resolution; only `class_name` + `key_tokens` survive in
    fingerprint. Future silo classes inherit the privacy invariant.

    Note: state_dir for canonical_resolutions bind is the silo dir's parent.
    For the server-dispatch path we don't have direct state_dir reference;
    the caller (apply_writes) handed us a path already resolved relative
    to state_dir. We reconstruct: <state_dir>/silos/<class>.jsonl →
    state_dir = path.parent.parent.
    """
    try:
        from silo_base import strip_entry_for_wire
    except ImportError:
        # If silo_base can't load, drop the strip rather than crashing —
        # entry will be stripped at read-time via silo_excerpt.
        return content

    return strip_entry_for_wire(content)


def _bind_canonical_from_silo_content(
    state_dir: Path,
    content: dict[str, Any],
) -> None:
    """Bind any non-whitelist canonical fields from a silo write to local store.

    Best-effort. Called by `_append_jsonl` before the strip + write. The
    server-dispatched silo entry shouldn't carry canonical text after the
    Phase 2 server fix — but this is defense-in-depth if a future server
    regresses or a non-Phase-2 server is talking to the wrapper.
    """
    try:
        from silo_base import SiloFingerprint, extract_canonical_payload
        import canonical_resolutions

        payload, raw_excerpt = extract_canonical_payload(content)
        if not payload and not raw_excerpt:
            return

        fp = content.get("fingerprint")
        fingerprint_hash = ""
        class_name = ""
        if isinstance(fp, dict):
            class_name = str(fp.get("class_name") or "")
            try:
                sf = SiloFingerprint(
                    class_name=class_name,
                    key_tokens=[str(t) for t in fp.get("key_tokens") or []],
                )
                fingerprint_hash = sf.to_hash()
            except Exception:
                fingerprint_hash = ""

        canonical_resolutions.inscribe_payload(
            state_dir,
            fingerprint_hash=fingerprint_hash,
            payload=payload,
            silo_class=class_name,
            session_id=str(content.get("session_id") or ""),
            raw_excerpt=raw_excerpt,
            now_iso=str(content.get("timestamp") or "") or None,
        )
    except Exception:
        # Best-effort bind — privacy invariant doesn't depend on this side
        # write. If it fails, recall-display loses the wrapper-local
        # mapping but wire shape stays clean.
        pass


def _append_jsonl(path: Path, content: dict[str, Any]) -> None:
    # Phase 2 (2026-05-28) — defense in depth. Silo entries on disk carry
    # whitelist fields only (resolution: canonical_source; fingerprint:
    # class_name + key_tokens). Non-whitelist canonical fields bind to
    # wrapper-local canonical_resolutions.jsonl before the strip + write.
    is_silo = path.parent.name == "silos" and path.suffix == ".jsonl"
    if is_silo:
        # State_dir for canonical_resolutions bind: silos/ parent.
        _bind_canonical_from_silo_content(path.parent.parent, content)
        content = _strip_canonical_from_silo_content(content)
    line = json.dumps(content, default=str)
    # M-04: append under the shared JSONL lock so a concurrent rotation can't
    # clobber this write. Held only for the append, released before rotate
    # (which re-takes the same lock) — never nested (self-deadlock). Best-
    # effort: if local_state can't load in a degraded install, fall back to a
    # plain append.
    try:
        from local_state import jsonl_lock
        _lock_cm = jsonl_lock(path)
    except ImportError:
        _lock_cm = contextlib.nullcontext()
    with _lock_cm:
        # Dedup fix (2026-08-07): the server-dispatched inscribe folds onto an
        # existing case exactly like the wrapper-direct one
        # (silo_base.write_entry) — one case, one row, a rising fire_count.
        # Two write paths into the same file must agree about what a row means.
        # Best-effort: a degraded install without silo_base still appends.
        coalesced = False
        if is_silo:
            try:
                from silo_base import coalesce_or_append
                coalesce_or_append(path, content)
                coalesced = True
            except ImportError:
                coalesced = False
        if not coalesced:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    # PATCH-144 v1.2.0 — rotate when file crosses threshold. Audit D1 fix.
    # Best-effort; silent on filesystem error.
    try:
        from local_state import (
            NUDGE_HISTORY_KEEP_RECENT_ENTRIES,
            NUDGE_HISTORY_ROTATE_THRESHOLD_BYTES,
            OBSERVATIONS_KEEP_RECENT_ENTRIES,
            OBSERVATIONS_ROTATE_THRESHOLD_BYTES,
            rotate_jsonl_if_oversize,
        )
        # Nudge history rotates at lower threshold (smaller per-entry, more
        # frequent fires); everything else uses the observation-class threshold.
        if path.name == "nudge_history.jsonl":
            threshold = NUDGE_HISTORY_ROTATE_THRESHOLD_BYTES
            keep = NUDGE_HISTORY_KEEP_RECENT_ENTRIES
        else:
            threshold = OBSERVATIONS_ROTATE_THRESHOLD_BYTES
            keep = OBSERVATIONS_KEEP_RECENT_ENTRIES
        rotate_jsonl_if_oversize(path, threshold, keep)
    except (OSError, ImportError):
        # M14 (fixpass 2026-07-01): environmental failures stay silent per
        # the stated best-effort design (filesystem error, or local_state
        # absent in a degraded install). Logic errors now PROPAGATE — a bug
        # in rotation must surface to the hook crash armor, not vanish.
        pass


def _purge_silo(path: Path, content: dict[str, Any]) -> tuple[bool, str | None]:
    """Remove every row of a silo matching `content["pattern"]`, then VERIFY.

    The client does the removing because the client is where the bytes are —
    the server has never held silo content and must not start in order to
    delete it. It also does the verifying: a purge that reports success without
    re-reading the file is the same false claim, one layer down.

    Returns (False, reason) if anything still matches afterwards, so the write
    is audited as rejected rather than logged as applied.
    """
    pattern = content.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return False, "purge_missing_pattern"
    regex_mode = content.get("regex", False)
    if not isinstance(regex_mode, bool):
        return False, "purge_invalid_regex_mode"
    if not path.is_file():
        return True, None  # nothing to purge is not a failure

    import re as _re

    try:
        rx = _re.compile(pattern if regex_mode else _re.escape(pattern), _re.IGNORECASE)
    except _re.error:
        return False, "purge_invalid_regex"
    try:
        from local_state import jsonl_lock
        lock_cm = jsonl_lock(path)
    except ImportError:
        lock_cm = contextlib.nullcontext()
    with lock_cm:
        text = path.read_text(encoding="utf-8")
        if regex_mode and any(
            match.start() == match.end() for match in rx.finditer(text)
        ):
            return False, "purge_zero_width_regex"
        lines = [ln for ln in text.splitlines() if ln.strip()]
        kept = [ln for ln in lines if not rx.search(ln)]
        if len(kept) != len(lines):
            from local_state import atomic_write_text
            atomic_write_text(
                path,
                "\n".join(kept) + ("\n" if kept else ""),
                encoding="utf-8",
            )
        survivors = sum(
            1 for ln in path.read_text(encoding="utf-8").splitlines() if rx.search(ln)
        )
    if survivors:
        return False, f"purge_incomplete:{survivors}_rows_remain"
    return True, None


def _write_json(path: Path, content: dict[str, Any]) -> None:
    # Atomic write (deep-audit 2026-07-02): temp+rename so a crash mid-write
    # can't leave a truncated/corrupt shared state file (epigenetic_signature,
    # prediction_calibration, chronic_stress_baseline, drift_seen, ...). Also
    # covers _merge_json, which serializes through here. (A concurrent-writer
    # lost-update race across sessions remains — tracked for a locking pass.)
    from local_state import atomic_write_text  # noqa: E402
    atomic_write_text(path, json.dumps(content, indent=2, default=str))


def _merge_json(path: Path, content: dict[str, Any]) -> None:
    """Shallow-merge content into the existing JSON object at path.

    Existing file is overwritten with the merged result. If the file
    doesn't exist or contains malformed JSON, treat existing as empty
    dict.

    M-04: the read-modify-write is held under the shared jsonl_lock so two
    concurrent merges of different keys can't each read the same prior object
    and clobber one another's update (lost update). Best-effort: if local_state
    can't load in a degraded install, fall back to an unlocked RMW rather than
    breaking the write. _write_json (temp+rename) is not itself locked and does
    not re-take the lock, so no self-deadlock.
    """
    try:
        from local_state import jsonl_lock
        _lock_cm = jsonl_lock(path)
    except ImportError:
        _lock_cm = contextlib.nullcontext()
    with _lock_cm:
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = data
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(content)
        _write_json(path, existing)


def _audit_log(audit_path: Path, entry: dict[str, Any]) -> None:
    """Append one audit entry. Best-effort; failures swallowed so the
    audit log can't break the wrapper's session.

    M-04 (audit 2026-07-21): the append takes the shared JSONL lock, like
    `_append_jsonl` above. It did not, so two sessions writing
    `server_instructed_writes.jsonl` in one project dropped each other's
    records — and this is the forensic log of what the SERVER told the client to
    write. A dropped line here is a server-instructed write that happened and
    left no trace, which is the one thing this file exists to make impossible.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **entry,
    }
    try:
        from local_state import jsonl_lock

        lock_cm = jsonl_lock(audit_path)
    except ImportError:
        lock_cm = contextlib.nullcontext()
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_cm:
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass
