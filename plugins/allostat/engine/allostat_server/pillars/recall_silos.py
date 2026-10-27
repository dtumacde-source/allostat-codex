"""Recall-silos pillar — fingerprint match + retrieval from silo entries.

The recall-silos pillar matches a query fingerprint against silo entries
the wrapper composed from the operator's local `.allostat/silos/<class>.jsonl`
files. Server does the match + ranking; wrapper provides the data.

Pure model: the wrapper sends ONLY the entries that might be relevant
(usually filtered by class_name already), and the server returns the
subset that fingerprint-matches the query. Non-matching entries are
discarded server-side per request — nothing persists.

Tool exposed by δ:

    recall_silos_query(excerpt, question) → PillarToolResponse

  question.question_type includes "query_silo", "compute_fingerprint_hash",
  and "purge_silo".

  For "query_silo", question.details contains:
    class_name:  str — the signal class (must match entry.fingerprint.class_name)
    key_tokens:  list[str] — query identity tokens (hashed against entry tokens)
    top_n:       int (default 5) — max entries to return

  excerpt.retrievals: list[dict] — the wrapper-composed silo entries in
                                   the SiloEntry JSONL shape (each dict has
                                   `fingerprint`, `resolution`,
                                   `success_marker`, `timestamp`,
                                   `session_id`, `fire_count`, `last_fire`,
                                   `decayed`).

Decision logic is a faithful port of plugin/lib/silo_base.py::query_silo's
match + rank (fire_count desc + last_fire desc, skip decayed).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .._time import utc_now_iso as _utc_iso_now
from ..pillar_protocol import (
    ClientStateWrite,
    DecisionPayload,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "recall-silos"
DEFAULT_TOP_N = 5


# ---------- Decision helpers (pure functions, no I/O) ----------


def compute_fingerprint_hash(class_name: str, key_tokens: list[str]) -> str:
    """SiloFingerprint identity hash.

    Mirrors plugin/lib/silo_base.py::SiloFingerprint.to_hash:
      sha256("class_name::sorted(key_tokens) joined by :") truncated to 16 chars.

    Token order doesn't affect identity (sorted before joining).
    """
    seed = f"{class_name}::{':'.join(sorted(key_tokens))}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def compute_class_tokens(class_name: str, fields: dict[str, Any]) -> list[str]:
    """Construct the canonical key_tokens for a per-class silo fingerprint.

    Mirrors plugin/lib/silos/<class>.py::_to_silo_fp for each of the five
    canonical signal classes. Wrapper calls this question_type when it has
    raw fingerprint inputs (e.g., a drift claim + source) and needs the
    canonical token list to query the silo.

    Sources (drift between server + plugin must be zero until θ moves
    these constructors to MCP resources):
      drift                ← plugin/lib/silos/drift.py::_to_silo_fp
      voice                ← plugin/lib/silos/voice.py::_to_silo_fp
      customization        ← plugin/lib/silos/customization.py::_to_silo_fp
      workflow             ← plugin/lib/silos/workflow.py::_to_silo_fp
      confidence_recovery  ← plugin/lib/silos/confidence_recovery.py::_to_silo_fp

    Raises ValueError on unknown class_name. Returns [] on missing
    required fields for the named class.
    """
    if class_name == "drift":
        claim = str(fields.get("claim", ""))
        claimed_in = str(fields.get("claimed_in", ""))
        if not claim:
            return []
        return [claim[:80].lower(), claimed_in[:40]]
    if class_name == "voice":
        violation_type = str(fields.get("violation_type", ""))
        sample_text = str(fields.get("sample_text", ""))
        if not violation_type:
            return []
        return [violation_type, sample_text[:60].lower()]
    if class_name == "customization":
        decision_class = str(fields.get("decision_class", ""))
        options = fields.get("options_seen") or []
        if not decision_class or not isinstance(options, list):
            return []
        return [decision_class, *sorted(str(o) for o in options)]
    if class_name == "workflow":
        workflow_file = str(fields.get("workflow_file", ""))
        phase = str(fields.get("phase", ""))
        gotcha = str(fields.get("gotcha", ""))
        if not workflow_file:
            return []
        return [workflow_file, phase, gotcha[:60].lower()]
    if class_name == "confidence_recovery":
        topic = str(fields.get("topic", ""))
        starting_confidence = fields.get("starting_confidence")
        if not topic or starting_confidence is None:
            return []
        try:
            rounded = round(float(starting_confidence), 1)
        except (TypeError, ValueError):
            return []
        return [topic[:60].lower(), f"start_{rounded}"]
    raise ValueError(f"unknown silo class: {class_name!r}")


def compose_drift_silo_entry(
    prompt: str,
    session_id: str | None,
    *,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Compose a SiloEntry-shaped dict for the drift silo from an operator
    correction prompt. PATCH-194 — Phase 3.4b drift inscribe.

    The dispatcher detects operator corrections via `_is_operator_correction`
    and emits a `ClientStateWrite(target="silo_entry.jsonl", ...)` carrying
    this dict. The wrapper's `client_state_writer` routes by `silo_class` to
    `<state_dir>/silos/drift.jsonl`.

    Token construction MUST mirror `compute_class_tokens("drift")` byte-for-
    byte: `[claim[:80].lower(), claimed_in[:40]]`. Wrapper-side
    `lib/silos/drift.py::_to_silo_fp` uses the same formula. Drift between
    server + wrapper here would break recall.

    Heuristic mapping for the inscribe (PATCH-194 first soak):
      claim            = prompt[:80] (the contested phrasing — first words
                         of the correction)
      claimed_in       = "response_text" (corrections nearly always target
                         the prior assistant turn)
      canonical_source = f"operator_correction:{session_id or 'unknown'}"

    PHASE-2 PRIVACY FIX (2026-05-28). The full operator prompt is NEVER
    included in the returned entry. Earlier shape carried
    `resolution.canonical_value = prompt`, which then crossed the wire
    on every subsequent dispatch via `excerpt.retrievals`. The new
    contract: the entry on the wire carries fingerprint hash + provenance
    only. The wrapper binds `fingerprint_hash -> canonical_value` in a
    LOCAL file (`<state_dir>/silos/canonical_resolutions.jsonl`) at
    inscribe-application time, using its own copy of the prompt it just
    dispatched. The server never sees canonical text after the initial
    dispatch payload (which is unavoidable — corrections detection
    requires the prompt).

    Also dropped `fingerprint.raw_excerpt` (was first 200 chars of the
    prompt). That field never participated in fingerprint hashing — it
    was orientation metadata only — and was leaking operator content via
    excerpt.retrievals on every dispatch. Provenance for fingerprint
    construction lives in the `key_tokens` (already hash-input) and the
    wrapper-local canonical_resolutions lookup.

    Returned shape matches `plugin/lib/silo_base.py::SiloEntry.to_jsonl` so
    the wrapper just appends the dict as a JSONL line. Plus a top-level
    `silo_class` field that the wrapper's `_resolve_target_path` reads to
    route to the per-class silo file.
    """
    claim = prompt[:80]
    claimed_in = "response_text"
    ts = now_iso or _utc_iso_now()
    return {
        "silo_class": "drift",
        "fingerprint": {
            "class_name": "drift",
            "key_tokens": [claim.lower(), claimed_in[:40]],
        },
        "resolution": {
            "canonical_source": f"operator_correction:{session_id or 'unknown'}",
        },
        "success_marker": "operator_correction",
        "timestamp": ts,
        "session_id": session_id or "",
        "fire_count": 1,
        "last_fire": ts,
        "decayed": False,
    }


