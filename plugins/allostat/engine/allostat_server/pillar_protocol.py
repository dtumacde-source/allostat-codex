"""Pillar-tool input/output schemas (β — per-call context protocol).

Every pillar MCP tool on the hosted server follows the same shape:

    @server.tool(name="<pillar>_evaluate")
    def stress_response_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        ...

The wrapper plugin (Track B / η) composes the `excerpt` from the operator's
local `.allostat/` tree on each call, including ONLY the slice relevant to
the specific question. The server NEVER receives the full memory tree;
this is the load-bearing data-minimization that the privacy-as-moat
marketing claim depends on.

The server returns a `PillarToolResponse` envelope with `decision` (the
pillar's recommendation, applied by the wrapper) and `stats` (telemetry
the wrapper logs LOCALLY — pillar_id surfaces in the server's request_log
row but no decision content does).

Models are intentionally permissive at β. γ and later pillars extend
these with per-pillar subtypes (e.g., StressResponseQuestion). Keeping
the base loose avoids over-constraining the protocol before the pillar
implementations confirm their actual shapes.

Privacy guardrail: NEVER add a field to ExcerptPayload, QuestionPayload,
or any subtype that holds full prompt content, response content, or
unredacted memory file content. The wrapper's job is to send EXCERPTS,
not contents. If a future pillar requires the full message, that's a
signal the pillar needs to live client-side, not on the server.
"""
from __future__ import annotations

import contextvars
from typing import Any, Literal

from pydantic import BaseModel, Field


# Pillar-ID ContextVar — pillar tools set this on entry so the request
# logging middleware (logging.RequestLoggingMiddleware) can persist
# `pillar_id` in the request_log row. None when a request doesn't dispatch
# to a pillar tool (e.g., health_check, or pre-MCP middleware failures).
#
# Mirrors the auth.current_auth pattern: set on the way IN, read on the
# way OUT during request finalization.
#
# NOTE (PATCH-121): the OUTER ASGI logging middleware can't reliably observe
# this ContextVar after a pillar's token+reset cycle completes. Pillars
# additionally call record_pillar_invocation(PILLAR_ID) which appends to
# pillar_ids_invoked — a mutable list shared via reference, robust against
# anyio task-group context isolation. logging.RequestLoggingMiddleware
# reads pillar_ids_invoked.get() to derive the request_log pillar_id field.
# current_pillar_id is preserved for backward compat with code that reads
# the "currently executing" pillar during request handling.
current_pillar_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "allostat_current_pillar_id", default=None
)


# Mutable list of pillar IDs invoked during the current request. Initialized
# to a fresh list by the outer logging middleware at request start; pillars
# APPEND their PILLAR_ID via record_pillar_invocation(). The list object is
# shared by reference across anyio task groups, so mutations made by pillars
# inside BaseHTTPMiddleware's task group ARE visible to the outer pure-ASGI
# logging middleware when it reads in the finally block.
#
# Multi-pillar requests (e.g., v0.2.0 dispatch_post_tool_use that invokes
# stress-response + pattern-observer) join the list with commas for the
# pillar_id column. Single-pillar requests record just the one id.
pillar_ids_invoked: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "allostat_pillar_ids_invoked", default=None
)


def record_pillar_invocation(pillar_id: str) -> None:
    """Append `pillar_id` to the per-request pillar invocation list.

    Pillars call this at entry (alongside the existing current_pillar_id
    token+reset pattern). The outer logging middleware reads the list in
    its finally block and persists the joined value as request_log.pillar_id.

    If no list is in context (call happens outside an HTTP request, e.g.,
    in unit tests calling pillar functions directly), this is a no-op so
    test code doesn't need to know about the list.

    Idempotent: duplicate appends are skipped so multi-question_type calls
    on the same pillar don't double-record.
    """
    ids = pillar_ids_invoked.get()
    if ids is None:
        return
    if pillar_id and pillar_id not in ids:
        ids.append(pillar_id)


def coerce_int(value: object, default: int = 0) -> int:
    """int() with a safe default — pillar `details` fields arrive from the
    wire and MUST NOT crash a pillar tool on a non-numeric value (deep-audit
    2026-07-02: watchdog/metabolism/hypothalamic evaluate crashed with
    ValueError/TypeError on malformed details). Bool is accepted as int.
    """
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            return int(float(value.strip()))
        return default
    except (ValueError, TypeError, OverflowError):
        return default


