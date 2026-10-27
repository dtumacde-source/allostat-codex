"""Local-state helpers for the operator's `.allostat/` directory.

The wrapper plugin reads from + writes to the operator's local memory
tree. The server NEVER sees these files. This module provides:

  - resolve_state_dir: find the project's .allostat/ root
  - read_recent_observations: tail of observations.jsonl (parsed)
  - append_observation: append one observation event
  - read_session_state: parse state.json
  - read_drift_seen: parse stress drift-seen dedup state
  - write_drift_seen_entry: increment fire_count / set last_seen
  - read_orphan_salience_state / write_orphan_salience_state: orphan-
    detector salience round-trip state (pre-release item 50)
  - read_silo_entries: parse a per-class silo JSONL file
  - resolve_project_root: walk up from cwd to find an allostat-aware
    project root (.allostat directory exists at root or anywhere up)

All operations are filesystem-only — no MCP, no network. Best-effort:
file errors return safe defaults (empty list, None) rather than raising.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR_NAME = ".allostat"


# M-04 (2026-07-15) — JSONL append/rotation lock.
#
# Rotation reads the whole file then os.replace()s it with the computed tail.
# Without a lock, a concurrent append landing between the read and the replace
# is clobbered (the append goes onto the pre-rotation inode/content, then the
# replace overwrites it with a tail computed *before* the append). The record
# is silently LOST.
#
# Fix: both the append path AND the rotation path take a single exclusive lock,
# keyed on a stable SIDECAR file (`<name>.jsonl.lock`). The sidecar is chosen
# deliberately: rotation os.replace()s the *data* file, so a lock held on the
# data file's own descriptor would be on the soon-to-be-orphaned inode. The
# sidecar is never renamed, so its lock identity is stable across rotation.
#
# GUARANTEE: mutual exclusion between threads and between processes ON THE SAME
# HOST (POSIX fcntl.flock / Windows msvcrt.locking). This covers the wrapper's
# real deployment shape (multiple hook invocations against one project dir on
# one machine — Windows dev, Linux prod). It is NOT safe over a network
# filesystem (NFS/SMB) where advisory locks may not be honored — out of scope
# for local `.allostat/` state. BEST-EFFORT: if the lock cannot be opened or
# acquired, the critical section still runs (unlocked) rather than breaking the
# operator's session — degrading to the pre-fix behavior only in that rare case.

if sys.platform == "win32":
    import msvcrt

    def _lock_acquire(fh) -> None:
        # Lock a 1-byte region at offset 0. LK_LOCK blocks (retrying ~10s
        # internally) then raises; loop so a longer contention still resolves
        # instead of falling through unlocked. Bounded so we can never spin
        # forever on a non-contention error.
        fh.seek(0)
        for _ in range(6):  # ~60s worst-case ceiling
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # Best-effort: a transient lock error retries within the bounded
                # ceiling above; a persistent one raises below (never silent).
                continue
        raise OSError("could not acquire jsonl lock")

    def _lock_release(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_acquire(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _lock_release(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def jsonl_lock(target_path: Path):
    """Exclusive lock guarding a JSONL file's read-modify-write / append.

    Takes a host-level exclusive lock on the sidecar `<target>.lock` file so
    that appends and rotation of `target_path` are serialized and no append is
    ever clobbered by an in-flight rotation. Cross-platform (POSIX flock /
    Windows msvcrt). Best-effort: yields even if the lock can't be acquired,
    so a locking failure never breaks the operator's session.

    Not re-entrant — never nest two `jsonl_lock(same_path)` on one thread
    (POSIX flock/msvcrt would self-deadlock). Callers append-then-rotate as
    two sequential locked sections, never nested.
    """
    lock_path = target_path.with_name(target_path.name + ".lock")
    fh = None
    acquired = False
    try:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+")
            _lock_acquire(fh)
            acquired = True
        except OSError:
            acquired = False
        yield
    finally:
        if fh is not None:
            if acquired:
                _lock_release(fh)
            try:
                fh.close()
            except OSError:
                # Best-effort: a close failure after the lock is released is
                # non-fatal — the fd is abandoned at process exit.
                pass


# PATCH-144 v1.2.0 — JSONL rotation thresholds (audit D1).
#
# Before PATCH-144, observations.jsonl and nudge_history.jsonl grew without
# bound. Operator's machine showed observations.jsonl at 1.7 MB after 6 days
# of use — projected 30-60 MB per project per year, with multi-project
# customers carrying 500 MB – 1 GB of state log on disk eventually. No
# silent disk-pressure protection at all.
#
# PATCH-144 adds inline rotation: after each `append_observation` (or
# equivalent jsonl append in client_state_writer.py), check the file size;
# when it crosses the per-file threshold, rotate the tail to a gzipped
# archive and keep only the last N entries hot.
OBSERVATIONS_ROTATE_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5 MB
OBSERVATIONS_KEEP_RECENT_ENTRIES = 1000
NUDGE_HISTORY_ROTATE_THRESHOLD_BYTES = 2 * 1024 * 1024  # 2 MB
NUDGE_HISTORY_KEEP_RECENT_ENTRIES = 500


def rotate_jsonl_if_oversize(
    jsonl_path: Path,
    threshold_bytes: int,
    keep_recent: int,
) -> bool:
    """Rotate a JSONL file if it has grown past threshold_bytes.

    After rotation:
      - `<name>.jsonl` keeps the last `keep_recent` entries (hot store).
      - `<name>.YYYYMMDDTHHMMSSZ.jsonl.gz` holds everything earlier (archive).

    Safe under concurrent appends (M-04): the read-modify-replace runs while
    holding `jsonl_lock`, and the append path takes the same lock, so an append
    can never be lost between rotation's read and its replace. Idempotent, and
    best-effort — silent on any filesystem error so a hook never breaks the
    operator's session. Returns True if rotation occurred, False otherwise.
    """
    # Cheap pre-check outside the lock: avoid taking the lock on every append
    # when the file is nowhere near threshold (the common case). A stale read
    # here is harmless — the size is re-confirmed under the lock below.
    try:
        if not jsonl_path.is_file():
            return False
        if jsonl_path.stat().st_size <= threshold_bytes:
            return False
    except OSError:
        return False

    with jsonl_lock(jsonl_path):
        # Re-confirm under the lock: another writer may have just rotated.
        try:
            if not jsonl_path.is_file():
                return False
            size = jsonl_path.stat().st_size
            if size <= threshold_bytes:
                return False
        except OSError:
            return False

        try:
            # Read all lines from the source file.
            with jsonl_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return False

        if len(lines) <= keep_recent:
            # File is over byte threshold but has few enough entries that
            # rotation wouldn't help (each entry is very large). Skip.
            return False

        # Split: archive = older entries; keep = last keep_recent.
        cut = len(lines) - keep_recent
        archive_lines = lines[:cut]
        keep_lines = lines[cut:]

        # Write archive as a gzipped JSONL. M-04: the archive name must be
        # collision-safe. Second-level precision + a TRUNCATE ("wt") open meant
        # two rotations within one second reused the same filename and the second
        # OVERWROTE the first archive — every record the first rotation moved was
        # lost. Fix: exclusive-create ("xt") so an existing archive is never
        # clobbered, with a numeric counter that bumps the name until a free one
        # is found. (Rotation is already serialized by jsonl_lock, so the loop
        # only needs to walk past this second's earlier archives.)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        gz = None
        counter = 0
        while gz is None:
            suffix = f".{ts}.jsonl.gz" if counter == 0 else f".{ts}-{counter}.jsonl.gz"
            archive_path = jsonl_path.with_suffix(suffix)
            try:
                gz = gzip.open(archive_path, "xt", encoding="utf-8")
            except FileExistsError:
                counter += 1
                if counter > 100_000:
                    # Pathological — never spin forever; bail without data loss
                    # (source file is still intact and untouched below).
                    return False
                continue
            except OSError:
                return False
        try:
            with gz:
                gz.writelines(archive_lines)
        except OSError:
            return False

        # Replace the source with the kept tail (atomic via rename). Any append
        # that raced in is blocked on the lock until after this replace, so it
        # lands on the rotated tail instead of being clobbered.
        tmp_path = jsonl_path.with_suffix(jsonl_path.suffix + ".rotating")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                f.writelines(keep_lines)
            os.replace(tmp_path, jsonl_path)
        except OSError:
            # Clean up partial state: keep the archive (no data loss) but
            # leave the source intact if the rename failed.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                # Best-effort: temp cleanup failure is non-fatal — the archive
                # is already kept (no data loss) and the source is left intact.
                pass
            return False

        # Record how many entries left the hot file, so consumers that need a
        # MONOTONIC count can have one. See `_bump_archived_line_count`.
        # Inside the lock on purpose: two rotations must not read-modify-write
        # this ledger concurrently.
        _bump_archived_line_count(
            jsonl_path, sum(1 for line in archive_lines if line.strip()),
        )

        return True


# Sidecar recording how many entries rotation has moved out of a hot JSONL
# file. Named `.rotated.json` (not `.jsonl`) so no directory walk that globs
# for silos or observation logs picks it up as data.
ROTATION_LEDGER_SUFFIX = ".rotated.json"


def rotation_ledger_path(jsonl_path: Path) -> Path:
    """Sidecar path holding the archived-entry count for `jsonl_path`."""
    return jsonl_path.with_name(jsonl_path.name + ROTATION_LEDGER_SUFFIX)


def archived_line_count(jsonl_path: Path) -> int:
    """Entries rotation has moved out of `jsonl_path`. 0 when unknown.

    Unknown reads as 0 deliberately: a missing or unreadable ledger means the
    file has never rotated (the overwhelmingly common case), and the caller's
    count then degrades to exactly the pre-existing hot-file count rather than
    to something worse.
    """
    path = rotation_ledger_path(jsonl_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    value = data.get("archived_lines") if isinstance(data, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


def _bump_archived_line_count(jsonl_path: Path, added: int) -> None:
    """Add `added` to the archived-entry ledger for `jsonl_path`.

    Best-effort: a ledger write failure must never fail a rotation that has
    already succeeded (the archive is written and the tail is in place). The
    consequence of losing it is that the count goes non-monotonic once more,
    which is the pre-fix behaviour, not a new failure.
    """
    if added <= 0:
        return
    path = rotation_ledger_path(jsonl_path)
    try:
        atomic_write_text(
            path,
            json.dumps({"archived_lines": archived_line_count(jsonl_path) + added}),
        )
    except OSError:
        # Best-effort: see the docstring. A ledger write failure must not fail a
        # rotation that has already succeeded, and losing the ledger only
        # restores the pre-fix non-monotonic count rather than causing a new
        # failure. The marker is repeated here because the silent-failure audit
        # looks within five lines of the handler, not at the docstring.
        pass


def resolve_state_dir(
    project_root: Path | None = None,
    *,
    state_dir_name: str | None = None,
) -> Path:
    """Return the path to the operator's `.allostat/` directory for the
    active project.

    `state_dir_name` honors the wrapper plugin's `state_dir` config
    (defaults to `.allostat` to share with v2.3 plugin; can be set to
    `.allostat-mcp` for differential isolation).

    Doesn't create the directory — callers do so before writing.
    """
    if state_dir_name is None:
        state_dir_name = os.environ.get(
            "ALLOSTAT_STATE_DIR_NAME", DEFAULT_STATE_DIR_NAME
        )
    if project_root is None:
        project_root = resolve_project_root() or Path.cwd()
    return project_root / state_dir_name


def resolve_project_root(starting_path: Path | None = None) -> Path | None:
    """Walk up from starting_path (default cwd) looking for an
    `.allostat/` directory. Returns the ancestor that owns it, or None
    when no ancestor has one (caller falls back to cwd or to
    /allostat init flow)."""
    if starting_path is None:
        starting_path = Path.cwd()
    starting_path = starting_path.resolve()
    for parent in (starting_path, *starting_path.parents):
        if (parent / DEFAULT_STATE_DIR_NAME).is_dir():
            return parent
    return None


# Pre-release item 15 — tail-read cap. read_recent_observations fires on
# every hook event but only ever needs the last `window` (<=200) entries;
# reading the whole file (up to the 5 MB rotation cap) on each fire was
# the hot-path waste. 256 KB comfortably covers >1000 typical entries
# (commands are truncated to 500 chars per entry).
OBSERVATIONS_TAIL_READ_BYTES = 256 * 1024


def read_recent_observations(
    state_dir: Path,
    *,
    window: int = 100,
) -> list[dict[str, Any]]:
    """Tail of the observation log, parsed as dicts. Malformed lines are
    silently skipped.

    Reads at most the last OBSERVATIONS_TAIL_READ_BYTES of the file
    (item 15): semantics are identical for files under the cap; for
    oversize files the seek lands mid-line and the partial first line
    is dropped.
    """
    obs_path = state_dir / "observations.jsonl"
    if not obs_path.is_file():
        return []
    try:
        size = obs_path.stat().st_size
        with obs_path.open("rb") as f:
            if size > OBSERVATIONS_TAIL_READ_BYTES:
                f.seek(size - OBSERVATIONS_TAIL_READ_BYTES)
                raw = f.read()
                # Drop the (almost certainly partial) first line so we
                # only parse whole records from a line boundary.
                cut = raw.find(b"\n")
                raw = raw[cut + 1:] if cut != -1 else b""
            else:
                raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-window:]:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


# data-min LB-1 (2026-07-14): the wire scrub is an ALLOWLIST, not a denylist.
#
# History: the first data-min pass (denylist) stripped four named operator-text
# fields (operator_language/prompt/subject/text). Codex proved a denylist is
# structurally blind to the NEXT carrier — it found operator prompt text still
# crossing via `phrase` (session_end_signal_detected) and `prompt_excerpt`
# (interrupt_chrome_suppressed), neither on the denylist. A denylist of field
# NAMES can never keep up with new carriers.
#
# Inversion: on the wire, a STRING value survives only if its key is a known
# metadata key the server actually reads (below). Every OTHER string is scrubbed
# by default; non-string scalars (counts/bools) always pass; containers recurse.
# Operator language is always a string under some key, so a brand-new carrier is
# stripped the day it is added — "metadata-only history" holds by CONSTRUCTION.
#
# The allowlist is defined against what the SERVER pillars read from the tail
# (audited 2026-07-14, dispatch_tools.py + pillars/*): stripping a consumed key
# silently breaks a pillar, so this set must stay a superset of every server read.
# Pattern-learning that consumed operator prose (subject/operator_language/text)
# already runs client-side (pattern_observer_local), so those keys are absent
# here on purpose — the server fingerprint path is dormant by design.
_WIRE_SAFE_STRING_KEYS = frozenset({
    # record-level discriminators
    "event", "timestamp", "class",
    # session / correlation ids
    "sessionId", "session_id",
    # tool telemetry — pillar match-keys (stress-response orphan/failure pairing,
    # volume-control legacy count). Accepted wire scope: Codex flagged prose, not
    # telemetry. If the data-min bar later requires hashing paths/commands, that
    # is a pillar-side follow-up (the match-keys would move to stable hashes).
    "tool_name", "operation", "file_path", "command",
    # bounded enums / derived tags the server reads
    "override_target", "direction", "trigger", "category", "severity", "marker",
    # already-hashed carrier (voice snippet -> context_sha256)
    "context_sha256",
})

# Illustrative — the KNOWN operator-text carriers (seeded by the privacy guards).
# NOT the scrub mechanism anymore: the allowlist strips these AND any future
# carrier. Kept so the guard battery can enumerate known-bad fields explicitly.
_WIRE_STRIP_DETAIL_FIELDS = (
    "operator_language", "prompt", "subject", "text", "phrase", "prompt_excerpt",
)


def _scrub_wire_obj(obj: Any) -> Any:
    """Recursively rebuild `obj` keeping only allowlisted string values.

    Fail-closed rebuild (never mutates the input):
      - dict: keep non-string scalars as-is; recurse dict/list values; a string
        value survives only if its key is in `_WIRE_SAFE_STRING_KEYS`, else it is
        DROPPED. A `snippet` string is HASHED into a sibling `context_sha256`
        (voice-violation prose, Wave-2 H-02) rather than dropped.
      - list: recurse dict/list elements; keep non-string scalars; DROP bare
        string elements (un-keyed prose has no allowlist justification).
      - scalar: returned unchanged.
    """
    if isinstance(obj, dict):
        new: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(value, str):
                if key == "snippet":
                    if value:
                        new["context_sha256"] = hashlib.sha256(
                            value.encode("utf-8")
                        ).hexdigest()[:16]
                elif key in _WIRE_SAFE_STRING_KEYS:
                    new[key] = value
                # else: non-allowlisted operator-text string -> drop
            elif isinstance(value, (dict, list)):
                new[key] = _scrub_wire_obj(value)
            else:
                new[key] = value
        return new
    if isinstance(obj, list):
        out_list: list[Any] = []
        for item in obj:
            if isinstance(item, str):
                continue  # bare prose element -> drop (fail-closed)
            if isinstance(item, (dict, list)):
                out_list.append(_scrub_wire_obj(item))
            else:
                out_list.append(item)
        return out_list
    return obj


def scrub_observations_for_wire(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Wire-boundary scrub for excerpt.observations — fail-closed allowlist.

    Each record is rebuilt (the local record is never mutated) keeping only
    metadata the server reads: allowlisted string keys, all non-string scalars,
    and recursed containers. Every other string (operator prose) is dropped, and
    voice-violation `snippet` prose is replaced by its truncated sha256. A new
    operator-text carrier is a new string key, so it is stripped by construction
    until explicitly allowlisted — see `_WIRE_SAFE_STRING_KEYS`.
    """
    out: list[dict[str, Any]] = []
    for record in observations:
        if not isinstance(record, dict):
            out.append(record)
            continue
        out.append(_scrub_wire_obj(record))
    return out