# Per-class success markers. Mirror the wrapper's record_* functions
# (plugin/lib/silos/<class>.py) so a server-dispatched inscribe and a
# wrapper-direct inscribe produce entries with the same success_marker.
#   customization        ← record_customization_decision: "decision_recorded"
#   workflow             ← record_workflow_gotcha:        "gotcha_recorded"
#   confidence_recovery  ← record_confidence_recovery:    "confidence_recovered"
#   voice                ← record_voice_detection:        "voice_detected"
# drift keeps its own marker ("operator_correction") via compose_drift_silo_entry.
#
# voice was "voice_corrected" until 2026-08-07. A dispatched inscribe records
# that a voice case was DETECTED — it carries no evidence that anything was
# surfaced to the operator or agreed by them, so it may not claim a correction.
# The wrapper's `silos/voice.py` makes the same distinction, and the corrected
# marker is reachable there only through a function that demands the evidence.
_SUCCESS_MARKERS: dict[str, str] = {
    "customization": "decision_recorded",
    "workflow": "gotcha_recorded",
    "confidence_recovery": "confidence_recovered",
    "voice": "voice_detected",
    "drift": "operator_correction",
}


def compose_silo_entry(
    class_name: str,
    fields: dict[str, Any],
    *,
    resolution: dict[str, Any] | None = None,
    session_id: str | None = None,
    now_iso: str | None = None,
) -> dict[str, Any] | None:
    """Compose a wire-shape SiloEntry dict for any non-drift silo class.

    Pillar-wiring 2026-06-05 — closes the inscribe gap for customization /
    workflow / confidence_recovery (the three classes that had a `query_silo`
    path but no capture path). Generalizes `compose_drift_silo_entry`:

      - drift derives its fingerprint from a prompt heuristic
        (claim=prompt[:80]); it stays on its own composer.
      - the other classes carry STRUCTURED fingerprint fields, so this
        composer defers token construction to `compute_class_tokens`
        (the single source of truth for all 5 classes, already parity-pinned
        against the wrapper's per-class `_to_silo_fp` in
        test_pillar_recall_silos_parity.py). No per-class duplication here.

    Token construction MUST stay byte-identical with the wrapper's
    `lib/silos/<class>.py::_to_silo_fp` — that's what `compute_class_tokens`
    guarantees. Drift between the two breaks recall (the inscribed entry
    fingerprints differently from the query).

    Privacy invariant (design_invariants.md §1 + wrapper-mcp.md §6.1): the
    returned entry is WIRE-SHAPE. The structured `resolution` payload
    (chosen_option/rationale, canonical_fix/lesson, recovery_action/
    ending_confidence) is the operator's canonical content and NEVER appears
    in the entry — only a wire-safe `canonical_source` reference string does.
    The wrapper's `client_state_writer._bind_canonical_from_silo_content`
    relocates the payload to wrapper-local `canonical_resolutions.jsonl` at
    apply time. The `resolution` parameter is accepted so the dispatch site
    can hand the composer the operator's canonical fields, but the composer
    deliberately does NOT place them on the wire — it exists so the
    canonical-source provenance can be derived and so the signature documents
    what the wrapper will bind locally.

    Returns:
      - the entry dict on success,
      - None when `compute_class_tokens` yields no tokens (required fields
        missing for the class) — the caller treats this as "nothing to
        inscribe" rather than emit an empty-fingerprint entry that would
        collide across every under-specified inscribe.

    Raises ValueError on an unknown `class_name` (propagated from
    `compute_class_tokens`).
    """
    tokens = compute_class_tokens(class_name, fields or {})
    if not tokens:
        return None
    ts = now_iso or _utc_iso_now()
    return {
        "silo_class": class_name,
        "fingerprint": {
            "class_name": class_name,
            "key_tokens": tokens,
        },
        "resolution": {
            "canonical_source": f"operator_correction:{session_id or 'unknown'}",
        },
        "success_marker": _SUCCESS_MARKERS.get(class_name, "recorded"),
        "timestamp": ts,
        "session_id": session_id or "",
        "fire_count": 1,
        "last_fire": ts,
        "decayed": False,
    }


