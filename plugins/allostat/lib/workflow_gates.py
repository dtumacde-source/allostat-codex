"""Workflow phase-gate logic — rule innate-11 (workflow gate nudge).

Rule 11 treats decision gates declared in `workflow_*.md` files as
innate-tier hard pauses: work may not cross a phase boundary without
explicit operator approval of that phase's gate. Origin: an autonomous
predeployment session scaffolded many Phase deliverables of a workflow
without firing any of the workflow's own declared gates — the gates were
WRITTEN but never ENFORCED, forcing the operator to backfill review of
dozens of unchecked decision items.

Contract (rules/innate/11-workflow-gate-nudge.yaml):
  - detection_logic: hypothalamic-axis calls
    `surface_workflow_gates(operator_message, project_memory)`. When a
    workflow is in play and work tries to advance to a phase whose
    entry-gate hasn't been approved, the rule fires.
  - parser_strategy:
      * structured_table — `## Decision gates` / `## Decision gates index`
        sections (one gate per list row: `- Phase N: <criteria>`)
      * inline_mention  — `Decision gate at end of Phase N: <criteria>` prose
  - phase tracking (v1 coarse heuristics):
      * "phase N complete" / "phase N ready" / "moving to phase N" mark a
        boundary; the LEFT phase's gate must be approved first.
      * "approve <phase> gate" approves a gate.
      * exception phrase: "skip <phase> gate — <reason>".
  - surface_helpers: `format_gates_overview`, `format_gate_pause`.

Rule 11 is designated guidance-only, so its producer may not be wired into
the PreToolUse path — but this module is importable and fully tested to the
contract so the producer can be wired without further code archaeology.

Pure text logic — no filesystem, no network. Never raises on bad input.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


# ---------- data model ----------


@dataclass(frozen=True)
class Gate:
    """A single declared decision gate.

    - phase:    the phase identifier as a STRING (e.g. "1", "2", "-1"). Kept
                as a string so coarse identifiers ("setup") could be carried
                in a later revision without a type change.
    - criteria: the approval criteria text (what the operator must confirm).
    - source:   which parser produced it ("structured" | "inline"), for
                forensics.
    """

    phase: str
    criteria: str
    source: str = ""


@dataclass(frozen=True)
class WorkflowGateSurface:
    """Result of `surface_workflow_gates`.

    - workflow_in_play: a workflow file appears active (gates parsed OR the
                        message references a workflow).
    - gates:            all parsed gates (overview material).
    - should_pause:     work is crossing a phase boundary whose left-phase
                        gate is neither approved nor skipped.
    - blocking_gate:    the gate that must be approved (when should_pause).
    - skipped_gate:     a gate the operator explicitly skipped this turn.
    - skip_reason:      the reason text from the skip exception phrase.
    - approved_phases:  phases approved this turn.
    """

    workflow_in_play: bool
    gates: list[Gate]
    should_pause: bool
    blocking_gate: Gate | None = None
    skipped_gate: Gate | None = None
    skip_reason: str | None = None
    approved_phases: tuple[str, ...] = ()


# ---------- parsing ----------

# Structured: a "## Decision gates" (optionally "... index") heading, then
# list rows shaped `- Phase N: <criteria>` (also tolerant of `*` bullets and
# table-row `| Phase N | criteria |`).
_DECISION_GATES_HEADING_RE = re.compile(
    r"^#{1,6}\s*decision\s+gates(?:\s+index)?\s*$", re.IGNORECASE | re.MULTILINE
)
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)

# A gate row inside the Decision-gates section: bullet or table form.
_GATE_ROW_RE = re.compile(
    r"""^\s*
        (?:[-*]|\|)\s*           # bullet (- / *) or table pipe
        phase\s+(?P<phase>-?\d+)  # "Phase 1", "Phase -1"
        \s*[:|]\s*               # ":" (bullet) or "|" (table column sep)
        (?P<criteria>.+?)\s*\|?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Inline: "Decision gate at end of Phase N: <criteria>"
_INLINE_GATE_RE = re.compile(
    r"decision\s+gate\s+at\s+end\s+of\s+phase\s+(?P<phase>-?\d+)\s*:\s*(?P<criteria>.+)",
    re.IGNORECASE,
)

# A message/memory "looks like a workflow" if it mentions a workflow file,
# the word "workflow", a Decision-gates heading, or phase language.
_WORKFLOW_SIGNAL_RE = re.compile(
    r"workflow_[\w-]*\.md|workflow|decision\s+gate|phase\s+-?\d+",
    re.IGNORECASE,
)