def read_state_json(path: Path, *, default=None):
    """Read a JSON state file, returning `default` on missing/unparseable.

    Generic helper for the wrapper's per-project `.allostat/state/*.json`
    files. Callers pass an explicit `default` to choose their preferred
    sentinel (`{}` for "treat absence as empty", `None` for "treat absence
    as not-yet-initialized").
    """
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def write_state_json(path: Path, data: dict) -> bool:
    """Write a dict to a JSON state file. Creates parent dir. Best-effort:
    returns True on success, False on OSError (never raises)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def count_observations(state_dir: Path) -> int:
    """Total observations this project has ever recorded — archived + hot.

    Used as a cheap observation-index proxy for fingerprint dedup
    (`session_handoff.write_session_handoff_if_needed`), which compares the
    current count against the count at the previous handoff and skips the write
    when it has not grown.

    Audit 2026-07-21: this used to count only the non-blank lines of the HOT
    file, and `rotate_jsonl_if_oversize` truncates that file to its last 1000
    entries. So the number a monotonic work-cursor was reading DROPPED by tens
    of thousands the moment rotation fired, and because the session_id had not
    changed, `current <= prior` stayed true for the rest of the session: every
    remaining session-end handoff was silently skipped, with no escape short of
    the operator typing an end-session phrase. Verified end to end before the
    fix (2000 obs -> handoff written; rotate to 1000; +700 obs -> still
    skipped).

    Adding the archived count restores monotonicity at the source, which fixes
    every consumer rather than one caller's comparison. The ledger is
    best-effort: when it is missing this returns exactly the old hot-file count,
    so a fresh install and a degraded one behave as before.
    """
    obs_path = state_dir / "observations.jsonl"
    archived = archived_line_count(obs_path)
    if not obs_path.is_file():
        return archived
    try:
        with open(obs_path, "r", encoding="utf-8") as f:
            return archived + sum(1 for line in f if line.strip())
    except OSError:
        return archived


def iter_observations(state_dir: Path):
    """Yield parsed observation dicts from observations.jsonl (no window).

    Generator; malformed lines + non-dict events silently skipped.
    Used by consumers that need to scan the whole observation log.
    """
    obs_path = state_dir / "observations.jsonl"
    if not obs_path.is_file():
        return
    try:
        with open(obs_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def append_observation(
    state_dir: Path,
    event_type: str,
    *,
    details: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> bool:
    """Append one event to observations.jsonl. Returns True on success.

    Best-effort: silent on filesystem errors so a hook never breaks the
    operator's session.
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        obs_path = state_dir / "observations.jsonl"
        record = {
            "timestamp": timestamp or utc_iso(),
            "event": event_type,
            "details": details or {},
        }
        # M-04: append under the shared lock so a concurrent rotation cannot
        # clobber this write. Held only for the append, then released before
        # rotate (which re-takes the lock) — never nested (self-deadlock).
        with jsonl_lock(obs_path):
            with obs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        # PATCH-144 v1.2.0 — rotate observations.jsonl when it crosses
        # threshold. Best-effort; silent on filesystem error.
        try:
            rotate_jsonl_if_oversize(
                obs_path,
                OBSERVATIONS_ROTATE_THRESHOLD_BYTES,
                OBSERVATIONS_KEEP_RECENT_ENTRIES,
            )
        except Exception:
            pass
        return True
    except OSError:
        return False