class ExcerptPayload(BaseModel):
    """Slice of operator state needed for one pillar evaluation.

    Composed by the wrapper plugin from local `.allostat/` files. Target
    ≤500 KB per call; the SDK enforces a 1 MB hard cap via nginx's
    client_max_body_size. NEVER include:
      - operator's literal prompt text (only the specific phrasing relevant
        to the question, if any, and only with the operator's knowledge)
      - LLM response content
      - filenames containing operator paths (paths leak project identity)
      - raw memory file contents — send excerpts, not contents

    Each field is a list of small dicts so the wrapper has flexibility
    in how it slices its local tree. Pillars consume these as plain dicts.
    """

    observations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Recent observation log entries relevant to the question",
    )
    corrections: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Recent operator-correction events",
    )
    retrievals: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Recent silo retrievals (excerpts only, no full content)",
    )
    pending_dusk_surface: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "v0.2.6 PATCH-128 A4 — pending dusk-phase surface entries the "
            "wrapper carried over from the previous session's Stop event. "
            "Each entry is a dict with `body` (str) and optionally `pillar` + "
            "`timestamp`. dispatch_session_start_logic surfaces these at the "
            "next session's dawn-phase, working around Claude Code's Stop "
            "hook silent-drop of additionalContext."
        ),
    )
    project_slug: str | None = Field(
        default=None,
        description="Project slug for the active session (e.g., 'allostat')",
    )


class QuestionPayload(BaseModel):
    """The specific evaluation the pillar should perform.

    `pillar` and `question_type` together address one of the pillar's
    decision points. `details` carries per-question parameters in an
    intentionally loose shape — each pillar in γ/δ/ε/ζ will document its
    accepted question_types.
    """

    pillar: str = Field(
        description=(
            "Pillar handling this question — e.g., 'stress-response', "
            "'hypothalamic-axis', 'innate-enforcer'"
        ),
    )
    question_type: str = Field(
        description=(
            "Specific evaluation kind — e.g., 'drift_check', "
            "'recurrence_match', 'innate_guardrail_check'"
        ),
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-question-type parameters",
    )


class ClientStateWrite(BaseModel):
    """One instruction telling the wrapper to persist server-computed state.

    v0.2.0 Option Z protocol. Pillars that want learning state to survive
    across sessions return one or more `ClientStateWrite` instructions in
    their `DecisionPayload`. The wrapper's `lib/client_state_writer.py`
    validates each write against a whitelist + per-target schema and applies
    it to the operator's local `<cwd>/.allostat/<target>` file.

    Privacy posture: server NEVER persists this content. The instruction is
    composed in RAM, returned to the wrapper, then released. Only the
    OPERATOR's local filesystem stores the state. The server is RAM-only.

    Whitelisted targets (enforced both server-side via the schema below and
    wrapper-side in the writer):
      nudge_history.jsonl          — emitted nudges, append-only audit
      epigenetic_signature.json    — learned operator preferences
      prediction_calibration.json  — pillar accuracy tracking
      chronic_stress_baseline.json — rolling baseline for cascade detection
      pending_proposals.jsonl      — pattern-observer proposals between sessions
      drift_seen.json              — stress drift-seen dedup state
      pillar_history.jsonl         — per-pillar decision history
      silo_entry.jsonl             — cross-class silo append

    `target` is the FILENAME (no path, no traversal). Wrapper resolves it
    relative to `<cwd>/.allostat/`. Server NEVER constructs paths.
    """

    target: Literal[
        "nudge_history.jsonl",
        "epigenetic_signature.json",
        "prediction_calibration.json",
        "chronic_stress_baseline.json",
        "pending_proposals.jsonl",
        "drift_seen.json",
        "pillar_history.jsonl",
        "silo_entry.jsonl",
        # v0.2.6 PATCH-128 A4 — dusk-phase content for next session's dawn
        # surface. Stop event drops additionalContext (per Claude Code hook
        # docs); persist instead, surface at next SessionStart.
        "pending_dusk_surface.jsonl",
    ] = Field(
        description="Whitelisted target filename inside <cwd>/.allostat/"
    )
    mode: Literal["append", "overwrite", "merge", "purge"] = Field(
        description=(
            "append: add a new JSONL line (jsonl targets); "
            "overwrite: replace the whole JSON file (json targets); "
            "merge: shallow-merge into existing JSON object (json targets); "
            "purge: remove JSONL rows matching content['pattern'] (retraction "
            "path, silo targets only — the client removes and verifies, since "
            "silo bytes are operator-local and the server never holds them)"
        ),
    )
    content: dict[str, Any] = Field(
        description=(
            "JSON-serializable payload. For append mode, becomes one line. "
            "For overwrite, becomes the whole file. For merge, top-level "
            "keys overwrite existing keys in the JSON object."
        ),
    )
    pillar_id: str = Field(
        description="Pillar that emitted this write (audit trail)",
    )


