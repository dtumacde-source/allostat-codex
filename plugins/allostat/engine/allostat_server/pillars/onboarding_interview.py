"""Onboarding-interview pillar — calibration prefill + architecture audit.

Plugin source: plugin/lib/existing_project_onboarding.py (476 lines).

Per v2.4 conversion plan §4 row ε, the onboarding-interview is a HYBRID
between an MCP resource (the question bank, served from this module) and
an MCP tool (the decision functions: precompute_calibration_answers,
audit_architecture, generate_optimization_proposals).

The wrapper plugin runs the interview state machine locally. It fetches
the question bank from the MCP resource, walks the operator through the
questions in conversation, and writes the resulting imprinted-rules YAML
to the operator's local `.allostat/imprinted/` directory. The server
holds zero per-user interview state.

The decision functions on the server provide:
  - confirm-and-amplify pre-fill: scan the wrapper-supplied excerpts
    of CLAUDE.md / memory files for patterns that infer calibration
    answers (turns 10-minute interview into 30-second confirmation
    exchange — the "dogfood lesson" cited in plugin SKILL.md L172).
  - architecture audit: identify low-confidence classifications + rules
    with absolute language that warrant operator-innate promotion.
  - optimization proposals: convert audit findings into concrete actions.

Tools exposed:

    onboarding_interview_evaluate(excerpt, question) → PillarToolResponse

question.question_type ∈ {
    "precompute_calibration_answers", — pattern-scan operator's existing rules
    "audit_architecture",             — categorize findings from wrapper-supplied classifications
    "generate_optimization_proposals", — derive concrete actions from audit findings
}

Resource exposed:

    question_bank://calibration       — YAML question bank for the wrapper to fetch

Lockstep with plugin/lib/existing_project_onboarding.py — drift between
the inference heuristics or the audit logic and the plugin's copies must
stay zero through v2.4-v2.5 until θ moves shared data to MCP resources.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..pillar_protocol import (
    DecisionPayload,
    ExcerptPayload,
    PillarToolResponse,
    QuestionPayload,
    StatsPayload,
    current_pillar_id,
    record_pillar_invocation,
)

PILLAR_ID = "onboarding-interview"


# ============================================================================
# Calibration inference heuristics — locked to plugin/lib/
# existing_project_onboarding.py::CALIBRATION_INFERENCE_HEURISTICS.
# ============================================================================

CALIBRATION_INFERENCE_HEURISTICS: dict[str, dict[str, Any]] = {
    "calib-q1": {  # deliverable polish
        "patterns": [
            (re.compile(r"polished\s+(PDF|deliverable)", re.IGNORECASE), "a"),
            (re.compile(r"strategy\s+report\s+ships", re.IGNORECASE), "a"),
        ],
        "default": None,
    },
    "calib-q3": {  # project planning
        "patterns": [
            (re.compile(r"3.pass\s+(workflow|design)", re.IGNORECASE), "d"),
            (re.compile(r"brainstorm\s*[→.\-]\s*plan\s*[→.\-]\s*execute", re.IGNORECASE), "d"),
        ],
        "default": None,
    },
    "calib-q4": {  # passes count
        "patterns": [
            (re.compile(r"3.pass", re.IGNORECASE), "c"),
        ],
        "default": None,
    },
    "calib-q5": {  # ROI mental model
        "patterns": [
            (re.compile(r"ROI[-\s]per[-\s]hour", re.IGNORECASE), "b"),
            (re.compile(r"compounding\s+work", re.IGNORECASE), "b"),
            (re.compile(r"lifestyle.business", re.IGNORECASE), "b"),
        ],
        "default": None,
    },
    "calib-q6": {  # risk tolerance
        "patterns": [
            (re.compile(r"explicit[-\s]permission", re.IGNORECASE), "b"),
            (re.compile(r"shared\s+(or\s+)?(public|external)\s+state", re.IGNORECASE), "b"),
        ],
        "default": None,
    },
    "calib-q7": {  # voice scope
        "patterns": [
            (re.compile(r"all\s+generated\s+(copy|output)", re.IGNORECASE), "a"),
            (re.compile(r"avoid\s+AI[-\s]speak", re.IGNORECASE), "a"),
        ],
        "default": None,
    },
    "calib-q11": {  # strategic format
        "patterns": [
            (re.compile(r"polished\s+PDF", re.IGNORECASE), "a"),
        ],
        "default": None,
    },
    "calib-q12": {  # save location
        "patterns": [
            (re.compile(r"Downloads\s+folder", re.IGNORECASE), "a"),
            (re.compile(r"D:.\\Downloads", re.IGNORECASE), "a"),
        ],
        "default": None,
    },
}

# Full question set — the wrapper fetches the bank from the resource, but
# server-side gap detection iterates this list for completeness.
FULL_QUESTION_SET: tuple[str, ...] = tuple(f"calib-q{i}" for i in range(1, 13))


# ============================================================================
# Decision logic
# ============================================================================


def precompute_calibration_answers(
    sources: list[dict[str, str]],
) -> dict[str, Any]:
    """Scan wrapper-supplied rule sources for patterns that infer answers
    to calibration questions.

    Mirrors plugin/lib/existing_project_onboarding.py::precompute_calibration_answers
    — but takes the source texts as parameters (wrapper reads CLAUDE.md
    + memory/*.md client-side and passes the contents in) rather than
    reading the filesystem.

    sources: list of {"path": str, "text": str}. Path is informational
    (returned in answer.source for operator visibility); text is what
    the inference patterns scan.

    Returns dict with:
      answers: {q_id: {"choice": str, "source": str, "confidence": str}}
      gaps: list[str] — question IDs without inferred answers
    """
    answers: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []

    for q_id, heuristic in CALIBRATION_INFERENCE_HEURISTICS.items():
        matched = False
        for pattern, choice in heuristic["patterns"]:
            for src in sources:
                text = src.get("text", "") or ""
                if pattern.search(text):
                    answers[q_id] = {
                        "choice": choice,
                        "source": src.get("path", ""),
                        "confidence": "high",
                    }
                    matched = True
                    break
            if matched:
                break
        if not matched:
            gaps.append(q_id)

    # Questions without heuristics also count as gaps.
    for q_id in FULL_QUESTION_SET:
        if q_id not in answers and q_id not in gaps:
            gaps.append(q_id)

    return {"answers": answers, "gaps": gaps}


def audit_architecture(
    *,
    classifications: dict[str, dict[str, Any]],
    file_texts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the architecture audit on wrapper-supplied classifications.

    Mirrors plugin/lib/existing_project_onboarding.py::audit_architecture.
    The plugin reads filesystem + tier_inference.classify_memory_folder
    inline; here the wrapper does both pieces and passes the result. This
    keeps the server pure-decision.

    classifications: {filename: {tier: str, confidence: str}}
                      from tier_inference.classify_memory_folder
    file_texts: {filename: text} for the cross-cutting-feedback inspection.
                Server scans the text for absolute language; wrapper
                supplies only the file contents needed for this audit
                (the operator is consenting to the audit).

    Returns dict with:
      tier_counts: dict[str, int]
      findings: list[dict]
    """
    if file_texts is None:
        file_texts = {}

    # Aggregate tier counts.
    tier_counts: dict[str, int] = {}
    for cls in classifications.values():
        tier = cls.get("tier", "unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    findings: list[dict[str, Any]] = []

    # Low-confidence classifications.
    for filename, cls in classifications.items():
        if cls.get("confidence") == "low":
            findings.append(
                {
                    "severity": "warning",
                    "file": filename,
                    "issue": (
                        "Tier inference confidence is low — filename pattern "
                        "doesn't match expected templates"
                    ),
                    "recommendation": (
                        "Add 'tier:' frontmatter field to override inference, "
                        "or rename to match a pattern (feedback_<topic>.md, "
                        "project_<name>.md, etc.)"
                    ),
                }
            )

    # Cross-cutting feedback rules with absolute language → consider
    # operator-innate promotion.
    for filename, cls in classifications.items():
        if (
            cls.get("tier") == "user"
            and filename.startswith("feedback_")
            and filename not in ("feedback_communication_style.md",)
        ):
            text = (file_texts.get(filename) or "").lower()
            if any(
                marker in text
                for marker in [
                    "is absolute",
                    "non-negotiable",
                    "always",
                    "never",
                    "hard rule",
                ]
            ):
                findings.append(
                    {
                        "severity": "info",
                        "file": filename,
                        "issue": "Cross-project feedback rule with absolute language",
                        "recommendation": (
                            "Consider promoting to operator-innate "
                            "(~/.claude/CLAUDE.md) — rule is functionally "
                            "non-negotiable"
                        ),
                    }
                )

    return {
        "tier_counts": tier_counts,
        "findings": findings,
    }


def generate_optimization_proposals(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert audit findings into concrete actionable proposals.

    Mirrors plugin/lib/existing_project_onboarding.py::generate_optimization_proposals.
    """
    proposals: list[dict[str, Any]] = []
    for finding in findings:
        if "Consider promoting to operator-innate" in finding.get("recommendation", ""):
            target_file = finding.get("file", "")
            proposals.append(
                {
                    "proposal_type": "promote-to-innate",
                    "target_files": [target_file],
                    "description": (
                        f"Promote {target_file} content to ~/.claude/CLAUDE.md. "
                        "The rule has absolute language ('always', 'never', "
                        "'is absolute') suggesting it's functionally non-negotiable."
                    ),
                    "diff_preview": (
                        f"+ Append rule content to global CLAUDE.md\n"
                        f"+ Mark {target_file} as superseded"
                    ),
                }
            )
    return proposals


# ============================================================================
# Question bank resource (lazy-loaded)
# ============================================================================


def _bundled_question_bank_path() -> Path | None:
    """Resolve the bundled question-bank YAML path.

    Honors ALLOSTAT_QUESTION_BANK_DIR for test fixtures, else searches the
    repo-rooted rules/question-bank/ dir.
    """
    if override := os.environ.get("ALLOSTAT_QUESTION_BANK_DIR"):
        return Path(override) / "calibration-questions.yaml"
    here = Path(__file__).resolve()
    for parent in (
        here.parent.parent.parent,
        here.parent.parent.parent.parent,
    ):
        candidate = parent / "rules" / "question-bank" / "calibration-questions.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_question_bank() -> str | None:
    """Load the calibration question bank YAML text. Returns None when
    the file isn't present. Cached per-call so an env-var override applied
    in tests picks up on the next call without process restart."""
    path = _bundled_question_bank_path()
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ============================================================================
# MCP tool dispatch
# ============================================================================


def onboarding_interview_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function — wrapped by the MCP tool below."""
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        qt = question.question_type
        details = question.details or {}

        if qt == "precompute_calibration_answers":
            # PATCH-198 / AUDIT-D25 (2026-05-24) — privacy moat regression
            # closure. Pre-PATCH-198 this branch accepted `sources` =
            # list[{path, text}] where `text` is raw memory file content.
            # Memory content lives on operator's filesystem; the server has
            # no business reading it (ExcerptPayload privacy invariant).
            # Stub to refuse per advisor §6 (REFUSE not DELETE — preserves
            # tool surface + gives clear deprecation message).
            return _no_op(
                decision_class="raw_content_refused",
                reason=(
                    "precompute_calibration_answers deprecated server-side "
                    "per AUDIT-D25 (2026-05-24): accepts raw memory file "
                    "content via sources[i].text, regressing the privacy "
                    "moat that keeps operator memory client-side. Migrate "
                    "to wrapper-local pre-compute when needed. Server-side "
                    "tool preserved for API surface compatibility but inert."
                ),
            )

        if qt == "audit_architecture":
            # PATCH-198 / AUDIT-D37 (2026-05-24) — privacy moat regression
            # closure. Pre-PATCH-198 this branch accepted `file_texts` =
            # dict[filename → raw text], i.e., raw memory file content
            # bundled with classifications. The classification-only path
            # (no file_texts) is privacy-safe but couldn't be reached via
            # this tool without the wrapper sending content. Stub the
            # whole branch to refuse per advisor §6.
            return _no_op(
                decision_class="raw_content_refused",
                reason=(
                    "audit_architecture deprecated server-side per "
                    "AUDIT-D37 (2026-05-24): accepts raw memory file "
                    "content via file_texts, regressing the privacy moat. "
                    "Migrate to wrapper-local audit (classification + "
                    "content inspection both client-side). Server-side "
                    "tool preserved for API surface compatibility but inert."
                ),
            )

        if qt == "generate_optimization_proposals":
            findings = details.get("findings", [])
            if not isinstance(findings, list):
                return _no_op(
                    decision_class="missing_findings",
                    reason="question.details.findings (list) required",
                )
            proposals = generate_optimization_proposals(findings)
            return PillarToolResponse(
                decision=DecisionPayload(
                    action="proposals_generated",
                    body=(
                        f"{len(proposals)} optimization proposal(s) generated. "
                        f"Review at next `/allostat-promote`."
                        if proposals else
                        "No optimization proposals (architecture is clean)."
                    ),
                    details={
                        "proposals": proposals,
                        "proposal_count": len(proposals),
                    },
                ),
                stats=StatsPayload(
                    pillar_id=PILLAR_ID,
                    decision_class=(
                        "proposed" if proposals else "no_proposals"
                    ),
                ),
            )

        return _no_op(
            decision_class="unknown_question_type",
            reason=f"unknown question_type {qt!r}",
        )
    finally:
        current_pillar_id.reset(token)


def _no_op(
    *, decision_class: str, reason: str | None = None
) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    return PillarToolResponse(
        decision=DecisionPayload(action="no_op", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )


def register_tools(server: FastMCP) -> None:
    @server.tool(
        name="onboarding_interview_evaluate",
        description=(
            "Onboarding-interview pillar — calibration prefill + architecture "
            "audit + optimization proposals. "
            "question_type=precompute_calibration_answers: scan wrapper-"
            "supplied rule-source texts for patterns inferring calibration "
            "answers (12-question bank). "
            "question_type=audit_architecture: categorize wrapper-supplied "
            "tier classifications + flag cross-cutting feedback with absolute "
            "language. "
            "question_type=generate_optimization_proposals: convert audit "
            "findings into concrete actions."
        ),
    )
    def onboarding_interview_evaluate(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return onboarding_interview_decision(excerpt, question)

    @server.resource(
        "allostat://question-bank/calibration",
        name="Calibration question bank",
        description=(
            "The 12-question behavioral-calibration interview bank. Wrapper "
            "fetches once at the start of /allostat init or /allostat "
            "calibrate, walks the operator through gaps after "
            "precompute_calibration_answers identifies which questions need "
            "operator input. YAML format."
        ),
        mime_type="application/yaml",
    )
    def calibration_question_bank() -> str:
        bank = load_question_bank()
        if bank is None:
            return "# Question bank not yet bundled with this server install\n"
        return bank