def _structured_gates(text: str) -> list[Gate]:
    """Parse `## Decision gates` section rows."""
    m = _DECISION_GATES_HEADING_RE.search(text)
    if not m:
        return []
    # Section body runs from after the heading to the next heading (or EOF).
    start = m.end()
    next_heading = _ANY_HEADING_RE.search(text, start)
    end = next_heading.start() if next_heading else len(text)
    body = text[start:end]

    gates: list[Gate] = []
    for line in body.splitlines():
        rm = _GATE_ROW_RE.match(line)
        if not rm:
            continue
        criteria = rm.group("criteria").strip().rstrip("|").strip()
        if not criteria:
            continue
        gates.append(
            Gate(phase=rm.group("phase"), criteria=criteria, source="structured")
        )
    return gates


def _inline_gates(text: str) -> list[Gate]:
    """Parse inline `Decision gate at end of Phase N:` prose mentions."""
    gates: list[Gate] = []
    for gm in _INLINE_GATE_RE.finditer(text):
        criteria = gm.group("criteria").strip().rstrip(".").strip()
        gates.append(Gate(phase=gm.group("phase"), criteria=criteria, source="inline"))
    return gates


def parse_gates(text: str | None) -> list[Gate]:
    """Parse all decision gates from `text` using both strategies.

    Structured rows take precedence over inline mentions for the same phase
    (dedup keeps the FIRST gate seen per phase, structured first). Returns a
    list ordered by first appearance. Empty list on no gates / bad input.
    """
    if not text:
        return []
    combined = _structured_gates(text) + _inline_gates(text)
    seen: set[str] = set()
    out: list[Gate] = []
    for g in combined:
        if g.phase in seen:
            continue
        seen.add(g.phase)
        out.append(g)
    return out


# ---------- phase-transition heuristics ----------

# "moving to phase N" / "advance to phase N" / "advancing to phase N" /
# "start phase N" / "starting phase N" / "begin phase N" → target phase N.
_ADVANCE_TO_RE = re.compile(
    r"(?:moving\s+to|advanc\w*\s+to|on\s+to|proceed\w*\s+to|start\w*|begin\w*)\s+phase\s+(?P<phase>-?\d+)",
    re.IGNORECASE,
)
# "phase N complete" / "phase N ready" / "phase N done" / "finished phase N"
# → phase N is the LEFT phase at a boundary.
_PHASE_DONE_RE = re.compile(
    r"phase\s+(?P<phase>-?\d+)\s+(?:complete|completed|ready|done|finished)"
    r"|finish\w*\s+phase\s+(?P<phase2>-?\d+)",
    re.IGNORECASE,
)
# "approve phase N gate" / "approve the phase N gate".
_APPROVE_RE = re.compile(
    r"approv\w*\s+(?:the\s+)?phase\s+(?P<phase>-?\d+)\s+gate", re.IGNORECASE
)
# Exception phrase: "skip phase N gate — <reason>" (em dash, en dash, or
# hyphen accepted as the separator).
_SKIP_RE = re.compile(
    r"skip\s+phase\s+(?P<phase>-?\d+)\s+gate\s*[—–-]\s*(?P<reason>.+)",
    re.IGNORECASE,
)


def _int_or_none(s: str | None):
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _detect_left_phase(message: str) -> str | None:
    """Determine which phase is being LEFT (whose gate must be approved).

    Two signals:
      - "phase N complete/ready/done" → left phase is N directly.
      - "moving to phase M" with no explicit completion → left phase is M-1
        (you must clear the prior phase's gate to enter M).
    "complete/ready" wins when both appear. Beginning phase 1 (or any
    target with no positive prior phase) yields None — no prior gate.
    """
    dm = _PHASE_DONE_RE.search(message)
    if dm:
        phase = dm.group("phase") or dm.group("phase2")
        if phase is not None:
            return phase

    am = _ADVANCE_TO_RE.search(message)
    if am:
        target = _int_or_none(am.group("phase"))
        if target is not None:
            prior = target - 1
            # Entering phase 1 (or lower) has no positive prior gate.
            if prior >= 1:
                return str(prior)
    return None


# ---------- env gate ----------