class DecisionPayload(BaseModel):
    """The pillar's recommendation, applied by the wrapper plugin."""

    action: str = Field(
        description=(
            "Action to take — e.g., 'emit_nudge', 'block_tool_call', "
            "'archive_files', 'no_op'"
        ),
    )
    body: str | None = Field(
        default=None,
        description=(
            "Action-specific text payload — e.g., the nudge body the "
            "wrapper injects via additionalContext"
        ),
    )
    confidence: float = Field(
        default=1.0,
        description="0.0-1.0 confidence in this decision",
        ge=0.0,
        le=1.0,
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-action additional fields (file lists, fingerprints, etc.)",
    )
    client_state_writes: list[ClientStateWrite] = Field(
        default_factory=list,
        description=(
            "v0.2.0 Option Z: writes the wrapper should persist on the "
            "operator's local filesystem. Empty for pillars that don't "
            "need cross-session persistence (most). The wrapper validates "
            "each write against its whitelist before applying."
        ),
    )


class StatsPayload(BaseModel):
    """Telemetry the wrapper logs locally and the server counts in aggregate.

    pillar_id surfaces in the server's request_log row (added at β as a
    standalone column on request_log). No other field here is persisted
    server-side at this time.
    """

    pillar_id: str = Field(
        description="Pillar identifier for fire-frequency counting",
    )
    decision_class: str = Field(
        description=(
            "High-level decision bucket for telemetry — e.g., 'fired', "
            "'suppressed', 'no_match'"
        ),
    )
    fingerprint: str | None = Field(
        default=None,
        description=(
            "Decision fingerprint for client-side dedup. Hash, not content."
        ),
    )


class PillarToolResponse(BaseModel):
    """Standard response envelope for any pillar MCP tool."""

    decision: DecisionPayload
    stats: StatsPayload


class DispatchResponse(BaseModel):
    """Aggregated response envelope for v0.2.0 dispatch_* hook tools.

    Each `dispatch_*` MCP tool wraps the wrapper's old per-hook classifier
    logic (now server-side) PLUS the invocation of every relevant pillar
    for the given hook event. This envelope aggregates the outputs:

      additional_context  — joined `body` text the wrapper renders to operator
      client_state_writes — union of all pillars' state-write instructions
      pillars_invoked     — telemetry: which pillars ran (also surfaces in
                            request_log.pillar_id via record_pillar_invocation)
      decisions           — per-pillar decision objects for forensic debug
                            (not normally rendered to operator)

    The wrapper hooks each call ONE dispatch tool per hook event — the
    server takes care of classifier dispatch + pillar invocation + the
    decision aggregation. This is the load-bearing simplification that
    eliminates the wrapper's classifier.py and the 1:120 dispatch gate.
    """

    additional_context: str = Field(
        default="",
        description="Joined nudge bodies (one per fired pillar) for additionalContext",
    )
    client_state_writes: list[ClientStateWrite] = Field(
        default_factory=list,
        description=(
            "Union of all pillars' state-write instructions. Wrapper "
            "validates each against its whitelist + per-target schema "
            "before applying to <cwd>/.allostat/<target>."
        ),
    )
    pillars_invoked: list[str] = Field(
        default_factory=list,
        description=(
            "Pillars that contributed to this dispatch. Also recorded via "
            "record_pillar_invocation so request_log.pillar_id captures the "
            "join."
        ),
    )
    decisions: list[DecisionPayload] = Field(
        default_factory=list,
        description="Per-pillar decisions for forensic debug (not rendered)",
    )