def _entry_hash(entry: dict[str, Any]) -> str | None:
    """Compute the fingerprint hash for an excerpt entry.

    Expected entry shape (matches plugin/lib/silo_base.py::SiloEntry.to_jsonl):
      {
        "fingerprint": {"class_name": str, "key_tokens": [str], ...},
        "resolution": dict, "success_marker": str, "timestamp": str,
        "session_id": str, "fire_count": int, "last_fire": str, "decayed": bool,
      }

    Returns None if the entry doesn't carry the fingerprint shape.
    """
    fp = entry.get("fingerprint")
    if not isinstance(fp, dict):
        return None
    # ultraswarm H-7/H-8 (2026-07-07): the wrapper now ships `token_hash` on the
    # wire instead of raw `key_tokens` (which leaked operator content). Prefer
    # the supplied hash. Fall back to recomputing from key_tokens for older
    # wrappers and for any locally-shaped entry that still carries tokens.
    token_hash = fp.get("token_hash")
    if isinstance(token_hash, str) and token_hash:
        return token_hash
    class_name = fp.get("class_name")
    tokens = fp.get("key_tokens", [])
    if not class_name or not isinstance(tokens, list):
        return None
    return compute_fingerprint_hash(str(class_name), [str(t) for t in tokens])


# C3 (Wave 3): canonical_source is the wire-permitted reference identifier (Phase-2
# privacy strip relocates every other resolution field). It is FREE-FORM by design
# — "operator_correction:<id>", "STATE.md §2", "cs", etc. — so it can't be format-
# coerced without corrupting real references (PATCH-167 roundtrip). It is instead
# STRUCTURALLY bounded like the other echoed free-form fields: no newlines/control
# chars (no multi-line prompt-injection block), whitespace collapsed, length-capped.
_CANONICAL_SOURCE_MAX = 128

