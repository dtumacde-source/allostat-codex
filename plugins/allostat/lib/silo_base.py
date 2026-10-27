"""Allostat — generic silo storage for allostatic memory cells.

Ported from the v2.3 plugin's lib/silo_base.py into the v2.4 wrapper.
This is the foundation module for PATCH-167 silo_inscribe port; per-class
silos (drift, voice, customization, workflow, confidence_recovery) build
on top of it.

Architecture context (v2.40 hosted-MCP):
  - Server-side recall_silos pillar already handles query_silo +
    compute_fingerprint_hash + compute_class_tokens question_types.
  - Wrapper-side INSCRIBE PATH (this module + per-class modules) was the
    v2.3→v2.40 port gap surfaced by audit 2026-05-22.
  - Silo files live at `<state_dir>/silos/<class>.jsonl` — operator-local
    per privacy moat. Wrapper reads them, passes entries as
    excerpt.retrievals to server's query_silo invocations.

Generalizes the immune_memory pattern across signal classes. Each signal
class (drift, voice, customization, confidence_recovery, workflow) gets
its own JSONL silo at `.allostat/silos/<class>.jsonl`.

Real-biology analog: each lymphocyte lineage (B-cell, T-cell, memory cell)
specializes for a class of antigen. Same shape — each silo stores resolved
cases for one signal class. Two consumers: the pillar that owns the class
(writes resolutions, queries for predictive pre-shift) and Claude (reads as
skill on relevant context).

Lifecycle:
- write_entry — append a resolved case
- read_entries — iterate all entries (skips corrupt lines)
- query_silo — fingerprint-hash match, ranked by fire_count + recency, skips decayed
- decay_quiescent — flag entries quiescent N days as decayed (default 90)
- archive_long_quiescent — move long-quiescent decayed entries to archive (default 180)

Schema versioning: SCHEMA_VERSION = 1 (no migrations needed yet). If
SiloEntry shape changes, bump SCHEMA_VERSION and add a migration in
from_jsonl.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1


@dataclass
class SiloFingerprint:
    """Identifies a case within a signal class.

    Hashing is over (class_name, sorted(key_tokens)) so token order doesn't
    affect identity. raw_excerpt is for human-readable recall surface, not
    identity.
    """

    class_name: str
    key_tokens: list[str]
    raw_excerpt: str = ""

    def to_hash(self) -> str:
        """sha256(class_name::sorted(key_tokens) joined by :) truncated to 16 chars.

        Mirrors server-side recall_silos.compute_fingerprint_hash exactly —
        drift between server + wrapper must stay zero or recall breaks.
        """
        seed = f"{self.class_name}::{':'.join(sorted(self.key_tokens))}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SiloFingerprint":
        return cls(
            class_name=d["class_name"],
            key_tokens=list(d.get("key_tokens", [])),
            raw_excerpt=d.get("raw_excerpt", ""),
        )


@dataclass
class SiloEntry:
    """A stored resolved case — the silo's atomic unit."""

    fingerprint: SiloFingerprint
    resolution: dict
    success_marker: str
    timestamp: str  # ISO-8601
    session_id: str
    fire_count: int = 1
    last_fire: str = ""
    decayed: bool = False

    def to_jsonl(self) -> str:
        d = {
            "fingerprint": self.fingerprint.to_dict(),
            "resolution": self.resolution,
            "success_marker": self.success_marker,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "fire_count": self.fire_count,
            "last_fire": self.last_fire or self.timestamp,
            "decayed": self.decayed,
        }
        return json.dumps(d, default=str)

    @classmethod
    def from_jsonl(cls, line: str) -> "SiloEntry":
        d = json.loads(line)
        return cls(
            fingerprint=SiloFingerprint.from_dict(d["fingerprint"]),
            resolution=d["resolution"],
            success_marker=d["success_marker"],
            timestamp=d["timestamp"],
            session_id=d["session_id"],
            fire_count=int(d.get("fire_count", 1)),
            last_fire=d.get("last_fire", d["timestamp"]),
            decayed=bool(d.get("decayed", False)),
        )


# PATCH-183.3 (2026-05-23): SiloStore class deleted; no instantiation
# sites in production code (debloat audit Category A1).


