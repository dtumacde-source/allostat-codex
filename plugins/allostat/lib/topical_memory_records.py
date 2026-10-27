"""Controlled typed updates inside existing Allostat topical memory leaves."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit


SCHEMA = "allostat.topical-record/v1"
BEGIN_PREFIX = "<!-- ALLOSTAT-MANAGED-RECORDS v1"
END_MARKER = "<!-- /ALLOSTAT-MANAGED-RECORDS -->"
BEGIN_RE = re.compile(
    rb"<!-- ALLOSTAT-MANAGED-RECORDS v1 generation=(\d+) cue_owner=(managed|curated) "
    rb"cue_preimage=([0-9a-f]{64}|none) -->"
)
ENTRY_CAP = 150
MAX_FIELD_CHARS = 4096
MAX_ENVELOPE_BYTES = 16 * 1024
MAX_RECORDS_PER_LEAF = 256
MAX_MANAGED_BLOCK_BYTES = 256 * 1024
MAX_LEAF_BYTES = 512 * 1024
_SAFE_STATUS = {"active", "superseded", "known_wrong", "known_unsafe", "uncertain"}
_VERIFY = {"user_reported", "assistant_generated", "externally_verified", "unverified", "known_wrong"}
_KINDS = {
    "relation", "event", "state", "exact_output", "preference",
    "legacy_unstructured", "correction",
}
_PRIVATE_PATH = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+|"
    r"//[^/\s]+/[^/\s]+|/(?:home|users|private|var/lib|root(?:/\.ssh)?|etc)(?:/|$))"
)
_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s<>\"']+")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


class ValidationError(ValueError):
    pass


class PrivacyError(ValidationError):
    pass


class RecoveryError(RuntimeError):
    pass


class InjectedCrash(RuntimeError):
    pass


def tool_record_schema() -> dict[str, Any]:
    """The same closed discriminated union advertised over MCP."""
    common = {
        "schema": {"const": SCHEMA},
        "kind": {"type": "string", "enum": sorted(_KINDS)},
        "record_id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,79}$"},
        "leaf_topic": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,79}$"},
        "status": {"type": "string", "enum": sorted(_SAFE_STATUS)},
        "claim_verification": {"type": "string", "enum": sorted(_VERIFY)},
        "correction_record_id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,79}$"},
    }
    base_required = ["schema", "kind", "record_id", "leaf_topic", "status", "claim_verification"]

    def variant(kind: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        props = dict(common)
        props["kind"] = {"const": kind}
        props.update(properties)
        return {
            "type": "object", "additionalProperties": False,
            "properties": props, "required": base_required + required,
        }

    text = {"type": "string", "minLength": 1, "maxLength": MAX_FIELD_CHARS}
    nullable_text = {"type": ["string", "null"], "maxLength": MAX_FIELD_CHARS}
    return {"oneOf": [
        variant("relation", {
            "subject": text, "predicate": text, "object_or_value": text,
            "negated": {"type": "boolean"}, "condition": nullable_text,
            "quantity": {"type": "number"}, "unit": text,
        }, ["subject", "predicate", "object_or_value"]),
        variant("event", {
            "actors": {"type": "array", "minItems": 1, "maxItems": 64, "items": text},
            "action": text, "asserted_event_time": nullable_text,
            "participants": {"type": "array", "maxItems": 64, "items": text},
            "asserted_historical_author": text,
            "interval": {
                "type": "object", "additionalProperties": False,
                "properties": {"start": nullable_text, "end": nullable_text,
                               "duration": {"type": ["number", "null"]}, "unit": nullable_text},
            },
        }, ["actors", "action", "asserted_event_time"]),
        variant("state", {
            "entity": text, "field": text, "value": text, "effective_at": nullable_text,
            "supersedes_record_id": text,
        }, ["entity", "field", "value", "effective_at"]),
        variant("exact_output", {
            "historical_output_author": text, "output_type": text, "value": text,
        }, ["historical_output_author", "output_type", "value"]),
        variant("preference", {
            "subject": text, "preference": text,
            "polarity": {"type": "string", "enum": ["likes", "dislikes", "prefers", "avoids", "neutral"]},
            "context": nullable_text, "condition": nullable_text,
        }, ["subject", "preference", "polarity"]),
        variant("legacy_unstructured", {"text": text, "reason": text}, ["text", "reason"]),
        variant("correction", {
            "target_record_id": text, "correction_record_id": text,
            "disposition": {
                "type": "string",
                "enum": ["known_wrong", "known_unsafe", "retracted"],
            },
            "reason": text,
        }, ["target_record_id", "correction_record_id", "disposition", "reason"]),
    ]}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _flatten_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _flatten_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_strings(item)


def _privacy_check(value: Any) -> None:
    try:
        from secret_redaction import redact_secrets
    except ImportError:
        redact_secrets = lambda text: text
    for text in _flatten_strings(value):
        if BEGIN_PREFIX in text or END_MARKER in text:
            raise ValidationError("record contains reserved serialization markers")
        if redact_secrets(text) != text:
            raise PrivacyError("record contains protected material")
        if re.search(r"(?i)\bfile:", text):
            raise PrivacyError("record contains protected material")
        local_text = text
        for match in reversed(list(_URL.finditer(text))):
            url = match.group(0)
            try:
                parsed = urlsplit(url)
                _ = parsed.port
            except ValueError as exc:
                raise PrivacyError("record contains protected material") from exc
            if parsed.scheme.lower() == "file":
                raise PrivacyError("record contains protected material")
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise PrivacyError("record contains protected material")
            if any(
                _PRIVATE_PATH.search(unquote(component))
                for component in (parsed.query, parsed.fragment)
            ):
                raise PrivacyError("record contains protected material")
            local_text = local_text[:match.start()] + " " * (match.end() - match.start()) + local_text[match.end():]
        if _PRIVATE_PATH.search(local_text):
            raise PrivacyError("record contains protected material")


def _require_string(record: dict[str, Any], key: str, *, nullable: bool = False) -> None:
    value = record.get(key)
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_CHARS:
        raise ValidationError(f"{key} must be a bounded non-empty string")


def validate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ValidationError("record envelope must be an object")
    kind = envelope.get("kind")
    if envelope.get("schema") != SCHEMA or kind not in _KINDS:
        raise ValidationError("unsupported record schema or kind")
    common = {
        "schema", "kind", "record_id", "leaf_topic", "status",
        "claim_verification", "correction_record_id",
    }
    fields = {
        "relation": common | {"subject", "predicate", "object_or_value", "negated", "condition", "quantity", "unit"},
        "event": common | {"actors", "action", "asserted_event_time", "interval", "participants", "asserted_historical_author"},
        "state": common | {"entity", "field", "value", "effective_at", "supersedes_record_id"},
        "exact_output": common | {"historical_output_author", "output_type", "value"},
        "preference": common | {"subject", "preference", "polarity", "context", "condition"},
        "legacy_unstructured": common | {"text", "reason"},
        "correction": common | {
            "target_record_id", "correction_record_id", "disposition", "reason",
        },
    }[kind]
    unknown = set(envelope) - fields
    if unknown:
        raise ValidationError("record contains unsupported fields")
    for key in {"record_id", "leaf_topic", "status", "claim_verification"}:
        _require_string(envelope, key)
    if not _SLUG.fullmatch(envelope["record_id"]):
        raise ValidationError("invalid record_id")
    if not _SLUG.fullmatch(envelope["leaf_topic"]):
        raise ValidationError("invalid leaf_topic")
    if envelope["status"] not in _SAFE_STATUS or envelope["claim_verification"] not in _VERIFY:
        raise ValidationError("unsupported status or verification")
    correction_id = envelope.get("correction_record_id")
    if correction_id is not None and (
        not isinstance(correction_id, str) or not _SLUG.fullmatch(correction_id)
    ):
        raise ValidationError("invalid correction_record_id")
    if envelope["status"] in {"known_wrong", "known_unsafe"}:
        if envelope["claim_verification"] != "known_wrong" or correction_id is None:
            raise ValidationError("wrong or unsafe history requires correction semantics")
    if kind == "relation":
        for key in ("subject", "predicate", "object_or_value"):
            _require_string(envelope, key)
        if "negated" in envelope and not isinstance(envelope["negated"], bool):
            raise ValidationError("negated must be boolean")
        if "quantity" in envelope and (
            isinstance(envelope["quantity"], bool)
            or not isinstance(envelope["quantity"], (int, float))
            or not math.isfinite(float(envelope["quantity"]))
        ):
            raise ValidationError("quantity must be numeric")
        if "unit" in envelope:
            _require_string(envelope, "unit")
        if "condition" in envelope and envelope["condition"] is not None:
            _require_string(envelope, "condition")
    elif kind == "event":
        actors = envelope.get("actors")
        if not isinstance(actors, list) or not actors or len(actors) > 64 or any(not isinstance(v, str) or not v for v in actors):
            raise ValidationError("actors must be a non-empty string list")
        _require_string(envelope, "action")
        if envelope.get("asserted_event_time") is not None:
            _require_string(envelope, "asserted_event_time")
        if "participants" in envelope and (
            not isinstance(envelope["participants"], list)
            or len(envelope["participants"]) > 64
            or any(not isinstance(v, str) or not v for v in envelope["participants"])
        ):
            raise ValidationError("participants must be strings")
        if "interval" in envelope:
            interval = envelope["interval"]
            if not isinstance(interval, dict) or not set(interval).issubset({"start", "end", "duration", "unit"}):
                raise ValidationError("interval must be a typed object")
            for key in ("start", "end", "unit"):
                if key in interval and interval[key] is not None and (not isinstance(interval[key], str) or not interval[key]):
                    raise ValidationError("invalid interval field")
            if "duration" in interval and interval["duration"] is not None and (
                isinstance(interval["duration"], bool)
                or not isinstance(interval["duration"], (int, float))
                or not math.isfinite(float(interval["duration"]))
            ):
                raise ValidationError("interval duration must be a finite number")
        if "asserted_historical_author" in envelope:
            _require_string(envelope, "asserted_historical_author")
    elif kind == "state":
        for key in ("entity", "field", "value"):
            _require_string(envelope, key)
        if envelope.get("effective_at") is not None:
            _require_string(envelope, "effective_at")
        if "supersedes_record_id" in envelope:
            _require_string(envelope, "supersedes_record_id")
            if not _SLUG.fullmatch(envelope["supersedes_record_id"]):
                raise ValidationError("invalid supersedes_record_id")
            if envelope["supersedes_record_id"] == envelope["record_id"]:
                raise ValidationError("record cannot supersede itself")
    elif kind == "exact_output":
        for key in ("historical_output_author", "output_type", "value"):
            _require_string(envelope, key)
    elif kind == "preference":
        for key in ("subject", "preference", "polarity"):
            _require_string(envelope, key)
        if envelope["polarity"] not in {"likes", "dislikes", "prefers", "avoids", "neutral"}:
            raise ValidationError("unsupported preference polarity")
        for key in ("context", "condition"):
            if key in envelope and envelope[key] is not None:
                _require_string(envelope, key)
    elif kind == "legacy_unstructured":
        _require_string(envelope, "text")
        _require_string(envelope, "reason")
    else:
        for key in ("target_record_id", "correction_record_id", "disposition", "reason"):
            _require_string(envelope, key)
        if not _SLUG.fullmatch(envelope["target_record_id"]):
            raise ValidationError("invalid target_record_id")
        if not _SLUG.fullmatch(envelope["correction_record_id"]):
            raise ValidationError("invalid correction_record_id")
        if envelope["disposition"] not in {"known_wrong", "known_unsafe", "retracted"}:
            raise ValidationError("unsupported correction disposition")
        if envelope["target_record_id"] in {
            envelope["record_id"], envelope["correction_record_id"],
        } or envelope["correction_record_id"] == envelope["record_id"]:
            raise ValidationError("correction transition ids must be distinct")
        if envelope["status"] != "active" or envelope["claim_verification"] == "known_wrong":
            raise ValidationError("correction transition must be active and safe")
    _privacy_check(envelope)
    if len(_canonical_json(envelope)) > MAX_ENVELOPE_BYTES:
        raise ValidationError("record envelope exceeds the local bound")
    return json.loads(_canonical_json(envelope))


def validate_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(provenance, dict):
        raise ValidationError("provenance must be an object")
    asserted = {"provenance_trust", "memory_reporter", "harness", "reported_at"}
    if set(provenance) != asserted:
        raise ValidationError("invalid provenance shape")
    for key in asserted:
        _require_string(provenance, key)
    if provenance["provenance_trust"] != "model_asserted":
        raise ValidationError("authenticated per-call provenance is unavailable")
    _privacy_check(provenance)
    return json.loads(_canonical_json(provenance))


def _state_dir(memory_dir: Path) -> Path:
    return memory_dir.parent / ".allostat" / "state"


def dirty_marker_path(memory_dir: Path) -> Path:
    return _state_dir(Path(memory_dir)) / "topical_memory_dirty_v1.json"


def _lock_path(memory_dir: Path) -> Path:
    return _state_dir(Path(memory_dir)) / "topical_memory_transaction_v1.lock"


if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(handle) -> None:
        handle.seek(0)
        try: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError: pass
else:
    import fcntl

    def _lock_file(handle) -> None: fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    def _unlock_file(handle) -> None: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _transaction_lock(memory_dir: Path):
    path = _lock_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"0")
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    with path.open("r+b") as handle:
        _lock_file(handle)
        try: yield
        finally: _unlock_file(handle)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError: pass
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _privacy_check(value)
    _atomic_bytes(path, _canonical_json(value) + b"\n")


def _resolve_leaf(memory_dir: Path, topic: str) -> tuple[Path, str]:
    root = memory_dir.resolve()
    candidates = [root / f"{prefix}_{topic}.md" for prefix in ("project", "feedback", "reference")]
    candidates += [p for p in root.rglob("*.md") if p.stem.lower().removeprefix("project_").removeprefix("feedback_").removeprefix("reference_") == topic]
    existing = []
    for path in candidates:
        try:
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                existing.append(path.resolve())
        except OSError:
            # Best-effort: a candidate may disappear during concurrent cleanup.
            continue
    unique = sorted(set(existing))
    if len(unique) > 1:
        raise ValidationError("leaf topic is ambiguous")
    leaf = unique[0] if unique else root / f"project_{topic}.md"
    if leaf.exists() and leaf.is_symlink():
        raise ValidationError("managed leaf cannot be a link")
    if not leaf.resolve().is_relative_to(root):
        raise ValidationError("managed leaf escaped memory root")
    rel = leaf.relative_to(root).as_posix()
    return leaf, rel


_PAYLOAD_FIELDS = {
    "relation": ("subject", "predicate", "object_or_value", "negated", "condition", "quantity", "unit"),
    "event": ("actors", "action", "asserted_event_time", "interval", "participants", "asserted_historical_author"),
    "state": ("entity", "field", "value", "effective_at", "supersedes_record_id"),
    "exact_output": ("historical_output_author", "output_type", "value"),
    "preference": ("subject", "preference", "polarity", "context", "condition"),
    "legacy_unstructured": ("text", "reason"),
    "correction": ("target_record_id", "correction_record_id", "disposition", "reason"),
}
_PAYLOAD_REQUIRED = {
    "relation": 3, "event": 3, "state": 4,
    "exact_output": 3, "preference": 3, "legacy_unstructured": 2,
    "correction": 4,
}


def _pack_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    record = bundle["record"]
    provenance = bundle["provenance"]
    payload = [record.get(key) for key in _PAYLOAD_FIELDS[record["kind"]]]
    while len(payload) > _PAYLOAD_REQUIRED[record["kind"]] and payload[-1] is None:
        payload.pop()
    return {
        "p": {
            "t": provenance["provenance_trust"],
            "r": provenance["memory_reporter"],
            "h": provenance["harness"],
            "at": provenance["reported_at"],
        },
        "r": [
            record["kind"], record["record_id"], record["leaf_topic"],
            record["status"], record["claim_verification"],
            record.get("correction_record_id"), payload,
        ],
    }


def _unpack_bundle(item: dict[str, Any]) -> dict[str, Any]:
    # Blocked pre-review M1 leaves used verbose bundles. They remain readable;
    # the next controlled write compacts them without changing semantics.
    if set(item) == {"provenance", "record"}:
        return item
    if set(item) != {"p", "r"} or not isinstance(item["p"], dict) or not isinstance(item["r"], list):
        raise RecoveryError("managed record bundle is invalid")
    p, row = item["p"], item["r"]
    if set(p) != {"t", "r", "h", "at"} or len(row) != 7 or not isinstance(row[6], list):
        raise RecoveryError("managed compact record bundle is invalid")
    kind = row[0]
    fields = _PAYLOAD_FIELDS.get(kind)
    if fields is None or len(row[6]) > len(fields):
        raise RecoveryError("managed compact record kind is invalid")
    record = {
        "schema": SCHEMA, "kind": kind, "record_id": row[1],
        "leaf_topic": row[2], "status": row[3], "claim_verification": row[4],
    }
    if row[5] is not None:
        record["correction_record_id"] = row[5]
    for key, value in zip(fields, row[6]):
        if value is not None:
            record[key] = value
    provenance = {
        "provenance_trust": p["t"], "memory_reporter": p["r"],
        "harness": p["h"], "reported_at": p["at"],
    }
    try:
        return {
            "provenance": validate_provenance(provenance),
            "record": validate_envelope(record),
        }
    except ValidationError as exc:
        raise RecoveryError("managed compact record failed validation") from exc


def _parse_leaf(raw: bytes) -> tuple[bytes, list[dict[str, Any]], int, str, str]:
    begin_marker = BEGIN_PREFIX.encode("ascii")
    end_marker = END_MARKER.encode("ascii")
    begin_count = raw.count(begin_marker)
    end_count = raw.count(end_marker)
    if begin_count == 0 and end_count == 0:
        return raw, [], 0, "managed", "none"
    if begin_count != 1 or end_count != 1:
        raise RecoveryError("managed block markers are incomplete or duplicated")
    begin = BEGIN_RE.search(raw)
    if begin is None:
        raise RecoveryError("managed block begin marker is malformed")
    end_at = raw.find(end_marker, begin.end())
    if end_at < 0:
        raise RecoveryError("managed record block is incomplete")
    after = end_at + len(END_MARKER)
    if raw[after:after + 2] == b"\r\n": after += 2
    elif raw[after:after + 1] == b"\n": after += 1
    outside = raw[:begin.start()] + raw[after:]
    if begin_marker in outside or end_marker in outside:
        raise RecoveryError("managed block markers escaped the managed block")
    payload = raw[begin.end():end_at].strip()
    bundles = []
    for line in payload.splitlines():
        if not line.strip(): continue
        try: item = json.loads(line)
        except json.JSONDecodeError as exc: raise RecoveryError("managed record JSON is invalid") from exc
        if not isinstance(item, dict):
            raise RecoveryError("managed record bundle is invalid")
        if set(item) == {"provenance", "record"}:
            try:
                item = {
                    "provenance": validate_provenance(item["provenance"]),
                    "record": validate_envelope(item["record"]),
                }
            except (KeyError, ValidationError) as exc:
                raise RecoveryError("managed verbose record failed validation") from exc
            bundles.append(item)
        else:
            bundles.append(_unpack_bundle(item))
    return outside, bundles, int(begin.group(1)), begin.group(2).decode(), begin.group(3).decode()


def _render_leaf(outside: bytes, bundles: list[dict[str, Any]], generation: int, cue_owner: str, cue_preimage: str) -> bytes:
    if outside and not outside.endswith((b"\n", b"\r")):
        outside += b"\n"
    begin = f"{BEGIN_PREFIX} generation={generation} cue_owner={cue_owner} cue_preimage={cue_preimage} -->\n".encode("ascii")
    lines = b"\n".join(_canonical_json(_pack_bundle(item)) for item in bundles)
    return outside + begin + lines + (b"\n" if lines else b"") + END_MARKER.encode("ascii") + b"\n"


def _read_leaf(path: Path) -> bytes:
    try: return path.read_bytes()
    except FileNotFoundError: return b""


def read_managed_records(path: Path) -> list[dict[str, Any]]:
    return [item["record"] for item in _parse_leaf(_read_leaf(path))[1]]


def _record_cue(record: dict[str, Any]) -> str | None:
    if (
        record["status"] != "active"
        or record["claim_verification"] == "known_wrong"
        or record["kind"] in {"legacy_unstructured", "correction"}
    ):
        return None
    kind = record["kind"]
    if kind == "exact_output": return f"{record['output_type']}: {record['value']}"
    if kind == "state": return f"{record['entity']} {record['field']}: {record['value']}"
    if kind == "relation":
        neg = " not" if record.get("negated") else ""
        value = f"{record['subject']}{neg} {record['predicate']} {record['object_or_value']}"
        if "quantity" in record: value += f" ({record['quantity']} {record.get('unit', '')})".rstrip()
        if record.get("condition"): value += f" when {record['condition']}"
        return value
    if kind == "preference": return f"{record['subject']} {record['polarity']} {record['preference']}"
    if kind == "event": return f"{', '.join(record['actors'])} {record['action']}"
    return None


def _safe_cue(bundles: list[dict[str, Any]], topic: str, available: int) -> str:
    priority = {"exact_output": 0, "state": 1, "relation": 2, "preference": 3, "event": 4}
    retracted = {
        item["record"]["target_record_id"]
        for item in bundles
        if item["record"]["kind"] == "correction"
        and item["record"]["status"] == "active"
        and item["record"]["claim_verification"] != "known_wrong"
    }
    candidates = []
    for order, item in enumerate(bundles):
        if item["record"]["record_id"] in retracted:
            continue
        cue = _record_cue(item["record"])
        if cue:
            try: _privacy_check(cue)
            except PrivacyError: continue
            candidates.append((priority.get(item["record"]["kind"], 9), -order, " ".join(cue.split())))
    candidates.sort()
    if not candidates: return f"{topic.replace('_', ' ')} memory"
    selected = []
    for _, _, cue in candidates:
        if cue not in selected: selected.append(cue)
        joined = "; ".join(selected)
        if len(joined) > available:
            selected.pop(); break
        if len(selected) == 3: break
    cue = "; ".join(selected) or candidates[0][2]
    if len(cue) > available:
        cue = cue[:max(1, available - 1)].rstrip() + "…"
    return cue


def _index_line(index: bytes, rel: str) -> tuple[int, int, str | None]:
    text = index.decode("utf-8", errors="strict")
    pattern = re.compile(rf"(?m)^[-*+] \[[^\]]+\]\({re.escape(rel)}\)(?:\s+[—-]\s+([^\r\n]*))?\r?$")
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise ValidationError("duplicate index ownership for managed leaf")
    match = matches[0] if matches else None
    return (match.start(), match.end(), match.group(1) if match else None) if match else (-1, -1, None)


def _render_index(index: bytes, rel: str, topic: str, bundles: list[dict[str, Any]]) -> bytes:
    text = index.decode("utf-8") if index else "# Memory index\n\n## Project\n"
    start, end, _ = _index_line(index, rel)
    title = topic.replace("_", " ").title()
    prefix = f"- [{title}]({rel}) — "
    cue = _safe_cue(bundles, topic, ENTRY_CAP - len(prefix))
    line = prefix + cue
    if len(line) > ENTRY_CAP: raise RecoveryError("rendered cue exceeds index cap")
    if start >= 0:
        text = text[:start] + line + text[end:]
    else:
        if text and not text.endswith("\n"): text += "\n"
        if "## Project" not in text: text += "\n## Project\n"
        text += line + "\n"
    return text.encode("utf-8")


def _relative_identity(rel: str) -> str:
    normalized = rel.replace("\\", "/")
    if not normalized or normalized.startswith(("/", "../")) or ":" in normalized or any(p in {"", ".", ".."} for p in normalized.split("/")):
        raise ValidationError("invalid relative leaf identity")
    return normalized


def _write_marker(
    memory_dir: Path,
    rel: str,
    prior: int,
    new: int,
    intended_leaf: bytes,
    intended_index: bytes,
) -> None:
    _atomic_json(dirty_marker_path(memory_dir), {
        "schema": "topical-memory-transaction/v1", "state": "dirty",
        "transaction_id": secrets.token_hex(16), "leaf_rel": _relative_identity(rel),
        "prior_generation": prior, "new_generation": new,
        "intended_leaf_digest": _sha(intended_leaf),
        "intended_index_digest": _sha(intended_index),
    })


def _clear_marker(memory_dir: Path) -> None:
    marker = dirty_marker_path(memory_dir)
    committed = marker.with_name("topical_memory_committed_v1.json")
    if marker.is_file():
        data = json.loads(marker.read_text("utf-8")); data["state"] = "committed"
        _atomic_json(committed, data)
        marker.unlink()
        try:
            dfd = os.open(marker.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError: pass


def upsert_record(
    memory_dir: Path,
    envelope: dict[str, Any],
    provenance: dict[str, Any],
    *,
    manage_curated_cue: bool = False,
    cue_takeover_expected_sha256: str | None = None,
    crash_at: str | None = None,
) -> dict[str, Any]:
    memory_dir = Path(memory_dir)
    record = validate_envelope(envelope)
    provenance = validate_provenance(provenance)
    memory_dir.mkdir(parents=True, exist_ok=True)
    with _transaction_lock(memory_dir):
        recover_if_dirty(memory_dir, _lock_held=True)
        leaf, rel = _resolve_leaf(memory_dir, record["leaf_topic"])
        raw = _read_leaf(leaf)
        if len(raw) > MAX_LEAF_BYTES:
            raise ValidationError("managed leaf exceeds the byte budget")
        outside, bundles, generation, cue_owner, cue_preimage = _parse_leaf(raw)
        index_path = memory_dir / "MEMORY.md"
        prior_index = _read_leaf(index_path)
        start, end, prior_cue = _index_line(prior_index, rel)
        takeover_requested = manage_curated_cue or cue_takeover_expected_sha256 is not None
        if manage_curated_cue and cue_takeover_expected_sha256 is None:
            raise ValidationError("managed cue takeover requires an expected preimage")
        if cue_takeover_expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", cue_takeover_expected_sha256
        ):
            raise ValidationError("invalid cue takeover preimage")
        current_cue_digest = _sha(prior_cue or "") if start >= 0 else "none"
        if takeover_requested and cue_takeover_expected_sha256 != current_cue_digest:
            raise ValidationError("cue takeover preimage mismatch")
        curated_drift = False
        if generation > 0 and cue_owner == "managed" and start >= 0:
            title = record["leaf_topic"].replace("_", " ").title()
            prefix = f"- [{title}]({rel}) — "
            expected_cue = _safe_cue(bundles, record["leaf_topic"], ENTRY_CAP - len(prefix))
            curated_drift = prior_cue != expected_cue
        if generation == 0:
            cue_preimage = current_cue_digest
            cue_owner = "managed" if start < 0 or takeover_requested else "curated"
        elif start >= 0:
            cue_preimage = current_cue_digest
        if curated_drift and not takeover_requested:
            cue_owner = "curated"
        if cue_owner == "curated" and not takeover_requested:
            cue_owner = "curated"
            cue_bundles: list[dict[str, Any]] | None = None
        else:
            cue_owner = "managed"
            cue_bundles = bundles
        by_id = {item["record"]["record_id"]: item for item in bundles}
        bundle = {"provenance": provenance, "record": record}
        if record["record_id"] in by_id:
            existing = by_id[record["record_id"]]
            if existing["record"] != record:
                raise ValidationError(
                    "record_id is immutable; use a new id and an explicit transition for a revision"
                )
            return {
                "ok": True, "status": "noop", "leaf": rel,
                "generation": generation, "cue_digest": _sha(prior_index),
                "leaf_bytes_before": len(raw), "leaf_bytes_after": len(raw),
                "managed_bytes_after": len(raw) - len(outside), "bytes_added": 0,
            }
        prior_retractions = {
            item["record"]["target_record_id"]
            for item in bundles
            if item["record"]["kind"] == "correction"
            and item["record"]["status"] == "active"
        }
        if record["kind"] == "correction":
            target = by_id.get(record["target_record_id"])
            replacement = by_id.get(record["correction_record_id"])
            if target is None:
                raise ValidationError("correction target does not exist in the topical leaf")
            if replacement is None:
                raise ValidationError("correction record does not exist in the topical leaf")
            target_record = target["record"]
            replacement_record = replacement["record"]
            if (
                target_record["kind"] == "correction"
                or target_record["status"] != "active"
                or record["target_record_id"] in prior_retractions
            ):
                raise ValidationError("correction target must be an unretracted active record")
            if (
                replacement_record["kind"] == "correction"
                or replacement_record["status"] != "active"
                or replacement_record["claim_verification"] == "known_wrong"
                or record["correction_record_id"] in prior_retractions
            ):
                raise ValidationError("correction record must be active, safe, and unretracted")
        supersedes = record.get("supersedes_record_id")
        if supersedes and supersedes not in by_id:
            raise ValidationError("superseded record does not exist in the topical leaf")
        if supersedes:
            by_id[supersedes]["record"]["status"] = "superseded"
        correction_id = record.get("correction_record_id")
        if correction_id is not None and record["kind"] != "correction":
            correction = by_id.get(correction_id)
            if correction is None:
                raise ValidationError("correction record does not exist in the topical leaf")
            correction_record = correction["record"]
            if (
                correction_id == record["record_id"]
                or correction_record["status"] != "active"
                or correction_record["claim_verification"] == "known_wrong"
            ):
                raise ValidationError("correction record must be distinct, active, and safe")
        if len(bundles) >= MAX_RECORDS_PER_LEAF:
            raise ValidationError("managed record block reached its bounded record limit")
        bundles.append(bundle)
        new_generation = generation + 1
        new_leaf = _render_leaf(outside, bundles, new_generation, cue_owner, cue_preimage)
        managed_bytes = len(new_leaf) - len(outside)
        if managed_bytes > MAX_MANAGED_BLOCK_BYTES:
            raise ValidationError("managed record block exceeds the byte budget")
        if len(new_leaf) > MAX_LEAF_BYTES:
            raise ValidationError("managed leaf exceeds the byte budget")
        intended_index = prior_index if cue_bundles is None else _render_index(prior_index, rel, record["leaf_topic"], bundles)
        _write_marker(memory_dir, rel, generation, new_generation, new_leaf, intended_index)
        if crash_at == "before_leaf": raise InjectedCrash("before leaf replacement")
        _atomic_bytes(leaf, new_leaf)
        if crash_at == "after_leaf": raise InjectedCrash("after leaf replacement")
        if intended_index != prior_index: _atomic_bytes(index_path, intended_index)
        if crash_at == "after_index": raise InjectedCrash("after index replacement")
        _clear_marker(memory_dir)
        return {
            "ok": True, "status": "updated", "leaf": rel,
            "generation": new_generation, "cue_digest": _sha(intended_index),
            "leaf_bytes_before": len(raw), "leaf_bytes_after": len(new_leaf),
            "managed_bytes_after": managed_bytes,
            "bytes_added": len(new_leaf) - len(raw),
        }


def recover_if_dirty(memory_dir: Path, *, _lock_held: bool = False) -> dict[str, Any]:
    memory_dir = Path(memory_dir)
    marker = dirty_marker_path(memory_dir)
    if not marker.is_file(): return {"status": "clean", "reads": 0, "writes": 0}
    if not _lock_held:
        with _transaction_lock(memory_dir): return recover_if_dirty(memory_dir, _lock_held=True)
    try: data = json.loads(marker.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise RecoveryError("recovery marker is invalid") from exc
    if data.get("schema") != "topical-memory-transaction/v1" or data.get("state") != "dirty":
        raise RecoveryError("recovery marker shape is invalid")
    rel = _relative_identity(data.get("leaf_rel", ""))
    leaf = (memory_dir.resolve() / rel).resolve()
    if not leaf.is_relative_to(memory_dir.resolve()) or leaf.is_symlink():
        raise RecoveryError("recovery leaf identity is invalid")
    raw = _read_leaf(leaf)
    _, bundles, generation, _, _ = _parse_leaf(raw)
    if generation == int(data["prior_generation"]):
        _clear_marker(memory_dir); return {"status": "retired_uncommitted", "reads": 1, "writes": 1}
    if generation != int(data["new_generation"]):
        raise RecoveryError("leaf generation does not match recovery marker")
    intended_leaf_digest = data.get("intended_leaf_digest")
    if not isinstance(intended_leaf_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", intended_leaf_digest):
        raise RecoveryError("recovery marker leaf digest is invalid")
    if _sha(raw) != intended_leaf_digest:
        raise RecoveryError("committed leaf digest does not match recovery marker")
    topic = Path(rel).stem
    for prefix in ("project_", "feedback_", "reference_"):
        if topic.startswith(prefix): topic = topic[len(prefix):]
    index_path = memory_dir / "MEMORY.md"
    try:
        intended = _render_index(_read_leaf(index_path), rel, topic, bundles)
    except ValidationError as exc:
        raise RecoveryError("duplicate index ownership during recovery") from exc
    _atomic_bytes(index_path, intended)
    _clear_marker(memory_dir)
    return {"status": "reconciled", "reads": 2, "writes": 2}


def _managed_span(existing: str) -> tuple[int, int] | None:
    begin = existing.find(BEGIN_PREFIX)
    if begin < 0:
        return None
    end = existing.find(END_MARKER, begin)
    if end < 0:
        return (begin, len(existing))
    return (begin, end + len(END_MARKER))


def _structured_edits(tool_input: dict[str, Any]) -> list[tuple[str, str]] | None:
    rows: list[dict[str, Any]]
    nested = tool_input.get("edits") or tool_input.get("changes")
    if isinstance(nested, list):
        if not nested or any(not isinstance(row, dict) for row in nested):
            return None
        rows = nested
    else:
        rows = [tool_input]
    parsed: list[tuple[str, str]] = []
    for row in rows:
        old = row.get("old_string", row.get("oldText", row.get("old_text")))
        new = row.get("new_string", row.get("newText", row.get("new_text")))
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return None
        parsed.append((old, new))
    return parsed


def direct_file_tool_targets_managed_block(
    path: Path | str, tool_input: dict[str, Any], *, operation: str = "edit",
) -> bool:
    """Classify visible file-tool mutations against the managed block.

    Edit ranges are located from their exact preimage. Missing or repeated
    preimages are ambiguous and fail closed for an existing managed leaf.
    Outside-only, uniquely located edits remain available for operator prose.
    """
    try:
        target = Path(path)
        if target.suffix.casefold() != ".md":
            return False
        existing = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except (OSError, TypeError, ValueError):
        existing = ""
    operation = operation.lower()
    span = _managed_span(existing)
    serialized_input = "\n".join(_flatten_strings(tool_input))
    contains_marker = BEGIN_PREFIX in serialized_input or END_MARKER in serialized_input
    if contains_marker:
        return True
    marker_counts = (existing.count(BEGIN_PREFIX), existing.count(END_MARKER))
    if marker_counts not in {(0, 0), (1, 1)}:
        return True
    if operation in {"write", "patch", "apply_patch"}:
        return span is not None
    if span is None:
        return False
    if operation not in {"edit", "multiedit"}:
        return True
    edits = _structured_edits(tool_input)
    if edits is None:
        return True
    for old, _ in edits:
        starts = [match.start() for match in re.finditer(re.escape(old), existing)]
        if len(starts) != 1:
            return True
        start = starts[0]
        end = start + len(old)
        if start < span[1] and end > span[0]:
            return True
    return False


def direct_write_targets_managed_block(
    path: Path | str, content: str, *, operation: str = "edit",
) -> bool:
    """Backward-compatible scalar adapter for older local callers."""
    if operation.lower() == "edit":
        tool_input = {"old_string": content, "new_string": content}
    else:
        tool_input = {"content": content}
    return direct_file_tool_targets_managed_block(path, tool_input, operation=operation)