def read_session_state(state_dir: Path) -> dict[str, Any] | None:
    """Parse `.allostat/state.json` if present. Returns None on any problem —
    including a well-formed-but-non-dict file (e.g. a hand-edited JSON array),
    which pre-fix flowed through callers' `... or {}` guard (a non-empty list
    is truthy) and crashed the every-prompt dispatch on the first `.get()`."""
    path = state_dir / "state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_text(path: Path, content: Any, encoding: str = "utf-8") -> None:
    """Write text durably: serialize to a sibling temp file, then os.replace()
    onto the target (atomic on POSIX and Windows). A crash mid-write leaves the
    original untouched rather than truncated. Raises on failure AFTER leaving
    the original intact — callers that want best-effort should catch.

    Shared crash-safety primitive for the deep-audit atomicity fixes: any write
    to an operator-relied-on file (memory rules, MEMORY.md, state) should route
    through here instead of a bare path.write_text()."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = str(content)  # force serialization failures BEFORE touching disk
    tmp = path.with_name(f"{path.name}.{os.getpid()}.allostat-tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def update_session_state_key(
    state_dir: Path, key: str, value: Any
) -> bool:
    """Best-effort merge a single top-level key into state.json.

    PATCH-140 v1.1.0 (Lever D): used by user-prompt-submit hook to record
    `last_surfaced_threshold` after a handoff red box fires, so the server
    can skip re-firing on subsequent turns within the same band.

    Returns True on successful write. Silent on filesystem errors.
    """
    path = state_dir / "state.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        # M-04: the read-modify-write must be ATOMIC across concurrent callers.
        # Unlocked, two updates both read the same prior state, each set their own
        # key, and the later writer clobbered the earlier key — one update lost.
        # Hold the shared jsonl_lock across the whole read+write so the second
        # caller reads the first's result. Atomic (deep-audit 2026-07-02): this
        # fires on effectively every prompt; the temp+rename inside
        # atomic_write_text keeps a crash mid-write from truncating state.json.
        with jsonl_lock(path):
            state: dict[str, Any]
            if path.is_file():
                try:
                    state = json.loads(path.read_text(encoding="utf-8")) or {}
                except json.JSONDecodeError:
                    state = {}
            else:
                state = {}
            if not isinstance(state, dict):
                state = {}
            state[key] = value
            atomic_write_text(path, json.dumps(state, indent=2))
        return True
    except OSError:
        return False


def read_drift_seen(state_dir: Path) -> list[dict[str, Any]]:
    """Return the stress drift-seen dedup entries (used for salience
    suppression). Empty list if the file is missing or malformed."""
    path = state_dir / "state" / "stress_drift_seen.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return []
    return entries


def write_drift_seen_entry(
    state_dir: Path,
    payload_hash: str,
    payload_excerpt: str,
) -> bool:
    """Append-or-increment a drift-seen entry for the given fingerprint.

    Mirrors plugin/lib/stress_response_runtime.py::_record_drift_seen.
    """
    path = state_dir / "state" / "stress_drift_seen.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any]
        if path.is_file():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {"entries": []}
        else:
            state = {"entries": []}
        if not isinstance(state, dict):
            state = {"entries": []}
        entries = state.setdefault("entries", [])
        if not isinstance(entries, list):
            entries = []
            state["entries"] = entries

        now_iso = utc_iso()
        for entry in entries:
            if isinstance(entry, dict) and entry.get("hash") == payload_hash:
                entry["fire_count"] = int(entry.get("fire_count", 0)) + 1
                entry["last_seen"] = now_iso
                break
        else:
            entries.append(
                {
                    "hash": payload_hash,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "fire_count": 1,
                    "payload_excerpt": (payload_excerpt or "")[:80],
                }
            )
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def read_orphan_salience_state(state_dir: Path) -> list[dict[str, Any]]:
    """Return the orphan-detector salience entries (pre-release item 50).

    The server's orphan-tool-call detector is stateless per request; the
    wrapper owns this state and ships it on every dispatch call as
    `event.orphan_salience_state = {"entries": [...]}`. Each entry is
    `{"tool_name": <str>, "last_orphan_fire_timestamp": <ISO8601 str>}`.
    Empty list if the file is missing or malformed (the server treats
    that as no-prior-fires — both sides roll independently)."""
    path = state_dir / "state" / "stress_orphan_salience.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def write_orphan_salience_state(
    state_dir: Path,
    entries: list[dict[str, Any]],
) -> bool:
    """Persist the server-returned orphan-salience entries (the updated
    list from `decision.details.orphan_state` — pre-release item 50).

    Full-replace semantics: the server returns the complete updated list
    each call. Best-effort: returns False on OSError (never raises)."""
    path = state_dir / "state" / "stress_orphan_salience.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic (deep-audit 2026-07-02): crash mid-write must not truncate.
        atomic_write_text(path, json.dumps({"entries": entries}, indent=2))
        return True
    except OSError:
        return False


def read_silo_entries(
    state_dir: Path,
    class_name: str,
) -> list[dict[str, Any]]:
    """Return parsed entries from `.allostat/silos/<class>.jsonl`."""
    path = state_dir / "silos" / f"{class_name}.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def list_silo_classes(state_dir: Path) -> list[str]:
    """Return the silo classes present in `.allostat/silos/`."""
    silos_dir = state_dir / "silos"
    if not silos_dir.is_dir():
        return []
    return sorted(
        p.stem for p in silos_dir.glob("*.jsonl") if p.is_file()
    )


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