# ============================================================================
# Phase 2 wire-shape whitelist (2026-05-28, post-audit refinement)
# ============================================================================
#
# Silo entries on the wire (wrapper -> server retrievals) carry these fields
# only. Anything outside this whitelist is operator canonical text and must
# never cross the wire — it lives wrapper-local in
# `<state_dir>/silos/canonical_resolutions.jsonl`.
#
# ultraswarm H-7/H-8 (2026-07-07): `key_tokens` was wire-whitelisted, but the
# tokens are NOT hashes — they carry raw operator/assistant content (the first
# ~80 chars of a correction claim, a 60-char assistant snippet from
# voice_keeper, raw filenames). That defeats invariant #1 ("only fingerprint
# hashes + provenance cross the wire"). Fix: the wire fingerprint now carries a
# `token_hash` (sha256 of class + sorted(tokens), the same identity hash the
# server matched on anyway) INSTEAD of the raw tokens. key_tokens stay
# wrapper-local on disk (needed for local query_silo matching) — no migration.
#
# The whitelist is intentional: future silo classes inherit the privacy
# invariant by construction. Only `canonical_source` (a known-safe reference
# string like `"operator_correction:s-id"`) stays in the wire-bound resolution.

RESOLUTION_WIRE_KEYS = frozenset({"canonical_source"})
# On disk (defense-in-depth strip) the fingerprint keeps class_name + key_tokens
# — key_tokens are the LOCAL identity surface (silo_base.query_silo, canonical
# binding, migration all read them). On the WIRE (retrievals -> server) the raw
# key_tokens are replaced by token_hash (see hash_tokens=True below) so operator
# content never crosses — ultraswarm H-7/H-8.
FINGERPRINT_WIRE_KEYS = frozenset({"class_name", "key_tokens"})

# Top-level entry fields with string-typed values (H-01 whitelist). Everything
# not listed here (or handled explicitly below) is dropped from the output.
ENTRY_WIRE_STR_FIELDS = (
    "silo_class",
    "success_marker",
    "timestamp",
    "session_id",
    "last_fire",
)