# C3 residual (Wave-3 follow-up): the echo boundary must whitelist-project the
# entry's TOP-LEVEL keys, not just sanitize a few known ones — mirroring the
# wrapper's own H-01 fix (silo_base.strip_entry_for_wire, which inverted
# `dict(entry)` to a strict whitelist). This is every top-level key the pillar
# legitimately reads for the body ("First: ...") and echoes under
# `details.matches`: the SiloEntry.to_jsonl shape + the sanitized display fields
# + the server-added fingerprint_hash. Any other key a compromised / hand-edited
# wrapper puts on the entry is dropped here instead of riding into recall context.
# Superset of what downstream reads, so a well-formed entry loses nothing.
_ECHO_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    # SiloEntry.to_jsonl standard shape
    "fingerprint", "resolution", "success_marker", "timestamp",
    "session_id", "fire_count", "last_fire", "decayed", "silo_class",
    # display fields read for the body + echoed in matches (sanitized above)
    "summary", "title", "canonical_source",
    # server-added: the wrapper re-joins canonical text locally by this hash
    "fingerprint_hash",
})


def _safe_text(value: Any, max_len: int = 200) -> str:
    """Structurally bound a wrapper-supplied DISPLAY field (summary/title) before
    it is echoed into agent-visible output. These fields are shown to the agent by
    design, so the CONTENT is preserved; only the STRUCTURE is bounded: non-strings
    → '', control chars and newlines removed (no multi-line prompt-injection block),
    whitespace collapsed, length-capped."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def _with_fingerprint_hash(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of `entry` with `fingerprint_hash` added.

    W5 (pillar-wiring 2026-06-05) retrieve enabler. The wrapper joins each
    recall match to its human-readable canonical text via
    `canonical_resolutions.lookup_by_hash(fingerprint_hash)`. We compute the
    hash from the entry's own fingerprint (class_name + key_tokens) and
    expose it at the top level so the wrapper doesn't recompute it.

    Shallow copy keeps the server side-effect-free on the wrapper-supplied
    `excerpt.retrievals` objects. Entries without a parseable fingerprint
    get `fingerprint_hash=None` (still copied, never mutated in place).
    """
    out = dict(entry)
    out["fingerprint_hash"] = _entry_hash(entry)
    # C3 (Wave 3): sanitize wrapper-supplied free-form fields before they are
    # echoed into agent-visible output (details.matches + the "First: ..." body
    # line). On real/wire entries canonical_source is NESTED under resolution;
    # summary/title feed the body. Copy nested containers so the wrapper-supplied
    # input is never mutated (server side-effect-free).
    if isinstance(out.get("resolution"), dict):
        # Wire contract: only canonical_source is permitted in resolution across
        # the wire (silo_base.RESOLUTION_WIRE_KEYS). Project the ECHOED resolution
        # to that single sanitized key so a compromised wrapper can't leak other
        # operator content (e.g. canonical_value) back through details.matches —
        # the wrapper re-joins canonical text locally by fingerprint_hash and needs
        # nothing else here.
        if "canonical_source" in out["resolution"]:
            out["resolution"] = {
                "canonical_source": _safe_text(
                    out["resolution"].get("canonical_source"), max_len=_CANONICAL_SOURCE_MAX)
            }
        else:
            out["resolution"] = {}
    if "canonical_source" in out:  # defensive: a top-level copy too, if present
        out["canonical_source"] = _safe_text(out.get("canonical_source"), max_len=_CANONICAL_SOURCE_MAX)
    for _k in ("summary", "title"):
        if _k in out:
            out[_k] = _safe_text(out.get(_k))
    # C3 residual: drop any top-level key outside the known echoed match shape,
    # so a compromised wrapper can't smuggle arbitrary fields into details.matches.
    return {k: v for k, v in out.items() if k in _ECHO_ALLOWED_TOP_LEVEL_KEYS}


