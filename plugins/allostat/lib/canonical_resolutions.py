"""Wrapper-local canonical-text store for silo entries.

Phase 2 (2026-05-28) silo-leak fix. Pairs with `lib/silo_base.py`:
- Silo entries on disk (`<state_dir>/silos/<class>.jsonl`) carry the
  fingerprint hash + provenance + timestamps. They never carry the
  operator's canonical text.
- Canonical text lives ONLY here, in `<state_dir>/silos/canonical_resolutions.jsonl`.
  One file global across all silo classes. Wrapper-local, never crosses
  the wire to the server.

Why the split:
  Pre-Phase-2, every silo entry stored the operator's full correction prompt
  in `resolution.canonical_value`. Those entries got shipped back to the
  server as `excerpt.retrievals` on every subsequent dispatch — operator's
  corrected truth crossed the wire repeatedly even after first inscribe.
  Phase 2 makes silo entries wire-shape by construction; canonical text
  is wrapper-local from the moment of write.

Operator-facing recall UI joins `<class>.jsonl` entries with this file by
fingerprint_hash to display human-readable canonical text. Server never
reads this file.

Schema (Phase 2 refined 2026-05-28 post-audit):
  {
    "fingerprint_hash": str,    # sha256 truncated-16, mirrors silo_base
    "payload": dict,            # arbitrary canonical fields; PER-SILO-CLASS
    "raw_excerpt": str,         # human-readable surface (was fingerprint.raw_excerpt)
    "silo_class": str,          # drift / voice / customization / workflow / confidence_recovery
    "session_id": str,
    "created_at": str,          # ISO-8601 UTC
  }

Payload shape varies by silo class — anything the silo composer put in
`resolution` that isn't `canonical_source` (a known-safe reference string)
gets relocated here. Examples:
  drift:       {"canonical_value": "<full operator prompt>"}
  voice:       {"canonical_sample": "...", "canonical_source": "..."}
  workflow:    {"canonical_fix": "...", "lesson": "..."}
  customization: {"chosen_option": "...", "rationale": "..."}
  confidence_recovery: {"recovery_action": "...", "ending_confidence": "..."}

Backward-compat: `inscribe(canonical_value="...")` is a thin shim that
wraps `inscribe_payload(payload={"canonical_value": "..."})`. The original
`canonical_value` key is also exposed at top level for legacy readers
(`lookup_canonical_value` returns it).

JSONL append-only. Multiple entries for the same fingerprint_hash are
allowed (e.g., refinements over time). `lookup_by_hash` returns the most
recent.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def resolve_canonical_path(state_dir: Path) -> Path:
    """Canonical-resolutions file path within an operator state dir."""
    return state_dir / "silos" / "canonical_resolutions.jsonl"


def inscribe_payload(
    state_dir: Path,
    *,
    fingerprint_hash: str,
    payload: dict[str, Any],
    silo_class: str,
    session_id: str = "",
    raw_excerpt: str = "",
    now_iso: str | None = None,
) -> bool:
    """Append a fingerprint_hash -> arbitrary-payload binding.

    The whitelist-flip API (Phase 2 post-audit 2026-05-28). Used by:
      - silo_base.write_entry (wrapper-local inscribe path)
      - client_state_writer._append_jsonl (server-dispatch inscribe path)
      - canonical_resolutions_migration (one-time relocation)

    `payload` is whatever the silo composer originally put in the entry's
    `resolution` dict, MINUS `canonical_source` (which stays in the wire
    entry as a known-safe reference string). Caller is responsible for
    that distinction.

    Returns True on success/no-op, False on filesystem error.

    Skipped (returns True without writing) when:
      - fingerprint_hash is empty
      - payload is empty AND raw_excerpt is empty (nothing to bind)
      - silo_class is empty
    """
    if not fingerprint_hash or not silo_class:
        return True
    if not payload and not raw_excerpt:
        return True

    record = {
        "fingerprint_hash": fingerprint_hash,
        # Additive version tag (deep-audit 2026-07-02, advisor rec): the hash
        # algorithm has a known narrow collision surface (unescaped ':' join,
        # parity-locked with the plugin). v1 = current algorithm. If the join
        # is ever fixed, v2 records are distinguishable + migratable instead
        # of guess-and-rehash. Purely additive — nothing reads it yet.
        "fingerprint_version": 1,
        "payload": payload or {},
        "raw_excerpt": raw_excerpt,
        "silo_class": silo_class,
        "session_id": session_id or "",
        "created_at": now_iso or _utc_iso_now(),
    }

    # Backward-compat top-level alias: many callers + tests expect
    # `canonical_value` at top level. Synthesize it when payload has a
    # `canonical_value` key. Saves churn on legacy lookups.
    cv = payload.get("canonical_value") if isinstance(payload, dict) else None
    if isinstance(cv, str) and cv:
        record["canonical_value"] = cv

    # M-04 (audit 2026-07-21): append under the shared JSONL lock. This file is
    # the ONLY place the operator's canonical text lives after the Phase-2
    # privacy strip, and the append was unlocked — so two sessions in one
    # project silently lost bindings to each other (measured: 1590 of 1800
    # records survived 6 concurrent writers; 1800 of 1800 through the locked
    # path). A lost binding is not a lost log line: recall then shows a
    # fingerprint with no resolvable text, and the text exists nowhere else.
    #
    # Best-effort, matching client_state_writer._append_jsonl: a degraded
    # install where local_state cannot be imported still gets its write.
    path = resolve_canonical_path(state_dir)
    try:
        from local_state import jsonl_lock

        lock_cm = jsonl_lock(path)
    except ImportError:
        lock_cm = contextlib.nullcontext()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_cm:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def inscribe(
    state_dir: Path,
    *,
    fingerprint_hash: str,
    canonical_value: str,
    silo_class: str,
    session_id: str = "",
    raw_excerpt: str = "",
    now_iso: str | None = None,
) -> bool:
    """Legacy single-field API — wraps inscribe_payload with canonical_value.

    Kept for backward compat. New callers should use inscribe_payload directly
    with whatever silo-class-appropriate payload dict they have.
    """
    payload = {"canonical_value": canonical_value} if canonical_value else {}
    return inscribe_payload(
        state_dir,
        fingerprint_hash=fingerprint_hash,
        payload=payload,
        silo_class=silo_class,
        session_id=session_id,
        raw_excerpt=raw_excerpt,
        now_iso=now_iso,
    )


def lookup_by_hash(
    state_dir: Path,
    fingerprint_hash: str,
) -> dict[str, Any] | None:
    """Return the most-recent canonical binding for a fingerprint hash.

    Returns None if no binding exists (and on any read error — silos must
    tolerate corruption without breaking recall). Iterates the file once;
    keeps the latest match wins by file order (JSONL append-only is time-
    ordered).
    """
    if not fingerprint_hash:
        return None

    path = resolve_canonical_path(state_dir)
    if not path.exists():
        return None

    latest: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("fingerprint_hash") == fingerprint_hash:
                    latest = record
    except OSError:
        return None
    return latest


def lookup_canonical_value(state_dir: Path, fingerprint_hash: str) -> str:
    """Convenience: return canonical_value string or '' if not bound."""
    record = lookup_by_hash(state_dir, fingerprint_hash)
    if not record:
        return ""
    value = record.get("canonical_value", "")
    return str(value) if value else ""


def read_all(state_dir: Path) -> list[dict[str, Any]]:
    """Return all canonical bindings (for migration + diagnostics).

    Skips malformed lines silently.
    """
    path = resolve_canonical_path(state_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return out
    return out


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