def strip_entry_for_wire(entry_dict: dict, *, hash_tokens: bool = False) -> dict:
    """Return a wire/disk-safe entry CONSTRUCTED from whitelisted fields only.

    H-01 (audit 2026-07-08, fixed Wave-2 2026-07-11): this used to start from
    `out = dict(entry_dict)` and redact in place — any unexpected TOP-LEVEL
    field, or a non-dict `resolution`/`fingerprint` (legacy or hand-edited
    silo lines), passed through carrying the operator's raw canonical text.
    Inverted to a strict whitelist: the output is built field by field from
    known-safe shapes; anything unexpected is dropped or replaced with a
    typed placeholder ({} / "" / schema default) — never passed through.
    (write_entry's canonical binding relocates a non-dict resolution's text
    wrapper-locally via extract_canonical_payload before this strip runs, so
    nothing is silently destroyed on the disk path.)

    Used at TWO kinds of sink:
      - disk storage (client_state_writer._append_jsonl, silo_base.write_entry):
        `hash_tokens=False` (default) — key_tokens stay on disk for local
        matching / canonical binding / migration.
      - the wrapper->server wire (silo_excerpt.read_silo_entries_for_excerpt):
        `hash_tokens=True` — key_tokens (which carry raw operator/assistant
        content) are replaced by their identity `token_hash`, so no raw content
        crosses the wire (ultraswarm H-7/H-8, 2026-07-07). The hash is
        SiloFingerprint.to_hash, byte-identical to the server's
        compute_fingerprint_hash, so recall still matches.
    """
    out: dict = {}
    for key in ENTRY_WIRE_STR_FIELDS:
        if key in entry_dict:
            value = entry_dict[key]
            out[key] = value if isinstance(value, str) else ""
    if "fire_count" in entry_dict:
        value = entry_dict["fire_count"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            out["fire_count"] = 1  # schema default — never the raw value
        else:
            out["fire_count"] = int(value)
    if "decayed" in entry_dict:
        out["decayed"] = bool(entry_dict["decayed"])
    if "resolution" in entry_dict:
        resolution = entry_dict["resolution"]
        if isinstance(resolution, dict):
            out["resolution"] = {
                k: v
                for k, v in resolution.items()
                if k in RESOLUTION_WIRE_KEYS and isinstance(v, str)
            }
        else:
            # Non-dict resolution IS raw canonical text — typed placeholder.
            out["resolution"] = {}
    if "fingerprint" in entry_dict:
        fingerprint = entry_dict["fingerprint"]
        if not isinstance(fingerprint, dict):
            # Non-dict fingerprint (hand-edited/corrupt) — typed placeholder.
            out["fingerprint"] = {}
        else:
            raw_class = fingerprint.get("class_name")
            class_name = raw_class if isinstance(raw_class, str) else ""
            raw_tokens = fingerprint.get("key_tokens")
            # A non-list key_tokens (e.g. a raw string) must never be kept or
            # iterated per-character onto the wire — sanitize to [].
            key_tokens = (
                [str(t) for t in raw_tokens] if isinstance(raw_tokens, list) else []
            )
            if hash_tokens:
                token_hash = SiloFingerprint(class_name, key_tokens).to_hash()
                out["fingerprint"] = {
                    "class_name": class_name,
                    "token_hash": token_hash,
                }
            else:
                fp_out: dict = {}
                if "class_name" in fingerprint:
                    fp_out["class_name"] = class_name
                if "key_tokens" in fingerprint:
                    fp_out["key_tokens"] = key_tokens
                out["fingerprint"] = fp_out
    return out


def extract_canonical_payload(entry_dict: dict) -> tuple[dict, str]:
    """Return (resolution_payload, raw_excerpt) for canonical_resolutions binding.

    `resolution_payload` is whatever's in `resolution` MINUS canonical_source
    (which stays in the wire-bound entry). `raw_excerpt` is the fingerprint's
    raw_excerpt field (which never crosses the wire post-Phase-2).

    H-01 (Wave-2 2026-07-11): a NON-DICT resolution (legacy/hand-edited entry)
    is raw canonical text. The strict whitelist in strip_entry_for_wire
    replaces it with {}, so it's captured here as `canonical_value` (and a
    non-dict string fingerprint as raw_excerpt) so the write-time binding
    relocates the text wrapper-locally instead of destroying it.

    Returns ({}, "") if nothing to relocate.
    """
    payload: dict = {}
    raw_excerpt = ""
    resolution = entry_dict.get("resolution")
    if isinstance(resolution, dict):
        payload = {
            k: v for k, v in resolution.items() if k not in RESOLUTION_WIRE_KEYS
        }
    elif isinstance(resolution, str) and resolution:
        payload = {"canonical_value": resolution}
    elif resolution not in (None, "", {}, []):
        payload = {"canonical_value": json.dumps(resolution, default=str)}
    fingerprint = entry_dict.get("fingerprint")
    if isinstance(fingerprint, dict):
        rx = fingerprint.get("raw_excerpt")
        if isinstance(rx, str) and rx:
            raw_excerpt = rx
    elif isinstance(fingerprint, str) and fingerprint:
        raw_excerpt = fingerprint
    return payload, raw_excerpt


def entry_identity(entry_dict: dict) -> tuple[str, str] | None:
    """(fingerprint hash, success_marker) for a disk-shape silo line.

    Returns None when the line can't be identified — a wire-shape line
    carrying `token_hash` instead of `key_tokens`, a non-dict fingerprint, a
    hand-edited record. Unidentifiable lines never coalesce; they are left
    exactly as found.
    """
    fingerprint = entry_dict.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return None
    raw_tokens = fingerprint.get("key_tokens")
    if not isinstance(raw_tokens, list):
        return None
    class_name = fingerprint.get("class_name")
    fp = SiloFingerprint(
        class_name=class_name if isinstance(class_name, str) else "",
        key_tokens=[str(t) for t in raw_tokens],
    )
    marker = entry_dict.get("success_marker")
    return fp.to_hash(), marker if isinstance(marker, str) else ""


def coalesce_or_append(silo_path: Path, entry_dict: dict) -> bool:
    """Fold a new entry onto an identical existing case, or append it.

    A silo row is a CASE, and `fire_count` is how many times that case has
    fired. Both silo write paths used to append unconditionally, so a case
    seen 18 times became 18 rows each claiming `fire_count: 1` — the bench
    VM's voice silo, exactly (2026-08-07). That is not a cosmetic duplicate
    problem: `query_silo` ranks by `fire_count`, so the count that decides
    what gets recalled was pinned at 1 forever, and every consumer reading
    row counts as case counts was reading an inflated number.

    Matching is on (fingerprint hash, success_marker) among non-decayed
    rows, most recent first:
      - same hash, same marker  → same case, same claim → bump.
      - same hash, DIFFERENT marker → different claims about the same case
        (a `voice_detected` row and a later `voice_corrected` row are two
        distinct facts) → separate rows.
      - decayed rows are closed history → never bumped; a fresh fire on a
        decayed case starts a live row again.

    On a bump the existing row is preserved field-for-field apart from
    `fire_count` (+= incoming) and `last_fire` (:= incoming). The first
    sighting stays the record of record; only the counter moves. Every other
    line is rewritten BYTE-IDENTICAL, including lines that fail to parse, so
    coalescing can never quietly reshape or drop a neighbouring record.

    The caller holds the file lock; this does read-modify-write inside it.

    Returns True if an existing row absorbed the entry, False if appended.
    """
    identity = entry_identity(entry_dict)
    lines: list[str] = []
    if identity is not None and silo_path.exists():
        with silo_path.open("r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        for i in range(len(lines) - 1, -1, -1):
            raw = lines[i].strip()
            if not raw:
                continue
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(existing, dict) or existing.get("decayed"):
                continue
            if entry_identity(existing) != identity:
                continue

            prior = existing.get("fire_count")
            if isinstance(prior, bool) or not isinstance(prior, (int, float)):
                prior = 1
            incoming = entry_dict.get("fire_count")
            if isinstance(incoming, bool) or not isinstance(incoming, (int, float)):
                incoming = 1
            existing["fire_count"] = int(prior) + int(incoming)
            new_fire = entry_dict.get("last_fire") or entry_dict.get("timestamp")
            if isinstance(new_fire, str) and new_fire:
                existing["last_fire"] = new_fire
            lines[i] = json.dumps(existing, default=str)

            tmp = silo_path.with_suffix(silo_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
            tmp.replace(silo_path)
            return True

    with silo_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry_dict, default=str) + "\n")
    return False


def write_entry(silo_path: Path, entry: SiloEntry) -> None:
    """Append a resolved case to the silo. Creates parent dir if missing.

    Phase 2 (2026-05-28, post-audit) — the storage egress point for silo
    entries. Closes the wrapper-direct inscribe bypass (voice_keeper_local
    → record_voice_resolution → write_entry was bypassing the wire-shape
    strip applied elsewhere). Two-step:

      1. Extract any non-whitelist canonical fields from the entry. Bind
         them to fingerprint hash via canonical_resolutions (wrapper-local).
      2. Strip the entry to wire-shape. Write the stripped form to disk.

    Result: disk state matches wire state — only fingerprint hash + safe
    provenance fields. Operator canonical text lives only in
    canonical_resolutions.jsonl.
    """
    silo_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize entry once via to_jsonl (which already produces a dict-style
    # JSON line) then operate on the parsed dict for strip + payload extract.
    entry_dict = json.loads(entry.to_jsonl())

    # Bind canonical text wrapper-local (best-effort; never blocks the write).
    try:
        import canonical_resolutions

        payload, raw_excerpt = extract_canonical_payload(entry_dict)
        if payload or raw_excerpt:
            state_dir = silo_path.parent.parent  # <state_dir>/silos/<class>.jsonl
            fingerprint_hash = ""
            fp = entry_dict.get("fingerprint")
            if isinstance(fp, dict):
                try:
                    sf = SiloFingerprint(
                        class_name=str(fp.get("class_name") or ""),
                        key_tokens=[str(t) for t in fp.get("key_tokens") or []],
                    )
                    fingerprint_hash = sf.to_hash()
                except Exception:
                    fingerprint_hash = ""
            class_name = ""
            if isinstance(fp, dict):
                class_name = str(fp.get("class_name") or "")
            canonical_resolutions.inscribe_payload(
                state_dir,
                fingerprint_hash=fingerprint_hash,
                payload=payload,
                silo_class=class_name,
                session_id=str(entry_dict.get("session_id") or ""),
                raw_excerpt=raw_excerpt,
                now_iso=str(entry_dict.get("timestamp") or "") or None,
            )
    except Exception:
        # Best-effort bind. If canonical_resolutions write fails, the strip
        # below still runs — the entry just won't have a recall-display
        # binding. Operator-facing recall degrades; privacy invariant holds.
        pass

    # Strip to wire-shape + write.
    #
    # M-04 (audit 2026-07-21): the append runs under the shared JSONL lock, the
    # same one `local_state.append_observation` and
    # `client_state_writer._append_jsonl` take. It did not, and this is the
    # wrapper-DIRECT write path into the very same `silos/<class>.jsonl` that
    # the server-dispatch path locks and rotates — so it raced both a concurrent
    # append and an in-flight rotation (which reads the file and os.replace()s
    # the tail; anything appended in between is gone). On Windows the CRT
    # emulates O_APPEND as lseek-to-end-then-write, which is not atomic, so
    # concurrent unlocked appends silently overwrite each other and occasionally
    # tear a line in half. Measured with the real code path, 6 processes x 300
    # entries into one `.allostat`: 1621 of 1800 voice entries survived. Through
    # the locked path under identical load: 1800 of 1800.
    #
    # The lock is taken AFTER the canonical bind above, never around it: that
    # bind writes a DIFFERENT file (canonical_resolutions.jsonl) under its own
    # lock, and `jsonl_lock` is not re-entrant. Nothing acquires the canonical
    # lock and then this one, so there is no lock-ordering cycle.
    #
    # Best-effort, matching client_state_writer: if local_state cannot be
    # imported in a degraded install, fall back to a plain append rather than
    # losing the entry outright.
    stripped = strip_entry_for_wire(entry_dict)
    try:
        from local_state import jsonl_lock

        lock_cm = jsonl_lock(silo_path)
    except ImportError:
        lock_cm = contextlib.nullcontext()
    with lock_cm:
        # Dedup fix (2026-08-07): repeat fires of one case bump that case's
        # fire_count instead of laying down another row. Read-modify-write
        # runs inside the lock the append already held, so a concurrent
        # writer can neither miss the bump nor tear the rewrite.
        coalesce_or_append(silo_path, stripped)


def read_entries(silo_path: Path) -> Iterable[SiloEntry]:
    """Yield all entries from a silo file. Yields nothing if file missing.

    Corrupt or partial lines are skipped silently — silos must tolerate
    write interruption mid-line without losing the rest of the file.
    """
    if not silo_path.exists():
        return
    with silo_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield SiloEntry.from_jsonl(line)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue


def query_silo(
    silo_path: Path,
    query_fingerprint: SiloFingerprint,
    top_n: int = 5,
) -> list[SiloEntry]:
    """Return up to top_n entries matching the fingerprint hash.

    Ranking: fire_count desc, then last_fire desc (most-fired wins; ties
    break by recency). Decayed entries are excluded.

    Note: this is the WRAPPER-LOCAL query path. The server-side
    recall_silos pillar handles dispatcher-level query via query_silo
    question_type — same algorithm, different invocation surface. This
    function exists for wrapper-side use cases (e.g., pre-call probing
    before invoking the pillar).
    """
    target_hash = query_fingerprint.to_hash()
    matches = [
        e
        for e in read_entries(silo_path)
        if e.fingerprint.to_hash() == target_hash and not e.decayed
    ]
    matches.sort(key=lambda e: (e.fire_count, e.last_fire or e.timestamp), reverse=True)
    return matches[:top_n]


def decay_quiescent(silo_path: Path, quiescent_days: int = 90) -> int:
    """Mark entries decayed if last_fire older than quiescent_days.

    Returns count newly decayed (idempotent — re-running counts only fresh
    transitions). Already-decayed entries are not re-flagged.
    """
    if not silo_path.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=quiescent_days)
    entries = list(read_entries(silo_path))
    decayed_count = 0
    for e in entries:
        if e.decayed:
            continue
        last_fire_dt = _parse_iso(e.last_fire or e.timestamp)
        if last_fire_dt < cutoff:
            e.decayed = True
            decayed_count += 1
    if decayed_count:
        _rewrite(silo_path, entries)
    return decayed_count


def archive_long_quiescent(
    silo_path: Path,
    archive_path: Path,
    archive_days: int = 180,
) -> int:
    """Move decayed entries older than archive_days to archive_path.

    Returns count archived. Only decayed entries are eligible — undecayed
    quiescent entries are left in place (decay precedes archive in the
    lifecycle). Archive file is append-only.
    """
    if not silo_path.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=archive_days)
    entries = list(read_entries(silo_path))
    keep: list[SiloEntry] = []
    archive_batch: list[SiloEntry] = []
    for e in entries:
        if not e.decayed:
            keep.append(e)
            continue
        last_fire_dt = _parse_iso(e.last_fire or e.timestamp)
        if last_fire_dt < cutoff:
            archive_batch.append(e)
        else:
            keep.append(e)
    if archive_batch:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as f:
            for e in archive_batch:
                f.write(e.to_jsonl() + "\n")
        _rewrite(silo_path, keep)
    return len(archive_batch)


# ----------------------------------------------------------------------------
# internals
# ----------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """Parse ISO-8601, tolerating trailing Z and missing tz."""
    if not s:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _rewrite(silo_path: Path, entries: list[SiloEntry]) -> None:
    """Atomic rewrite of a silo file via tmp + replace."""
    tmp = silo_path.with_suffix(silo_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(e.to_jsonl() + "\n")
    tmp.replace(silo_path)


def resolve_silo_path(state_dir: Path, class_name: str) -> Path:
    """Conventional path for a class's silo within an operator state dir.

    `<state_dir>/silos/<class_name>.jsonl`

    Created on demand by write_entry's parent.mkdir; this helper just
    resolves the path without filesystem side effects.
    """
    return state_dir / "silos" / f"{class_name}.jsonl"