def query_entries(
    entries: list[dict[str, Any]],
    class_name: str,
    key_tokens: list[str],
    top_n: int = DEFAULT_TOP_N,
) -> list[dict[str, Any]]:
    """Filter + rank entries that fingerprint-match the query.

    Match: entry's fingerprint hash equals query hash (class + sorted tokens).
    Skip: entries with decayed=True.
    Rank: fire_count desc, then last_fire desc (then timestamp desc).
    Cap: top_n.

    Mirrors plugin/lib/silo_base.py::query_silo's match + rank, but operates
    on the wrapper-supplied entries list rather than reading the silo JSONL.
    """
    target_hash = compute_fingerprint_hash(class_name, key_tokens)
    matches: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("decayed"):
            continue
        h = _entry_hash(entry)
        if h is None or h != target_hash:
            continue
        matches.append(entry)

    # ISO-8601 strings compare lexically — newer sorts larger. Tuple sort
    # with reverse=True puts highest fire_count first, then most-recent
    # last_fire as tiebreaker. Mirrors plugin/lib/silo_base.py::query_silo's
    # ranking.
    def _safe_count(e: dict[str, Any]) -> int:
        # A corrupt/non-numeric fire_count in ONE entry must not crash the
        # whole query (deep-audit 2026-07-02); it just ranks last.
        try:
            return int(e.get("fire_count", 0) or 0)
        except (ValueError, TypeError):
            return 0

    matches.sort(
        key=lambda e: (
            _safe_count(e),
            str(e.get("last_fire") or e.get("timestamp", "")),
        ),
        reverse=True,
    )
    return matches[: max(0, int(top_n))]


# ---------- MCP tool ----------