def _gates_enabled() -> bool:
    """Honor ALLOSTAT_WORKFLOW_GATES. Defaults to enabled.

    Operator can set `0|false|no|off` to disable rule-11 surfacing for a
    session. Mirrors the rollout_detector env-gate idiom.
    """
    raw = os.environ.get("ALLOSTAT_WORKFLOW_GATES", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ---------- surface predicate ----------


def surface_workflow_gates(
    operator_message: str | None,
    project_memory: str | None = None,
) -> WorkflowGateSurface:
    """Rule-11 surface predicate the hypothalamic-axis skill calls.

    Inputs:
      - operator_message: the latest operator message (phase-transition +
        approval + skip language is parsed from here).
      - project_memory: text the active workflow_*.md / project memory
        contributes (gates are parsed from here, and from the message).

    Returns a WorkflowGateSurface. `should_pause` is True iff a workflow is
    in play AND the message advances across a phase boundary whose left-phase
    gate is neither approved (this turn) nor skipped (this turn).

    Never raises; on disable or bad input returns a no-pause surface.
    """
    if not _gates_enabled():
        return WorkflowGateSurface(
            workflow_in_play=False, gates=[], should_pause=False
        )

    msg = operator_message or ""
    mem = project_memory or ""
    corpus = (mem + "\n" + msg).strip()

    gates = parse_gates(corpus)
    workflow_in_play = bool(gates) or bool(_WORKFLOW_SIGNAL_RE.search(corpus))

    if not workflow_in_play:
        return WorkflowGateSurface(
            workflow_in_play=False, gates=gates, should_pause=False
        )

    gates_by_phase = {g.phase: g for g in gates}

    # Approvals this turn.
    approved_phases = tuple(m.group("phase") for m in _APPROVE_RE.finditer(msg))

    # Skip exception phrase this turn.
    skipped_gate: Gate | None = None
    skip_reason: str | None = None
    sm = _SKIP_RE.search(msg)
    if sm:
        sk_phase = sm.group("phase")
        skip_reason = sm.group("reason").strip()
        skipped_gate = gates_by_phase.get(sk_phase, Gate(phase=sk_phase, criteria=""))

    # Which phase is being left (whose gate must be cleared)?
    left_phase = _detect_left_phase(msg)

    should_pause = False
    blocking_gate: Gate | None = None
    if left_phase is not None:
        cleared = (
            left_phase in approved_phases
            or (skipped_gate is not None and skipped_gate.phase == left_phase)
        )
        if not cleared:
            # The blocking gate is the left phase's declared gate. If the
            # workflow declared one for that phase, use it. Otherwise, only
            # pause when the workflow declared gates for OTHER phases (i.e.
            # it's a gated workflow) — synthesize a bare gate so the boundary
            # still pauses. A doc with no gates at all → phase language is
            # treated as informal and does not pause.
            blocking_gate = gates_by_phase.get(left_phase)
            if blocking_gate is None and gates:
                blocking_gate = Gate(phase=left_phase, criteria="")
            should_pause = blocking_gate is not None

    return WorkflowGateSurface(
        workflow_in_play=True,
        gates=gates,
        should_pause=should_pause,
        blocking_gate=blocking_gate,
        skipped_gate=skipped_gate,
        skip_reason=skip_reason,
        approved_phases=approved_phases,
    )


# ---------- format helpers ----------


def format_gates_overview(gates: list[Gate]) -> str:
    """Render all declared gates as an at-a-glance overview block.

    Surfaced at the start of workflow-touching work so the operator sees the
    full set of decision points up front. Empty string when there are no
    gates.
    """
    if not gates:
        return ""
    lines = ["Decision gates for this workflow:", ""]
    for g in gates:
        crit = g.criteria.strip() if g.criteria else "(no criteria documented)"
        lines.append(f"  • Phase {g.phase}: {crit}")
    lines.append("")
    lines.append(
        "Each gate needs explicit approval before work crosses that phase "
        "boundary. Approve with: \"approve phase <N> gate\"."
    )
    return "\n".join(lines)


def format_gate_pause(gate: Gate) -> str:
    """Render the single-gate red-box surfaced on an attempted bypass.

    Includes the exception phrase from the rule YAML template:
        "skip {gate.phase} gate — <reason>"
    rendered concretely as "skip phase <N> gate — <reason>".
    """
    crit = gate.criteria.strip() if gate.criteria else "(no criteria documented)"
    return "\n".join(
        [
            "╔══════════════════════════════════════════════════════════════╗",
            "║ ⛔  WORKFLOW GATE  ⛔                                         ║",
            "╠══════════════════════════════════════════════════════════════╣",
            f"║  Phase {gate.phase} decision gate has not been approved.",
            "║",
            f"║  Gate criteria: {crit}",
            "║",
            "║  Work cannot cross this phase boundary until the gate is",
            "║  approved. To approve, say:",
            f"║    \"approve phase {gate.phase} gate\"",
            "║",
            "║  To proceed PAST this gate without approval, say:",
            f"║    \"skip phase {gate.phase} gate — <reason>\"",
            "║  (the reason is logged to observations.jsonl and reviewed in",
            "║   the next /allostat audit)",
            "╚══════════════════════════════════════════════════════════════╝",
        ]
    )