def recall_silos_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below."""
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        if question.question_type == "compute_fingerprint_hash":
            return _hash_response(question)
        if question.question_type == "compute_class_tokens":
            return _class_tokens_response(question)
        if question.question_type == "inscribe_drift":
            return _inscribe_drift_response(question)
        if question.question_type == "inscribe_silo":
            return _inscribe_silo_response(question)
        if question.question_type == "purge_silo":
            return _purge_silo_response(question)
        if question.question_type != "query_silo":
            return _no_op(
                decision_class="unknown_question_type",
                reason=f"unknown question_type {question.question_type!r}",
            )

        class_name = question.details.get("class_name")
        key_tokens = question.details.get("key_tokens")
        if not isinstance(class_name, str) or not class_name:
            return _no_op(
                decision_class="missing_class_name",
                reason="question.details.class_name (str) required",
            )
        if not isinstance(key_tokens, list):
            return _no_op(
                decision_class="missing_key_tokens",
                reason="question.details.key_tokens (list[str]) required",
            )
        top_n_raw = question.details.get("top_n", DEFAULT_TOP_N)
        try:
            top_n = int(top_n_raw)
        except (TypeError, ValueError):
            top_n = DEFAULT_TOP_N

        results = query_entries(
            excerpt.retrievals,
            class_name=class_name,
            key_tokens=[str(t) for t in key_tokens],
            top_n=top_n,
        )
        if not results:
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="no_match",
                    details={
                        "class_name": class_name,
                        "matches": [],
                        "candidates_seen": len(excerpt.retrievals),
                    },
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class="no_match",
                    fingerprint=compute_fingerprint_hash(class_name, [str(t) for t in key_tokens]),
                ),
            )
        # W5 (pillar-wiring 2026-06-05) — canonical-text join enabler.
        # Each matched entry is wire-shape (fingerprint hash + provenance
        # only); the human-readable canonical text lives wrapper-local in
        # canonical_resolutions.jsonl keyed by fingerprint_hash. The SERVER
        # cannot read that file (privacy moat, design_invariants.md §1), so
        # the join is wrapper-side: lookup_by_hash(fingerprint_hash). We
        # surface the exact per-match hash here so the wrapper has the lookup
        # key without recomputing it from key_tokens. Done on a SHALLOW COPY
        # so the wrapper-supplied entry objects in excerpt.retrievals are not
        # mutated (server is RAM-only + side-effect-free on inputs).
        results = [_with_fingerprint_hash(e) for e in results]
        first = results[0] if results else {}
        first_summary = str(first.get("summary") or first.get("title") or "")[:80]
        return PillarToolResponse(
            decision=DecisionPayload(
                action="recall_match",
                body=(
                    f"Recall: {len(results)} prior case(s) in `{class_name}` "
                    f"silo. First: \"{first_summary}\"."
                    if first_summary else
                    f"Recall: {len(results)} prior case(s) in `{class_name}` silo."
                ),
                details={
                    "class_name": class_name,
                    "matches": results,
                    "match_count": len(results),
                },
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="matched",
                fingerprint=compute_fingerprint_hash(class_name, [str(t) for t in key_tokens]),
            ),
        )
    finally:
        current_pillar_id.reset(token)


def _hash_response(question: QuestionPayload) -> PillarToolResponse:
    class_name = question.details.get("class_name", "")
    key_tokens = question.details.get("key_tokens", [])
    if not isinstance(key_tokens, list):
        key_tokens = []
    h = compute_fingerprint_hash(str(class_name), [str(t) for t in key_tokens])
    return PillarToolResponse(
        decision=DecisionPayload(
            action="fingerprint_hash",
            details={"hash": h, "class_name": class_name, "sorted_tokens": sorted([str(t) for t in key_tokens])},
        ),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="hashed", fingerprint=h),
    )


def _class_tokens_response(question: QuestionPayload) -> PillarToolResponse:
    details = question.details or {}
    class_name = details.get("class_name")
    fields = details.get("fields", {}) or {}
    if not isinstance(class_name, str) or not class_name:
        return _no_op(
            decision_class="missing_class_name",
            reason="question.details.class_name (str) required",
        )
    if not isinstance(fields, dict):
        return _no_op(
            decision_class="invalid_fields",
            reason="question.details.fields (dict) required",
        )
    try:
        tokens = compute_class_tokens(class_name, fields)
    except ValueError as e:
        return _no_op(
            decision_class="unknown_silo_class",
            reason=str(e),
        )
    if not tokens:
        return PillarToolResponse(
            decision=DecisionPayload(
                action="no_op",
                details={
                    "class_name": class_name,
                    "tokens": [],
                    "reason": "missing required fields for this class",
                },
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="missing_class_fields",
            ),
        )
    h = compute_fingerprint_hash(class_name, tokens)
    return PillarToolResponse(
        decision=DecisionPayload(
            action="class_fingerprint",
            details={
                "class_name": class_name,
                "tokens": tokens,
                "hash": h,
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="class_fingerprint_computed",
            fingerprint=h,
        ),
    )


def _inscribe_drift_response(question: QuestionPayload) -> PillarToolResponse:
    """Handle the `inscribe_drift` question_type.

    PATCH-194 (2026-05-23) — Phase 3.4b drift silo inscribe. Dispatcher
    invokes this when `_is_operator_correction(prompt)` fires; pillar
    composes a SiloEntry-shaped ClientStateWrite that the wrapper's
    `client_state_writer` routes to `<state_dir>/silos/drift.jsonl`.

    Inputs (question.details):
      prompt:     str — operator's correction prompt (required, non-empty)
      session_id: str | None — current session id for resolution provenance

    Returns DecisionPayload with `action="inscribe_drift"`, no body (silent
    inscribe — felt-presence belongs to the pattern-observer fingerprint
    that fires at the same dispatch site), and one ClientStateWrite.

    Dedup posture: the dispatcher fires this every correction. Multiple
    corrections of the same claim within a session produce multiple silo
    entries with the same fingerprint hash; recall ranks them by
    `fire_count desc, last_fire desc` so the most-recent + most-fired
    surfaces first. PATCH-149 cascade dedup passes empty-body decisions
    through unchanged so this never gets suppressed.
    """
    details = question.details or {}
    prompt = str(details.get("prompt") or "")
    if not prompt:
        return _no_op(
            decision_class="missing_prompt",
            reason="question.details.prompt (str) required for inscribe_drift",
        )
    session_id_raw = details.get("session_id")
    session_id = str(session_id_raw) if session_id_raw else None
    entry = compose_drift_silo_entry(prompt, session_id)
    fingerprint_hash = compute_fingerprint_hash(
        "drift", entry["fingerprint"]["key_tokens"]
    )
    write = ClientStateWrite(
        target="silo_entry.jsonl",
        mode="append",
        content=entry,
        pillar_id=PILLAR_ID,
    )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="inscribe_drift",
            client_state_writes=[write],
            details={
                "fingerprint_hash": fingerprint_hash,
                "silo_class": "drift",
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="inscribed",
            fingerprint=fingerprint_hash,
        ),
    )


def _inscribe_silo_response(question: QuestionPayload) -> PillarToolResponse:
    """Handle the `inscribe_silo` question_type (pillar-wiring 2026-06-05).

    Generic capture path for customization / workflow / confidence_recovery —
    the three silo classes that had a `query_silo` path but no inscribe path.
    Mirrors `_inscribe_drift_response` but is class-parameterized: the
    dispatcher passes `class_name` + the structured `fields` + the operator's
    `resolution` payload, and this composes a wire-shape SiloEntry
    ClientStateWrite that the wrapper's `client_state_writer` routes to
    `<state_dir>/silos/<class_name>.jsonl` (generic `content.silo_class`
    routing — already in place for drift).

    Why one generic handler instead of three `inscribe_<class>` siblings:
    token construction for all five classes already lives in
    `compute_class_tokens`; a per-class handler would just re-thread the same
    fields with no added value. drift keeps its dedicated handler only because
    its fingerprint is a prompt heuristic, not structured fields.

    Inputs (question.details):
      class_name: str  — one of customization / workflow / confidence_recovery
                         (any class compute_class_tokens knows; drift should
                         route through inscribe_drift instead).
      fields:     dict — the structured fingerprint inputs for the class
                         (e.g. {"decision_class","options_seen"} for
                         customization). MUST match the wrapper's
                         <class>Fingerprint field names.
      resolution: dict — the operator's canonical payload (e.g.
                         {"chosen_option","rationale"}). NOT placed on the
                         wire; the wrapper binds it to canonical_resolutions
                         locally. Accepted for signature symmetry + future
                         provenance use.
      session_id: str | None — provenance for canonical_source.

    Returns DecisionPayload action="inscribe_silo", body=None (silent
    inscribe — felt-presence is the dispatch site's pattern-observer / nudge,
    not the silo write), and one ClientStateWrite. no_op when class_name is
    missing/unknown, fields are the wrong type, or required fields are absent.
    """
    details = question.details or {}
    class_name = details.get("class_name")
    if not isinstance(class_name, str) or not class_name:
        return _no_op(
            decision_class="missing_class_name",
            reason="question.details.class_name (str) required for inscribe_silo",
        )
    fields = details.get("fields", {})
    if not isinstance(fields, dict):
        return _no_op(
            decision_class="invalid_fields",
            reason="question.details.fields (dict) required",
        )
    resolution = details.get("resolution")
    if resolution is not None and not isinstance(resolution, dict):
        resolution = None
    session_id_raw = details.get("session_id")
    session_id = str(session_id_raw) if session_id_raw else None

    try:
        entry = compose_silo_entry(
            class_name,
            fields,
            resolution=resolution,
            session_id=session_id,
        )
    except ValueError as e:
        return _no_op(decision_class="unknown_silo_class", reason=str(e))

    if entry is None:
        return _no_op(
            decision_class="missing_class_fields",
            reason=f"missing required fields for silo class {class_name!r}",
        )

    fingerprint_hash = compute_fingerprint_hash(
        class_name, entry["fingerprint"]["key_tokens"]
    )
    write = ClientStateWrite(
        target="silo_entry.jsonl",
        mode="append",
        content=entry,
        pillar_id=PILLAR_ID,
    )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="inscribe_silo",
            client_state_writes=[write],
            details={
                "fingerprint_hash": fingerprint_hash,
                "silo_class": class_name,
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="inscribed",
            fingerprint=fingerprint_hash,
        ),
    )


def _purge_silo_response(question: QuestionPayload) -> PillarToolResponse:
    """Retraction, server axis (0.2.9) — instruct the client to purge a silo.

    Counterpart to the wrapper's `retraction` module. "Forget X" used to delete
    the memory leaf, report success, and leave verbatim copies in the
    observation log and the audit files; the wrapper now purges every store it
    can reach, and this is how the silo written through the SERVER's dispatch
    path gets reached too.

    Deliberately a ClientStateWrite, not a server-side delete: silo content is
    operator-local by the privacy moat — the server has never held it and must
    not start now in order to remove it. The server's role is to authorize and
    instruct; the bytes only ever exist on the client.

    question.details:
      silo_class: str  — the class to purge from ("voice", "drift", ...)
      pattern:    str  — the literal text to remove (matched client-side)

    The response carries `purge_verified: false` — the SERVER cannot verify a
    client-side purge, and saying so is the entire point of this change. The
    wrapper re-scans and reports the truth.
    """
    details = question.details or {}
    silo_class = details.get("silo_class")
    pattern = details.get("pattern")
    regex_mode = details.get("regex", False)
    if not isinstance(silo_class, str) or not silo_class:
        return _no_op(
            decision_class="missing_class_name",
            reason="question.details.silo_class (str) required for purge_silo",
        )
    if not all(c.isalnum() or c in ("_", "-") for c in silo_class):
        return _no_op(
            decision_class="invalid_class_name",
            reason="silo_class must be a simple identifier",
        )
    if not isinstance(pattern, str) or not pattern.strip():
        return _no_op(
            decision_class="missing_pattern",
            reason="question.details.pattern (non-empty str) required for purge_silo",
        )
    if not isinstance(regex_mode, bool):
        return _no_op(
            decision_class="invalid_regex_mode",
            reason="question.details.regex must be a boolean",
        )
    if regex_mode:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            return _no_op(
                decision_class="invalid_purge_regex",
                reason="question.details.pattern is not a valid regex",
            )
        # The server cannot see the operator's local silo corpus. Refuse every
        # regex whose parsed minimum width is zero; the client additionally
        # checks actual target bytes before the request and again before apply.
        if re._parser.parse(pattern, re.IGNORECASE).getwidth()[0] == 0:
            return _no_op(
                decision_class="zero_width_purge_regex",
                reason="zero-width purge regexes are not allowed",
            )

    write = ClientStateWrite(
        target="silo_entry.jsonl",
        mode="purge",
        content={"silo_class": silo_class, "pattern": pattern, "regex": regex_mode},
        pillar_id=PILLAR_ID,
    )
    return PillarToolResponse(
        decision=DecisionPayload(
            action="purge_silo",
            client_state_writes=[write],
            details={
                "silo_class": silo_class,
                # Never true from here. The client owns the bytes and the
                # client verifies; a server claiming a purge it cannot see is
                # the same false-success this work exists to remove.
                "purge_verified": False,
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="purge_instructed",
        ),
    )


def _no_op(*, decision_class: str, reason: str | None = None) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="recall_silos_query",
        description=(
            "Recall-silos pillar — fingerprint match + retrieval + inscribe. "
            "question_type=query_silo: filter wrapper-supplied entries by "
            "fingerprint match, rank by fire_count desc + recency, skip "
            "decayed. "
            "question_type=compute_fingerprint_hash: return the canonical "
            "hash for (class_name, key_tokens). "
            "question_type=compute_class_tokens: construct canonical "
            "key_tokens for a per-class silo (drift, voice, customization, "
            "workflow, confidence_recovery) from raw fields. "
            "question_type=inscribe_drift (PATCH-194): compose a "
            "SiloEntry-shaped ClientStateWrite for the drift silo from "
            "an operator-correction prompt; wrapper applies the append. "
            "question_type=inscribe_silo (pillar-wiring 2026-06-05): compose "
            "a SiloEntry-shaped ClientStateWrite for customization / workflow "
            "/ confidence_recovery from structured fields + a resolution "
            "payload; wire-shape (fingerprint hash + provenance only), "
            "wrapper binds canonical text locally + routes by content.silo_class. "
            "question_type=purge_silo: authorize a literal or regex client-side "
            "silo purge; the wrapper applies and verifies the returned write."
        ),
    )
    def recall_silos_query(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return recall_silos_decision(excerpt, question)
