"""Innate-enforcer pillar — hardwired guardrail rules.

The innate-enforcer is Allostat's autonomic refusal layer. When a tool
call or signal matches a hardwired innate rule, the pillar returns a
"fire_red_box" decision with the rule's threshold-specific template.
The wrapper plugin renders the template as additionalContext on the
operator's session — same red-box format Allostat has surfaced since
the v2.3 plugin's innate_rule_engine.py.

The 10 hardwired rules ship with the server (innate rules library is
loaded at module init from rules/innate/*.yaml). γ implements the
matching logic; θ migrates the rule data to MCP resources so the
wrapper can fetch the rules surface too.

Tool exposed by γ:

    innate_enforcer_check(excerpt, question) → PillarToolResponse

  question.question_type ∈ {"check_signal", "list_rules"}

  For "check_signal", question.details contains:
    signal_type:    str — the signal label (e.g., "usage_threshold_crossed")
    threshold:      int | str | None — the signal threshold value if any

  The pillar finds the matching rule's matching threshold template and
  returns it. No match → action="no_match".

Decision logic is a faithful but minimal first-cut port of
plugin/lib/innate_rule_engine.py's `_register_rule` + `_build_red_box_handler`
matching. v2.4.x follow-ups can deepen behavior parity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
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

logger = logging.getLogger(__name__)

PILLAR_ID = "innate-enforcer"


# ---------- Rule loading ----------


def _bundled_rules_dir() -> Path | None:
    """Path to the bundled rules/innate/ directory shipped with the server.

    The directory lives one level up from allostat_server/, alongside the
    deploy/ and tests/ trees, so production deploys (which install the
    `allostat_server` Python package) need the rules path resolved
    relative to the package install location.

    Returns None if the bundled rules directory isn't present — production
    deploys are expected to set ALLOSTAT_INNATE_RULES_DIR; θ moves rules
    to an MCP resource that doesn't require filesystem co-location.
    """
    import os

    if override := os.environ.get("ALLOSTAT_INNATE_RULES_DIR"):
        path = Path(override)
        return path if path.is_dir() else None

    # Walk up from this file to find a repo-rooted rules/innate dir.
    here = Path(__file__).resolve()
    for parent in (here.parent.parent.parent, here.parent.parent.parent.parent):
        candidate = parent / "rules" / "innate"
        if candidate.is_dir():
            return candidate
    return None


def _rules_search_locations() -> list[str]:
    """The locations _bundled_rules_dir() searched, for the fail-loud
    diagnostic when no rules directory could be resolved.

    Mirrors _bundled_rules_dir()'s resolution order exactly: a set
    ALLOSTAT_INNATE_RULES_DIR short-circuits (the walk-up never runs),
    otherwise the two repo-rooted walk-up candidates were tried.
    """
    import os

    if override := os.environ.get("ALLOSTAT_INNATE_RULES_DIR"):
        return [f"ALLOSTAT_INNATE_RULES_DIR={override}"]
    here = Path(__file__).resolve()
    return [
        str(parent / "rules" / "innate")
        for parent in (here.parent.parent.parent, here.parent.parent.parent.parent)
    ]


class _LoadDiagnostics:
    """Why the ruleset is in the state it is in.

    Mirrors the wrapper's. Kept because "no rules loaded" and "rules loaded,
    destructive one is fine" were indistinguishable at the call site — both
    were an empty match. An absence is not a signal unless something records it.
    """

    def __init__(self) -> None:
        self.reason: str | None = None

    def set(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason


_LOAD_DIAGNOSTICS = _LoadDiagnostics()

# Every rule the manifest-identity gate removed in the most recent
# working-set build — id + reason, one entry per drop. Deliberately richer
# than _LOAD_DIAGNOSTICS (last reason only): announcement has to name each
# missing rule, and "several dropped, one reported" is itself a silence.
# Mirrored from wrapper/lib/innate_rules.py — drift-parity lockstep tier.
# The wrapper's consumer is pre-tool-use's drop announcement; this side's
# consumer today is the manifest health surface, and the recording exists
# here first so the two _build_working_set bodies stay byte-in-lockstep.
_DROPPED_RULES: list[dict[str, str]] = []

# C01 (2026-08-03): per-file loader failures, captured where they happen so
# the expected-minus-seen diff in _build_working_set can attach a REASON to
# an id that never arrived. `file` is the rule file's name, or "*" for a
# loader-wide condition (PyYAML absent, rules directory missing) that
# answers for every id without a per-file failure of its own. Reset by
# `_get_raw_rules` at each fresh load, alongside the raw cache it describes.
_LOAD_FAILURES: list[dict[str, str]] = []


def _loader_diagnostic_for(rule_id: str) -> str:
    """Best known loader failure for an expected id that never arrived at
    the working-set gate; the missing-file default otherwise.

    Filename prefix maps the id: shipped rule files are `NN-<slug>.*` and
    expected ids are `innate-NN`, so `innate-09` claims failures recorded
    against `09-*` files. Wildcard entries (file == "*") are loader-wide
    conditions and answer for every id that has no per-file failure.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier (test_pillar_wrapper_server_drift_parity).
    """
    prefix = rule_id.rsplit("-", 1)[-1] + "-"
    problems = [
        f["problem"] for f in _LOAD_FAILURES if f["file"].startswith(prefix)
    ]
    if problems:
        return "; ".join(problems)
    wide = [f["problem"] for f in _LOAD_FAILURES if f["file"] == "*"]
    if wide:
        return "; ".join(wide)
    return ("no rule file on disk claims this id (file missing, renamed, or "
            "unreadable)")


def dropped_rules() -> list[dict[str, str]]:
    """Rules the manifest-identity gate dropped when the working set was
    built: `[{"id": ..., "reason": ...}, ...]`, empty when the set is whole.

    C01 (2026-08-03): includes expected ids that never ARRIVED at the gate —
    missing, unreadable, or non-mapping rule files, and the rules-directory-
    lost / PyYAML-absent conditions — not only rules the gate rejected. An
    absence and a rejection are both degradations; only one of them used to
    leave a record.

    Forces the build (same trigger as matching) so a caller asking before
    any match still gets the truth. Returns copies — the module state is
    not a mutation surface.
    """
    _get_rules()
    return [dict(d) for d in _DROPPED_RULES]

DESTRUCTIVE_RULE_ID = "innate-02"

# R2 (2026-07-20) — mirrors wrapper/lib/innate_rules.py. See the long note
# there. Short version: a rule file that parses, carries the right id, and can
# never match is not a guard, and the previous loader admitted it silently.
_ENFORCEABLE_DESTRUCTIVE_SEVERITIES = frozenset({"lethal", "critical"})
_BLOCKING_RESPONSE_ACTIONS = frozenset({"hard_pause"})

# Round-7 (2026-07-23) mirror of wrapper/lib/innate_rules.py — lockstep tier.
# See that copy's docstrings for the full rationale of every constant below;
# comments here are kept short and point back at the wrapper copy rather than
# duplicating the reasoning.
_KNOWN_SEVERITIES = frozenset({"lethal", "critical", "high", "medium", "low"})

# The innate rules are the fixed constitutional layer — the expected set is a
# manifest, not a count. Mirrored verbatim (same generator expression) from
# wrapper/lib/innate_rules.py:EXPECTED_INNATE_RULE_IDS.
EXPECTED_INNATE_RULE_IDS = frozenset(f"innate-{n:02d}" for n in range(1, 13))


def _json_key_spelling(key):
    """Return the key string JSON itself would write for `key`.

    Derived by asking `json.dumps`, not by reimplementing its rules. Python's
    `str()` is NOT JSON's key spelling and the difference is silent:

        str(True) == "True"   JSON writes "true"
        str(None) == "None"   JSON writes "null"

    So a YAML rule carrying a boolean or null mapping key hashed differently
    from its vendored JSON compilation -- exactly the innate-05 defect, in a
    type that had not been used yet. Claiming that class closed while only
    integers worked is worse than leaving it open, because it stops anyone
    looking.

    Raises TypeError for keys JSON cannot express as an object key at all
    (dates, tuples, objects) rather than inventing a spelling for them.
    """
    if isinstance(key, str):
        return key
    try:
        encoded = json.dumps({key: 0})
    except TypeError as exc:
        raise TypeError(
            f"mapping key {key!r} of type {type(key).__name__} cannot be a JSON "
            "object key; it has no canonical spelling and must not be hashed"
        ) from exc
    return next(iter(json.loads(encoded)))


def _canonicalize_keys(value):
    """Recursively rewrite mapping keys to their JSON spelling.

    WHY (2026-07-24, customer-facing defect): rule 05's `response.templates`
    keys are INTEGERS in YAML (`50000:`) and necessarily STRINGS in JSON
    (`"50000":`). `json.dumps` renders both as strings, so the output looks
    identical -- but `sort_keys=True` sorts the INT keys numerically and the STR
    keys lexicographically:

        int keys -> {"50000":..,"150000":..,"500000":..,"750000":..}
        str keys -> {"150000":..,"50000":..,"500000":..,"750000":..}

    Same content, different byte order, different SHA-256. So the YAML source
    and its vendored JSON compilation hashed differently for innate-05 alone,
    the loader preferred the complete vendored JSON set, and every customer's
    first session reported `ruleset_degraded` and silently ran 11 of 12 rules.

    COLLISIONS ARE REJECTED, not resolved. `{1: "a", "1": "b"}` collapses to one
    member under any str-based scheme, silently discarding the other. A hash
    computed over silently-lost content is a hash of something nobody wrote.
    """
    if isinstance(value, dict):
        out = {}
        origin = {}
        for key, val in value.items():
            spelled = _json_key_spelling(key)
            if spelled in out:
                raise ValueError(
                    f"mapping keys {origin[spelled]!r} and {key!r} both spell "
                    f'as "{spelled}" in JSON; one would silently overwrite the '
                    "other, so this content cannot be canonicalized"
                )
            origin[spelled] = key
            out[spelled] = _canonicalize_keys(val)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalize_keys(item) for item in value]
    return value


def _rule_content_hash(rule: dict) -> str:
    """SHA-256 of the rule's NORMALIZED content. Mirrored verbatim from
    wrapper/lib/innate_rules.py::_rule_content_hash — see that copy's
    docstring for the `default=str` hash-stability note (innate-11/12 carry
    an unquoted YAML date scalar that yaml.safe_load parses as a
    datetime.date, not natively JSON-serializable)."""
    return hashlib.sha256(
        json.dumps(_canonicalize_keys(rule), sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# Generated by running `_rule_content_hash` over THIS repo's own shipped
# `server/rules/innate/*.yaml` (the same algorithm wrapper/scripts/
# gen_innate_manifest.py uses, pointed at this side's rules dir instead of
# the wrapper's). NOT copy-pasted from wrapper/lib/innate_rules.py's own
# dict: as of this commit the two rule directories are not byte-identical
# for every id (innate-02/05/11/12 carry different response wording / trigger
# content on the two sides — a pre-existing content drift between the
# wrapper and server rule libraries, orthogonal to this task's code mirror
# and out of scope to reconcile here; flagged for a follow-up). Pinning to
# THIS side's actual shipped content is what makes "shipped 12-rule set
# loads clean and hash-clean server-side" provable; pasting the wrapper's
# literal values would make every one of those four ids fail its own
# manifest on day one. Regenerate ONLY as part of a deliberate constitution
# change to server/rules/innate, in the same commit as the rule edit.
EXPECTED_INNATE_RULE_HASHES: dict[str, str] = {
    # 2026-07-30: re-pinned alongside the wrapper for the dead-remedy fix. The
    # server's copy of innate-02 was TWO MONTHS STALE — PATCH-175 (2026-05-22)
    # replaced the fictional "Yes, run {short_command}" phrase in the wrapper
    # only, so this side kept printing it. 01/03/04/09/10 were byte-equivalent
    # to the wrapper and are again; 02/05/11/12 carry pre-existing content
    # divergence that predates this change (tracked separately).
    "innate-01": "429230ae9a3158fcf9a7a732e160be07d92236796a4c70a231dceb53f74eb9ab",
    # 2026-08-01: re-pinned for the destructive-operation registry (advisor
    # H-03). The TRIGGER block is now lockstep with the wrapper's; the
    # response text remains this side's own shorter form, which is why the
    # hash still differs from the wrapper pin.
    # 2026-08-02: re-pinned for the checkout split (safe `--detach`/`--track`
    # freed; `-f`/`--force` closed). The rule edit landed lockstep on both
    # sides but only the wrapper pin was regenerated — this side's innate-02
    # was hash-dropped and CI caught the server running 11 of 12 rules
    # (manifest test + every cross-engine parity case, all four jobs, one
    # cause). The comment above this table says it plainly: regenerate in the
    # SAME COMMIT as the rule edit. That instruction now has a measured miss.
    "innate-02": "468e3b5c6dcd32efae4d5a314b6c48e74b2f44ba6bbc3fa81999f8f30236d3f8",
    "innate-03": "1ff64c569af6b8d692524125aef755e7a9c0ae7d585aa4873028f6ac711d13d1",
    "innate-04": "8322b4dc89931e8a803801b804417a3a852f6ceab9a184eadf07a752d24aa5b4",
    "innate-05": "0c0b90f14a14d9244d25cc458efa57add074cad178744b1b742a47926cefaccd",
    "innate-06": "c4f114cf62742061e333b63292cd7327e20eac5b597cf930c120ef9a735009d9",
    "innate-07": "793488571903147d9b951bab5aa61705cb74265076681454103ce75a77e58c50",
    "innate-08": "bee935b1b2eb8f27b958c4b970bb6e7441dce7c39a7f7ddfc287cea1c70d6d76",
    "innate-09": "2f13b4f770be176ffdc5482755ef9805d13ebd8bbf18690c5646f29bc49799cb",
    # 2026-08-03 (F02): innate-10 narrowed to its recognized shapes and two
    # unemittable trigger operations removed. Lockstep mirror of the wrapper
    # pin — both move in the same commit or the tier fails parity.
    "innate-10": "5813fb7ac4f3ca17cdd37b29a104866a10d6aee377620b4ca5650b58316fe72c",
    "innate-11": "db354cc3796dda4f402032f3922188aab9f9dd26ac64edd9c2bb952d47b8d042",
    "innate-12": "442e918532b577aad3e27c087c6b33dee1520658c7e4922ef81e3b9ace06c3f9",
}


# Round-7 inversion — schema each runtime consumer honors, one entry per
# field the matcher may carry. Validity means EVERY supplied field proves
# out against this schema, not "one field is usable". Escalation-clause
# schema completion (2026-07-23): action/threshold/check/platforms/condition
# join description as recognized-and-validated fields (load-bearing content
# for the signal-dispatch consumer, not this module's own matcher) — the
# same widening wrapper/lib/innate_rules.py made. Mirrored verbatim from
# that copy — drift-parity lockstep tier.
def _entry_field_problem(key: str, val: Any) -> str | None:
    """Why `key: val` fails the matcher-field schema, or None if proven."""
    if key in ("command_pattern", "file_pattern", "context", "signal", "phrase",
               "action", "check", "condition"):
        if isinstance(val, str) and val.strip():
            return None
        return f"{key} must be a non-empty string, got {val!r}"
    if key == "operation":
        if isinstance(val, str) and val.strip():
            return None
        if isinstance(val, (list, tuple)) and len(val) > 0 and all(
            isinstance(o, str) and o.strip() for o in val
        ):
            return None
        return ("operation must be a non-empty string or a non-empty list of "
                f"non-empty strings, got {val!r}")
    if key == "platforms":
        if isinstance(val, (list, tuple)) and len(val) > 0 and all(
            isinstance(o, str) and o.strip() for o in val
        ):
            return None
        return (f"platforms must be a non-empty list of non-empty strings, "
                f"got {val!r}")
    if key == "threshold":
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return None
        return f"threshold must be a positive number, got {val!r}"
    if key in ("case_sensitive", "target_must_exist"):
        if isinstance(val, bool):
            return None
        return f"{key} must be a boolean, got {val!r}"
    if key == "description":
        if isinstance(val, str):
            return None
        return f"description must be a string, got {val!r}"
    return (f"unknown field {key!r} is not in the matcher schema and cannot "
            "be proven safe")


_MATCH_ANY_USABLE_KEYS = ("command_pattern", "file_pattern", "operation",
                          "context", "signal", "phrase")


def _matcher_entry_problem(entry: Any) -> str | None:
    """Why this match_any entry is unproven, or None when EVERY supplied field
    validates AND at least one usable matcher key is present.

    Round-7 inversion: round 6 returned success on the FIRST usable field,
    which excused a malformed sibling the runtime went on to consume. A rule
    is the unit of trust; a partially corrupted entry degrades, it does not
    half-run.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier."""
    if not isinstance(entry, dict) or not entry:
        return f"match_any entry {entry!r} is not a non-empty mapping"
    for key, val in entry.items():
        p = _entry_field_problem(key, val)
        if p:
            return f"match_any entry {entry!r}: {p}"
    if not any(k in entry for k in _MATCH_ANY_USABLE_KEYS):
        return (
            f"match_any entry {entry!r} has no usable matcher "
            "(command_pattern/file_pattern/operation/context/signal/phrase), "
            "so it can never fire"
        )
    return None


def _match_all_entry_problem(entry: Any) -> str | None:
    """Why this match_all entry is unproven, or None when EVERY supplied field
    validates AND at least one of operation/context is present.

    match_all has exactly one runtime consumer (_match_all_satisfied) reading
    ONLY operation and context; an empty mapping is vacuously satisfied
    (matches EVERYTHING) and a context list is unhashable at runtime — both
    reject here. Round-7: a valid operation no longer excuses a malformed
    context sibling.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier."""
    if not isinstance(entry, dict) or not entry:
        return f"match_all entry {entry!r} is not a non-empty mapping"
    for key, val in entry.items():
        if key in ("operation", "context", "description"):
            p = _entry_field_problem(key, val)
            if p:
                return f"match_all entry {entry!r}: {p}"
        else:
            return (f"match_all entry {entry!r}: field {key!r} is not consumed "
                    "by _match_all_satisfied and cannot be proven safe")
    if not ("operation" in entry or "context" in entry):
        return (
            f"match_all entry {entry!r} has no supported condition "
            "(operation/context), so it cannot gate anything"
        )
    return None


def _uncompilable_command_patterns(trigger: dict) -> list[str]:
    """Reasons any `command_pattern` in `trigger.match_any` cannot match.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier."""
    reasons: list[str] = []
    for entry in trigger.get("match_any") or []:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("command_pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        if any(ord(ch) < 32 for ch in pattern):
            reasons.append(
                f"{pattern!r} contains control character(s) — a YAML "
                "double-quoted scalar ate the backslash; write it doubled"
            )
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            reasons.append(f"{pattern!r} ({exc})")
    return reasons


def validate_innate_rule(rule: Any) -> list[str]:
    """Return the reasons `rule` cannot serve as an enforceable innate rule.

    Empty list means enforceable. Applies to EVERY rule, not just the
    destructive one; `validate_destructive_rule` layers stricter,
    innate-02-specific requirements on top of this.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier."""
    if not isinstance(rule, dict):
        return ["it did not parse to a mapping"]

    problems: list[str] = []

    rid = rule.get("id")
    if not isinstance(rid, str) or not rid.strip():
        problems.append(f"its id is {rid!r}, not a non-empty string")

    severity = rule.get("severity")
    if severity not in _KNOWN_SEVERITIES:
        problems.append(
            f"its severity is {severity!r}, not one of {sorted(_KNOWN_SEVERITIES)}"
        )

    trigger = rule.get("trigger")
    if not isinstance(trigger, dict) or not trigger:
        problems.append(
            "its trigger is missing, not a mapping, or empty, so it can never match"
        )
    else:
        match_any = trigger.get("match_any")
        match_all = trigger.get("match_all")
        entry_problems: list[str] = []
        has_any = False
        if isinstance(match_any, list):
            for e in match_any:
                p = _matcher_entry_problem(e)
                if p:
                    entry_problems.append(p)
                else:
                    has_any = True
        has_all = False
        if isinstance(match_all, list) and match_all:
            all_ok = True
            for e in match_all:
                p = _match_all_entry_problem(e)
                if p:
                    entry_problems.append(p)
                    all_ok = False
            has_all = all_ok  # one bad entry kills the whole conjunction at runtime
        if not (has_any or has_all):
            problems.append(
                "it has no usable match_any or match_all trigger, so it can "
                "never match"
            )
        problems.extend(entry_problems)
        bad = _uncompilable_command_patterns(trigger)
        if bad:
            problems.append("it has command pattern(s) that cannot match: "
                            + "; ".join(bad))

    response = rule.get("response")
    action = response.get("action") if isinstance(response, dict) else None
    if not isinstance(action, str) or not action.strip():
        problems.append(
            f"its response action is {action!r}, not a non-empty string, so a "
            "match would do nothing"
        )

    return problems


def validate_destructive_rule(rule: Any) -> list[str]:
    """Return the reasons `rule` is not an enforceable destructive guard."""
    if not isinstance(rule, dict):
        return ["it did not parse to a mapping"]

    problems: list[str] = []

    if rule.get("id") != DESTRUCTIVE_RULE_ID:
        problems.append(f"its id is {rule.get('id')!r}, not {DESTRUCTIVE_RULE_ID!r}")

    trigger = rule.get("trigger")
    if not isinstance(trigger, dict):
        problems.append(
            "its trigger is missing or is not a mapping, so it can never match"
        )
    elif not trigger:
        problems.append("its trigger is an empty mapping, so it can never match")
    else:
        matchers = 0
        uncompilable: list[str] = []
        entries = trigger.get("match_any")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                # Round-6 P1-3 mirror: skip entries with no usable matcher key
                # at all (mirrors wrapper/lib/innate_rules.py verbatim).
                if _matcher_entry_problem(entry) is not None:
                    continue
                pattern = entry.get("command_pattern")
                if isinstance(pattern, str) and pattern:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        uncompilable.append(f"{pattern!r} ({exc})")
                    else:
                        # Compiling is necessary and not sufficient — a YAML
                        # double-quoted scalar turns `"\bX\b"` into literal
                        # BACKSPACE characters, which compile fine and match
                        # nothing. See the wrapper copy for the full note.
                        if any(ord(ch) < 32 for ch in pattern):
                            uncompilable.append(
                                f"{pattern!r} contains control character(s) — "
                                "a YAML double-quoted scalar ate the backslash; "
                                "write the pattern with doubled backslashes"
                            )
                        else:
                            matchers += 1
                file_pattern = entry.get("file_pattern")
                if isinstance(file_pattern, str) and file_pattern:
                    matchers += 1
        match_all = trigger.get("match_all")
        # Round-6 P1-3 mirror: match_all only counts as a matcher when EVERY
        # entry is usable (mirrors wrapper/lib/innate_rules.py verbatim).
        if isinstance(match_all, list) and match_all and all(
            _match_all_entry_problem(e) is None for e in match_all
        ):
            matchers += 1

        if uncompilable:
            problems.append(
                "it contains destructive pattern(s) that do not compile: "
                + "; ".join(uncompilable)
            )
        if matchers == 0:
            problems.append(
                "it has no compile-valid destructive matcher, so it can never match"
            )

    severity = rule.get("severity")
    if severity not in _ENFORCEABLE_DESTRUCTIVE_SEVERITIES:
        problems.append(
            f"its severity is {severity!r}, not one of "
            f"{sorted(_ENFORCEABLE_DESTRUCTIVE_SEVERITIES)}"
        )

    response = rule.get("response")
    action = response.get("action") if isinstance(response, dict) else None
    if action not in _BLOCKING_RESPONSE_ACTIONS:
        problems.append(
            f"its response action is {action!r}, which does not block; expected "
            f"one of {sorted(_BLOCKING_RESPONSE_ACTIONS)}"
        )

    return problems


def _build_working_set(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The only rules the matcher may consult. Round-7 inversion: membership
    is PROVEN, not assumed.

      - id not in EXPECTED_INNATE_RULE_IDS -> dropped.
      - content hash != the id's pin -> dropped. Identity, never severity,
        decides who acts.
      - duplicates: only hash-matching copies survive, and those are
        byte-identical by construction -> dedupe to one.
      - fails validate_innate_rule -> dropped.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier. See that copy's docstring for the full rationale."""
    by_id: dict[str, dict[str, Any]] = {}
    # Rebuilt on every working-set build: this list is "what the gate removed
    # THIS time", not an append-only history. `_LOAD_DIAGNOSTICS` keeps only
    # the LAST reason, which is how a 12-rule set could lose several rules and
    # report one; announcement needs every drop, per rule, with its reason
    # (2026-08-02 — the server ran 11 of 12 and nothing said so until CI).
    _DROPPED_RULES.clear()

    def _record_drop(rule_id: Any, reason: str) -> None:
        logger.warning("innate rule rejected: %s", reason)
        _LOAD_DIAGNOSTICS.set(reason)
        _DROPPED_RULES.append({"id": str(rule_id), "reason": reason})

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rid = rule.get("id")
        if rid not in EXPECTED_INNATE_RULE_IDS:
            _record_drop(rid, f"{rid!r} is not in the innate manifest; rule dropped")
            continue
        observed_hash = _rule_content_hash(rule)
        if observed_hash != EXPECTED_INNATE_RULE_HASHES.get(rid):
            # M01 (2026-08-03): the OBSERVED hash is part of the reason, so
            # two different edits to the same rule are two different
            # degradations — the announcement digest keys on reasons, and a
            # generic reason made every edit of one rule the same event.
            _record_drop(rid, (f"{rid} does not match its pinned content hash "
                               f"(edited or replaced; observed "
                               f"{observed_hash[:12]}); rule dropped"))
            continue
        if validate_innate_rule(rule):
            _record_drop(rid, (f"{rid} failed validation and was dropped from the "
                               "working set: " + "; ".join(validate_innate_rule(rule))))
            continue
        if rid == DESTRUCTIVE_RULE_ID and validate_destructive_rule(rule):
            _record_drop(rid, (f"{DESTRUCTIVE_RULE_ID} loaded but is not enforceable: "
                               + "; ".join(validate_destructive_rule(rule))))
            continue
        by_id.setdefault(rid, rule)   # survivors of one id are byte-identical
    # C01 (2026-08-03): the loop above can only drop what PARSED AND ARRIVED.
    # A rule file that is missing, unreadable, or not a mapping never appears
    # in `rules`, so the gate never saw it and nothing recorded it — the
    # reviewer moved one rule file out of the glob and the real hook exited 0
    # with zero durable records. Expected-minus-seen is the only construction
    # that can see an ABSENCE rather than a rejection; loader-captured
    # per-file failures supply the reason when one exists.
    seen_ids = {str(r.get("id")) for r in rules if isinstance(r, dict)}
    for rid in sorted(set(EXPECTED_INNATE_RULE_IDS) - seen_ids):
        _record_drop(
            rid,
            f"{rid} never reached the working-set gate: "
            + _loader_diagnostic_for(rid),
        )
    # Preserve the canonical first-match order: manifest order is rule 01..12.
    return [by_id[rid] for rid in sorted(by_id)]


def _note_duplicate_manifest_claims(rules: list[dict[str, Any]]) -> None:
    """Record (via `_LOAD_DIAGNOSTICS`) any manifest id claimed by more than
    one raw-loaded file.

    `_build_working_set`'s hash gate silently dedupes hash-MATCHING
    duplicates (by construction they are byte-identical, so there is no
    behavioral difference in which copy "wins"), which would otherwise let a
    genuine duplicate-identity condition reach `_get_rules()` with no trace.
    This runs over the RAW list (before the identity gate collapses it) so
    the duplicate claim itself is recorded regardless of whether the two
    copies happen to hash-match.

    Server-specific plumbing: this module does not carry a wrapper-style
    standalone `ruleset_degraded()` (see this file's `destructive_guard_
    degraded`, which is the narrower surface Task 5 extends). Wrapper's
    equivalent duplicate-claim check lives inside `ruleset_degraded()`
    (lib/innate_rules.py) — no verbatim mirror source exists for THIS
    function name, so it is not part of the wrapper↔server lockstep parity
    set."""
    counts: dict[str, int] = {}
    for rule in rules:
        if isinstance(rule, dict):
            rid = rule.get("id")
            if isinstance(rid, str):
                counts[rid] = counts.get(rid, 0) + 1
    for rid in sorted(counts):
        if counts[rid] > 1:
            _LOAD_DIAGNOSTICS.set(
                f"{rid} is claimed by {counts[rid]} files — a duplicate "
                "identity cannot be trusted"
            )


def _load_rules(rules_dir: Path) -> list[dict[str, Any]]:
    """Load every non-LEGACY YAML in rules_dir.

    R2 (2026-07-20) — FAILURES ARE RECORDED, NOT SILENT, and an unenforceable
    destructive rule is REJECTED rather than admitted.

    The previous docstring said "Failures skip silently" and meant it: an
    unreadable file, an unparseable file, and a non-mapping document each hit a
    bare `continue`, so eleven rules could arm while the twelfth — the
    destructive one — quietly did not, and every caller saw an ordinary
    no-match. That is the same partial-ruleset defect the wrapper closed in
    0B.4, still open on the server, which is the copy connected agents read.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; innate rules cannot load")
        _LOAD_DIAGNOSTICS.set("PyYAML is not installed, so no rule file can be parsed")
        # C01: loader-wide condition — answers for every expected id.
        _LOAD_FAILURES.append({
            "file": "*",
            "problem": "PyYAML is not installed, so no rule file can be parsed",
        })
        return []

    rules: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        if "_LEGACY_" in path.name:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("innate rule %s could not be loaded: %s", path.name, exc)
            _LOAD_DIAGNOSTICS.set(
                f"{path.name} could not be parsed ({type(exc).__name__})"
            )
            _LOAD_FAILURES.append({
                "file": path.name,
                "problem": f"could not be parsed ({type(exc).__name__})",
            })
            continue
        if not isinstance(data, dict):
            logger.warning("innate rule %s is not a mapping; skipped", path.name)
            _LOAD_DIAGNOSTICS.set(f"{path.name} is not a YAML mapping")
            _LOAD_FAILURES.append({
                "file": path.name,
                "problem": "is not a YAML mapping",
            })
            continue
        if "id" not in data:
            data["id"] = path.stem
        if data.get("id") == DESTRUCTIVE_RULE_ID:
            problems = validate_destructive_rule(data)
            if problems:
                reason = (
                    f"{DESTRUCTIVE_RULE_ID} loaded from {path.name} but is not "
                    "enforceable: " + "; ".join(problems)
                )
                logger.error("innate rule rejected: %s", reason)
                print(f"ALLOSTAT ERROR: {reason}", file=sys.stderr)
                _LOAD_DIAGNOSTICS.set(reason)
                _LOAD_FAILURES.append({"file": path.name, "problem": reason})
                continue
        rules.append(data)
    return rules


def _manifest_identity_problems(raw_rules: list[dict[str, Any]]) -> list[str]:
    """Fresh-computed (never `_LOAD_DIAGNOSTICS`-dependent) manifest-identity
    problems across the RAW loaded rules: an expected id claimed by more
    than one file, or hash-mismatched against its pin.

    Recomputed on every call by re-scanning `raw_rules` against
    `EXPECTED_INNATE_RULE_IDS`/`EXPECTED_INNATE_RULE_HASHES` — deliberately
    NOT read from the sticky `_LOAD_DIAGNOSTICS.reason` singleton, so a rule
    that was broken and is now fixed reads as fixed instead of a stale
    reason from an earlier load leaking into a later, healthy one.

    Deliberately narrower than wrapper's `ruleset_degraded()`
    (lib/innate_rules.py): this only reports on ids that HAVE at least one
    claimant on disk (duplicate-claim / hash-mismatch), never "id X has no
    file at all". Full-manifest completeness (flagging every one of the
    other eleven rules as "missing" whenever a test or a real deployment
    exercises the destructive rule in isolation) is out of scope for this
    module's narrower 2-tuple `destructive_guard_degraded` contract — the
    server does not carry a standalone `ruleset_degraded()` surface, and
    several existing tests (e.g. `test_a_valid_rule_02_is_not_degraded`,
    `server/tests/test_server_innate_loader_fails_closed.py`) legitimately
    exercise innate-02 with the other eleven rule files absent. The
    destructive rule's OWN absence is still covered — by this function's
    caller's pre-existing "N rule(s) loaded but {DESTRUCTIVE_RULE_ID} was
    not among them" fallback, unrelated to this helper.

    "Unknown" (an extra, non-manifest id) IS included: unlike "missing", it
    only reports on ids that DO appear on disk, so it cannot trip on the
    same partial-ruleset test scenarios above — an extra id is never
    ambiguous with "this test simply didn't write that file"."""
    by_id: dict[str, list[dict]] = {}
    for r in raw_rules:
        if isinstance(r, dict):
            by_id.setdefault(str(r.get("id")), []).append(r)
    problems: list[str] = []
    for rid in sorted(EXPECTED_INNATE_RULE_IDS):
        claimants = by_id.get(rid, [])
        if not claimants:
            continue
        if len(claimants) > 1:
            problems.append(
                f"{rid} is claimed by {len(claimants)} files — a duplicate "
                "identity cannot be trusted"
            )
        expected_hash = EXPECTED_INNATE_RULE_HASHES.get(rid)
        if not any(_rule_content_hash(r) == expected_hash for r in claimants):
            problems.append(
                f"{rid} does not match its pinned content hash — the rule "
                "on disk is not the manifest rule (edited or replaced)"
            )
    for rid in sorted(set(by_id) - EXPECTED_INNATE_RULE_IDS):
        problems.append(
            f"{rid} is not in the innate manifest — the constitution is "
            "fixed and non-extensible"
        )
    return problems


def destructive_guard_degraded() -> tuple[bool, str]:
    """(degraded, reason). True when the destructive rule is not enforceable,
    OR (Round-7) when the manifest-identity gate found ANY sibling problem
    among the raw ruleset — missing/duplicate/hash-mismatch — even though
    innate-02 itself loaded and validated cleanly. A different lethal rule
    corrupted by the same partial sync that could just as easily have hit
    innate-02 is exactly the signal a SessionStart banner needs; without
    this, the one backstop that runs on every command would read healthy
    while the operator's safety net is actually holed elsewhere. Mirrors
    wrapper's `destructive_guard_degraded`/`ruleset_degraded` split
    (lib/innate_rules.py), narrowed to this module's existing 2-tuple
    contract — no separate public `ruleset_degraded` surface exists
    server-side; `_manifest_identity_problems` supplies the missing/
    duplicate/hash-mismatch half, and `_LOAD_DIAGNOSTICS` (already set by
    `_load_rules`/`_build_working_set`/`_note_duplicate_manifest_claims`)
    remains the fallback for load-time failures that leave no rule object
    to inspect (PyYAML absent, a file that failed to parse).

    Enforceability, not mere presence, for innate-02 itself — see
    `validate_destructive_rule`.
    """
    rules = _get_rules()
    manifest_problems = _manifest_identity_problems(_get_raw_rules())
    for rule in rules:
        if isinstance(rule, dict) and rule.get("id") == DESTRUCTIVE_RULE_ID:
            problems = validate_destructive_rule(rule)
            if not problems:
                if manifest_problems:
                    return True, "; ".join(manifest_problems)
                return False, ""
            return True, (
                f"{DESTRUCTIVE_RULE_ID} is present but not enforceable: "
                + "; ".join(problems)
            )
    if not rules:
        return True, _LOAD_DIAGNOSTICS.reason or "no innate rules could be loaded"
    reason = (
        "; ".join(manifest_problems) if manifest_problems
        else (_LOAD_DIAGNOSTICS.reason or "its file was unreadable or unparseable")
    )
    return True, (
        f"{len(rules)} rule(s) loaded but {DESTRUCTIVE_RULE_ID} was not among "
        f"them: {reason}"
    )


# Module-level cached rule lists. _RULES/_RAW_RULES are None until first
# lookup so tests can override the rules directory by setting
# ALLOSTAT_INNATE_RULES_DIR before any tool call. _RULES_LOCK makes the
# lazy init thread-safe under the FastMCP threadpool (pre-release item 8 —
# double-checked locking), shared by both caches since they are populated
# together on the same first-lookup path.
#
# Round-7: RAW loaded rules (every file `_load_rules` accepted, before the
# manifest-identity gate) cached separately from `_RULES` (the GATED working
# set `_get_rules()` returns) — mirrors wrapper's `_get_raw_rules()`/
# `_RAW_RULES_CACHE` split (lib/innate_rules.py). Degraded reporting needs
# the raw list: a shadow file, a duplicate, or a hash-mismatched edit is
# exactly what the working-set gate REMOVES, so reporting that read the
# gated set could never see the thing it exists to name.
_RAW_RULES: list[dict[str, Any]] | None = None
_RULES: list[dict[str, Any]] | None = None
_RULES_LOCK = threading.Lock()


def _get_raw_rules() -> list[dict[str, Any]]:
    """Return every rule `_load_rules` accepted, before the manifest-identity
    gate. This is the pre-Round-7 `_get_rules()` body verbatim (same loud
    missing-rules-dir fallback), kept as its own cached accessor because
    `destructive_guard_degraded` (via `_manifest_identity_problems`) needs
    the raw, ungated list."""
    global _RAW_RULES
    if _RAW_RULES is None:
        with _RULES_LOCK:
            if _RAW_RULES is None:
                # C01: the failures list describes THIS load — reset with
                # the cache.
                _LOAD_FAILURES.clear()
                rules_dir = _bundled_rules_dir()
                if rules_dir is None:
                    # W2-B (audit H-10): this fallback used to be SILENT —
                    # every hardwired guardrail off with zero signal. The
                    # empty ruleset stays (server keeps serving), but the
                    # condition is now loud: error log + stderr line so it
                    # shows in journalctl even if logging config swallows
                    # the logger. Fires once per process — the double-
                    # checked lazy init only runs on first lookup.
                    searched = ", ".join(_rules_search_locations())
                    message = (
                        "innate rules directory not found (searched: "
                        f"{searched}) — innate ruleset EMPTY — all "
                        "server-side guardrails disabled. Set "
                        "ALLOSTAT_INNATE_RULES_DIR to the rules/innate "
                        "directory to restore them."
                    )
                    logger.error(message)
                    print(f"ALLOSTAT ERROR: {message}", file=sys.stderr)
                    # C01: loader-wide condition — answers for every id.
                    _LOAD_FAILURES.append({
                        "file": "*",
                        "problem": "innate rules directory not found",
                    })
                    _RAW_RULES = []
                else:
                    _RAW_RULES = _load_rules(rules_dir)
    return _RAW_RULES


def _get_rules() -> list[dict[str, Any]]:
    """Return the GATED working set the matcher may consult; load on first
    call. Round-7: gating moved to `_build_working_set` (every rule,
    manifest-identity), applied to the raw list `_get_raw_rules()` returns —
    mirrors wrapper's `_get_rules()` (lib/innate_rules.py).

    `_get_raw_rules()` is called BEFORE acquiring `_RULES_LOCK` (not from
    within the `with` block): `_RULES_LOCK` is a plain `threading.Lock`,
    not reentrant, and `_get_raw_rules()` acquires that same lock itself —
    nesting the acquisition here would deadlock the first concurrent
    lookup instead of merely racing it.
    """
    global _RULES
    if _RULES is None:
        raw = _get_raw_rules()
        with _RULES_LOCK:
            if _RULES is None:
                _note_duplicate_manifest_claims(raw)
                _RULES = _build_working_set(raw)
                # H02 (2026-08-03): the drops finally get a consumer. The
                # per-drop logger.warning lines above are prose; this is ONE
                # machine-parseable structured event per build naming every
                # degraded id — journald is this RAM-only server's durable
                # operational record (observation_helpers.py's contract).
                # Once per process by construction (the cache makes builds
                # once); never raises past the build.
                if _DROPPED_RULES:
                    try:
                        from ..logging import _emit_log_line, _utc_now_iso
                        _emit_log_line({
                            "event": "innate_ruleset_degradation",
                            "ts": _utc_now_iso(),
                            "rules": [d["id"] for d in _DROPPED_RULES],
                            "reasons": {
                                d["id"]: d["reason"] for d in _DROPPED_RULES
                            },
                            "count": len(_DROPPED_RULES),
                        })
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "innate degradation log emit failed", exc_info=True,
                        )
    return _RULES


def reset_rules_cache() -> None:
    """Test helper — force the rules cache to reload on next lookup.

    Also clears `_LOAD_DIAGNOSTICS` (Round-7): it is a sticky, first-reason-
    wins singleton, and `destructive_guard_degraded` now reads it even on
    the "innate-02 itself is fine" path — leaving a prior test's reason in
    place across a forced reload would leak a stale degraded verdict into
    an otherwise-healthy reload."""
    global _RULES, _RAW_RULES
    _RULES = None
    _RAW_RULES = None
    _LOAD_DIAGNOSTICS.reason = None


# ---------- Decision logic ----------


def _match_signal_to_rule(
    rules: list[dict[str, Any]],
    signal_type: str,
    threshold: Any,
) -> tuple[dict[str, Any], Any] | None:
    """Return (rule, matched_threshold) when `signal_type` (and optional
    `threshold`) match any rule's trigger.match_any entry. None otherwise.

    Mirrors plugin/lib/innate_rule_engine.py's match_any iteration. The
    matched_threshold is what the response.templates dict should be keyed
    against (yaml may load it as int or str — we coerce on lookup).
    """
    for rule in rules:
        trigger = rule.get("trigger", {})
        match_any = trigger.get("match_any", [])
        if not isinstance(match_any, list):
            continue
        for entry in match_any:
            if not isinstance(entry, dict):
                continue
            if entry.get("signal") != signal_type:
                continue
            rule_threshold = entry.get("threshold")
            if rule_threshold is None:
                # Signal-only match — no threshold to compare.
                return rule, None
            # Try int comparison; fall back to string comparison.
            try:
                if int(rule_threshold) == int(threshold):
                    return rule, rule_threshold
            except (TypeError, ValueError):
                if str(rule_threshold) == str(threshold):
                    return rule, rule_threshold
    return None


def _path_match(path: str, pattern: str, case_sensitive: bool = False) -> bool:
    """Match path against pattern with gitignore-style `**` semantics.

    Standard fnmatch supplemented with: a `**/` prefix in the pattern matches
    zero OR more leading directory components. So `**/.env*` matches both
    `.env` (root-level) and `config/.env` (nested) — same semantics gitignore
    + tsconfig + ESLint configs use.

    Closes Finding #7 (sprint 2026-05-15): rule 01 secrets-protection
    patterns are written `**/.env*`, `**/credentials*` etc. Pure fnmatch
    required `**` to consume AT LEAST one directory, so root-level secrets
    files (`.env`, `credentials.json` at project root) silently bypassed
    the rule. This matcher closes that gap.

    M17 (fixpass 2026-07-01): matching is now platform-deterministic.
    fnmatch.fnmatch normcases both arguments — on Windows that silently
    forced case-insensitivity (flag was a no-op) and flipped separators,
    while on the Linux server the flag was live and backslash client paths
    could never match /-style patterns. Now: separators normalized to `/`
    explicitly, case folded only when case_sensitive is False, matching via
    fnmatchcase on every platform. Default flipped to case-INSENSITIVE —
    the fail-safe direction for a protection engine evaluating paths from
    case-insensitive client filesystems — and now matches the wrapper's
    default (cross-side parity).
    """
    import fnmatch

    hay = path.replace("\\", "/")
    needle = pattern.replace("\\", "/")
    if not case_sensitive:
        hay = hay.lower()
        needle = needle.lower()

    # Pure pattern match (no normcase — see M17 note above).
    if fnmatch.fnmatchcase(hay, needle):
        return True

    # Gitignore-style `**/` prefix: also match the pattern with the prefix
    # stripped, which covers the zero-leading-directory case (root-level
    # files that pure fnmatch missed).
    if needle.startswith("**/"):
        if fnmatch.fnmatchcase(hay, needle[3:]):
            return True

    return False




def _match_operation_entry(entry: dict, operation: str | None) -> bool:
    """Check a PURE `operation:` match_any entry against the call's
    classified operation (C02, 2026-08-03).

    Rules 09/10 are written entirely in bare `operation:` entries
    (`gmail_send`, `stripe_charge`, `oauth_authorize`, ...). Until this
    helper existed no matcher consumed them — the schema declared the key
    usable, the loop checked only command/file patterns, and two lethal
    rules were documentation (audit C02; a Gmail-send and a Stripe-payment
    payload both cleared the real hook with exit 0).

    Scope is deliberately narrow: an entry that ALSO carries another usable
    matcher key belongs to that matcher — an operation-GATED file_pattern
    entry (rules 01/03) keeps the bypass-when-unclassified semantics, and
    signal entries stay on the dispatch surface. Annotation keys
    (`platforms`, `check`) refine the CLASSIFIER's vocabulary rather than
    this equality test (see the wrapper's _classify_semantic_operation and
    audits/20260803_send_and_permission_operation_inventory.md).

    `operation=None` (unclassifiable call) does NOT fire — same strictness
    as `_match_all_satisfied`. This family guards NAMED semantic
    operations; treating unknown as matching would fire two lethal rules
    on every unclassified tool call.

    Mirrored verbatim from wrapper/lib/innate_rules.py — drift-parity
    lockstep tier (test_pillar_wrapper_server_drift_parity).
    """
    required = entry.get("operation")
    if not required or not operation:
        return False
    if any(
        k in entry
        for k in ("command_pattern", "file_pattern", "signal", "phrase", "context")
    ):
        return False
    return _operation_matches(required, operation)


def _operation_matches(required_op: Any, operation: str | None) -> bool:
    """True when `operation` satisfies an entry's `operation:` requirement.

    W2-D (audit H-08): the requirement may be a single string (the legacy
    form, `operation: write`) or a LIST of alternatives
    (`operation: [write, edit]`) for entries that must gate several
    mutating ops at once — rule 01 secrets-protection gates write AND
    edit. Membership is exact-string. None never satisfies a non-empty
    requirement; callers decide separately whether a None/unknown
    operation bypasses their gate (the match_any file gate bypasses
    fail-safe; match_all stays strict). Mirrored verbatim wrapper-side
    (lib/innate_rules.py) — drift-parity lockstep tier.
    """
    if isinstance(required_op, (list, tuple, set)):
        return operation in required_op
    return required_op == operation


def _match_all_satisfied(
    entries: list[Any],
    operation: str | None,
    context_flags: set[str],
) -> bool:
    """Return True iff EVERY entry in `entries` is satisfied by the
    supplied operation + context_flags.

    Each entry is a dict that may carry an `operation:` key (a string the
    supplied `operation` must equal, or a list it must be a member of —
    W2-D H-08) and/or a `context:` key (must be present in
    `context_flags`). An entry with neither key is treated as vacuously
    true (defensive — production rules always specify at least one).
    Unlike the match_any file gate, an op-gated entry here is NOT
    satisfied by operation=None (strict form, unchanged).

    Closes Finding #8 (sprint 2026-05-15): rule 04 canonical-verification
    uses match_all + context flags computed by the wrapper from the
    operator's filesystem. The server never inspects the filesystem
    directly; the wrapper sends the boolean flags in `question.details`.

    Round-7 type-guard (defense in depth, not the primary fix — the primary
    fix is `_match_all_entry_problem` rejecting this at validation time so a
    malformed entry never reaches the working set): a malformed entry that
    somehow reaches this runtime (e.g. `context: []`, unhashable against a
    `set`) makes the WHOLE conjunction UNSATISFIED rather than raising
    `TypeError` or matching vacuously. Mirrored verbatim from
    wrapper/lib/innate_rules.py — drift-parity lockstep tier.
    """
    if not entries:
        return False
    for entry in entries:
        if not isinstance(entry, dict) or _match_all_entry_problem(entry) is not None:
            return False
        required_op = entry.get("operation")
        if required_op is not None and not _operation_matches(required_op, operation):
            return False
        required_ctx = entry.get("context")
        if required_ctx is not None and required_ctx not in context_flags:
            return False
    return True


def _match_pre_tool_call_to_rule(
    rules: list[dict[str, Any]],
    tool_name: str,
    command: str | None,
    file_path: str | None,
    operation: str | None,
    context_flags: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return the first innate rule whose trigger matches the
    pre-tool-call attributes. None if no rule fires.

    Two trigger families are evaluated per rule:
      - `match_any`: any one entry firing is enough. Entries match via
        command_pattern (regex on `command`) or file_pattern (glob on
        `file_path` with gitignore-style `**` semantics, optionally
        gated on `operation`). Mirrors plugin/lib/innate_rule_engine.py.
      - `match_all`: every entry must fire. Entries match via `operation`
        equality and/or `context` flag presence in `context_flags`.
        Rule 04 canonical-verification uses this family — the wrapper
        computes the flags from the operator's filesystem and passes
        them via question.details.

    A rule fires if EITHER family matches. `match_any` is evaluated first
    because rules 01/02/etc. are higher-frequency.
    """
    import re

    flags_set = context_flags or set()

    # Round-10 (operator decision 2026-07-24): redaction DELETED; match the
    # FULL command text, in parity with wrapper lib/innate_rules.py check().
    # Destructive patterns that appear only as DATA (commit-message prose, a
    # grep for the pattern) now over-confirm — the accepted, fail-closed cost
    # of removing the bypass class rounds 7-9 lived inside. See
    # audits/20260724_task0_redaction_falsepositive_measurement.md.
    command_for_match = command

    for rule in rules:
        # Sending is governed by the host's authorization policy. The memory
        # engine no longer provides the retired mandatory send guard.
        if rule.get("id") == "innate-09":
            continue
        trigger = rule.get("trigger", {})
        match_any = trigger.get("match_any", [])
        match_all = trigger.get("match_all", [])

        # match_any family (existing path)
        if isinstance(match_any, list):
            for entry in match_any:
                if not isinstance(entry, dict):
                    continue

                # command_pattern check (matched against the commit-message-
                # redacted command so message-body content can't trigger).
                cmd_pat = entry.get("command_pattern")
                if cmd_pat and command_for_match:
                    re_flags = 0 if entry.get("case_sensitive", True) else re.IGNORECASE
                    try:
                        if re.search(cmd_pat, command_for_match, re_flags):
                            return rule
                    except re.error as exc:
                        # R2 (2026-07-20): was `pass`. A pattern that does not
                        # compile made its rule silently unable to fire, with
                        # no record anywhere — the guard was off and the log
                        # was clean. Rule 02's patterns are additionally
                        # validated at load time (see validate_destructive_rule),
                        # so this path is the backstop for the other eleven.
                        logger.error(
                            "innate rule %s has an uncompilable command_pattern "
                            "%r (%s); that matcher cannot fire",
                            rule.get("id"), cmd_pat, exc,
                        )
                        _LOAD_DIAGNOSTICS.set(
                            f"{rule.get('id')} has an uncompilable "
                            f"command_pattern {cmd_pat!r} ({exc})"
                        )
                    continue

                # file_pattern check (optionally gated on operation).
                # The gate is BYPASSED when `operation` is falsy (None) —
                # fail-safe: a tool the caller could not classify still
                # fires an op-gated rule on a path match. W2-D (H-08/H-09):
                # `operation:` may be a string or a list of alternatives,
                # and known read-only tools are classified as "read" (a
                # truthy op that mutating-op gates correctly exclude).
                file_pat = entry.get("file_pattern")
                if file_pat and file_path:
                    required_op = entry.get("operation")
                    if required_op and operation and not _operation_matches(
                        required_op, operation
                    ):
                        continue
                    # M17: file patterns default case-INSENSITIVE (parity
                    # with wrapper; fail-safe for Windows client paths).
                    # Command regexes above keep their case-sensitive default.
                    case_sensitive = entry.get("case_sensitive", False)
                    if _path_match(file_path, file_pat, case_sensitive=case_sensitive):
                        # DELIBERATE ASYMMETRY — do NOT "fix" this by adding a
                        # stat. `target_must_exist` (2026-07-30) is recognized
                        # by the schema above so innate-01 validates on both
                        # sides, but it is evaluated ONLY in the wrapper, which
                        # is the process running on the machine that owns the
                        # file. `file_path` here is a string describing a path
                        # on the CUSTOMER's disk; stat-ing it would interrogate
                        # this server's filesystem and answer a question about
                        # an entirely different machine — almost always "not
                        # found", which would silently disarm a lethal rule for
                        # every install at once.
                        #
                        # Ignoring the flag is the fail-safe direction: the
                        # server keeps firing on creates the wrapper now allows,
                        # so this side is never LESS protective than the side
                        # that can actually see the file. Pinned by
                        # tests/test_innate01_server_ignores_target_must_exist.py
                        return rule
                    continue

                # C02 (2026-08-03): pure `operation:` entries — rules
                # 09/10's send/grant/payment vocabulary, previously consumed
                # by no matcher anywhere.
                if _match_operation_entry(entry, operation):
                    return rule

        # match_all family (Finding #8 — rule 04 path)
        if isinstance(match_all, list) and match_all:
            if _match_all_satisfied(match_all, operation, flags_set):
                return rule

    return None


def _template_for_threshold(rule: dict[str, Any], threshold: Any) -> str:
    """Pick the rule's threshold-specific template, with fallbacks.

    Mirrors plugin/lib/innate_rule_engine.py:
      templates.get(threshold) or templates.get(str(threshold)) → description → generic
    """
    response = rule.get("response", {})
    templates = response.get("templates", {})
    if isinstance(templates, dict) and threshold is not None:
        if threshold in templates:
            return str(templates[threshold])
        if str(threshold) in templates:
            return str(templates[str(threshold)])
    return str(rule.get("description") or f"Innate rule {rule.get('id', '?')} fired")


# ---------- MCP tool ----------


def _effective_rules():
    """Owner preferences apply only to the bundled local engine, not hosted users."""
    from ..runtime_context import local_execution
    rules = _get_rules()
    if not local_execution.get():
        return rules
    try:
        from guard_settings import enabled
        return [r for r in rules if enabled(r.get('id', '')) is not False]
    except Exception:
        logger.warning('Owner guard settings unavailable; enabled defaults apply')
        return rules


def innate_enforcer_decision(excerpt, question):
    from ..runtime_context import local_execution
    if not local_execution.get():
        return _innate_enforcer_decision(excerpt, question)
    from contextlib import nullcontext
    try:
        from guard_settings import policy_scope
    except Exception:
        policy_scope = nullcontext
    with policy_scope():
        return _innate_enforcer_decision(excerpt, question)


def _innate_enforcer_decision(
    excerpt: ExcerptPayload,
    question: QuestionPayload,
) -> PillarToolResponse:
    """Pure decision function for the innate-enforcer pillar.

    Exposed at module scope so tests can drive it without the MCP/FastMCP
    session machinery. `register_tools` below wraps this into an MCP tool.

    Sets `current_pillar_id` for the duration of the call so the request-
    logging middleware can attribute the request to this pillar in the
    `request_log.pillar_id` column.
    """
    token = current_pillar_id.set(PILLAR_ID)
    record_pillar_invocation(PILLAR_ID)
    try:
        if question.question_type == "list_rules":
            return _list_rules_response()
        if question.question_type == "check_pre_tool_call":
            return _check_pre_tool_call_response(question)
        if question.question_type != "check_signal":
            return _no_match(
                decision_class="unknown_question_type",
                detail=f"unknown question_type {question.question_type!r}",
            )

        signal_type = question.details.get("signal_type")
        threshold = question.details.get("threshold")
        if not signal_type:
            return _no_match(
                decision_class="missing_signal_type",
                detail="question.details.signal_type is required",
            )

        match = _match_signal_to_rule(_effective_rules(), signal_type, threshold)
        if match is None:
            return _no_match(decision_class="no_rule_matched")

        rule, matched_threshold = match
        template = _template_for_threshold(rule, matched_threshold)
        return PillarToolResponse(
            decision=DecisionPayload(
                action="fire_red_box",
                body=template,
                details={
                    "rule_id": rule.get("id"),
                    "severity": rule.get("severity"),
                    "threshold": matched_threshold,
                },
            ),
            stats=StatsPayload(
                pillar_id=PILLAR_ID,
                decision_class="fired",
                fingerprint=f"{rule.get('id')}:{matched_threshold}",
            ),
        )
    finally:
        current_pillar_id.reset(token)


def register_tools(server: FastMCP) -> None:
    """Register innate-enforcer MCP tool(s) on `server`."""

    @server.tool(
        name="innate_enforcer_check",
        description=(
            "Check whether a signal matches any hardwired innate guardrail "
            "rule. Returns the matching rule's threshold-specific red-box "
            "template when matched (decision.action='fire_red_box'), or "
            "decision.action='no_match' when no rule fires."
        ),
    )
    def innate_enforcer_check(
        excerpt: ExcerptPayload,
        question: QuestionPayload,
    ) -> PillarToolResponse:
        return innate_enforcer_decision(excerpt, question)


def _list_rules_response() -> PillarToolResponse:
    summaries = [
        {
            "id": r.get("id"),
            "name": r.get("name", ""),
            "severity": r.get("severity", "unknown"),
        }
        for r in _get_rules()
    ]
    return PillarToolResponse(
        decision=DecisionPayload(
            action="list",
            details={"rules": summaries, "count": len(summaries)},
        ),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class="listed"),
    )


def _check_pre_tool_call_response(question: QuestionPayload) -> PillarToolResponse:
    """Handle `check_pre_tool_call` question_type.

    question.details:
      tool_name:  str  — the Claude Code tool about to fire (Bash, Write, Edit, ...)
      command:    str  — bash command text (when tool_name == "Bash")
      file_path:  str  — target file path (when tool_name in {"Write","Edit","NotebookEdit"})
      operation:  str  — "write" | "edit" | "read" | None (inferred from tool_name if omitted)

    Returns fire_red_box when a command_pattern or file_pattern rule matches.
    """
    details = question.details or {}
    tool_name = details.get("tool_name", "")
    command = details.get("command")
    file_path = details.get("file_path")
    operation = details.get("operation")

    if not operation and tool_name:
        tn_lower = tool_name.lower()
        if tn_lower == "bash":
            operation = "execute"
        elif tn_lower in ("write",):
            operation = "write"
        elif tn_lower in ("edit", "notebookedit", "multiedit"):
            # multiedit added (arbiter reconciliation, Wave-2): mirrors the
            # wrapper _op_map so the byte-identical rules 01/03/04 evaluate a
            # MultiEdit identically on both sides (rules 01/03 accept
            # [write, edit]; rule 04's match_all needs operation == edit).
            operation = "edit"
        elif tn_lower in ("read", "grep", "glob", "notebookread"):
            # W2-D (audit H-09): known read-only tools classify as a
            # first-class `read` — mirrors the wrapper's _op_map so a
            # mutating-op-gated rule never fires on a read. Tools absent
            # from this chain stay operation=None and keep firing
            # fail-safe.
            operation = "read"

    # context_flags carry wrapper-computed filesystem facts (rule 04 uses
    # `file_being_edited_has_sibling_folders_with_similar_files` +
    # `no_CANONICAL_txt_in_current_folder_or_any_sibling`). Accept either
    # list or set in the wire format; coerce to a set for membership tests.
    raw_flags = details.get("context_flags") or []
    if isinstance(raw_flags, (list, tuple, set)):
        context_flags = {str(f) for f in raw_flags}
    else:
        context_flags = set()

    rule = _match_pre_tool_call_to_rule(
        _effective_rules(),
        tool_name=tool_name,
        command=command,
        file_path=file_path,
        operation=operation,
        context_flags=context_flags,
    )
    if rule is None:
        return _no_match(decision_class="no_rule_matched")

    response = rule.get("response", {})
    template = response.get("message") or str(
        rule.get("description") or f"Innate rule {rule.get('id', '?')} fired"
    )
    # Substitute template placeholders. Set covers every variable referenced
    # across the production rule library — short_command + reversibility came
    # from rule 02's template, missing them caused KeyError → raw-template
    # fallback with literal `{command}` etc. showing in operator-facing red
    # boxes (Finding #10, sprint 2026-05-15).
    cmd_text = command or ""
    short_cmd = cmd_text if len(cmd_text) <= 60 else cmd_text[:57] + "..."

    # sibling_list rendering: wrapper sends a list of sibling folder names
    # alongside context_flags when rule 04 conditions are met. Render as a
    # comma-separated list in the operator-facing template.
    raw_siblings = details.get("sibling_list")
    if isinstance(raw_siblings, (list, tuple)):
        sibling_rendered = ", ".join(str(s) for s in raw_siblings if s)
    elif isinstance(raw_siblings, str):
        sibling_rendered = raw_siblings
    else:
        sibling_rendered = ""
    if not sibling_rendered:
        sibling_rendered = "(filesystem context not yet provided)"

    # today_date for rule 04's secondary_response_on_proceed (write_canonical_marker).
    from datetime import date as _date
    today_str = _date.today().isoformat()

    substitutions = {
        "file_path": file_path or "",
        "filename": (Path(file_path).name if file_path else ""),
        "command": cmd_text,
        "target": file_path or cmd_text or "(unspecified)",
        "short_command": short_cmd or "this command",
        "reversibility": rule.get("reversibility", "hard to undo"),
        "tool_name": tool_name or "",
        "operation": operation or "",
        # C02 (2026-08-03): rules 09/10's templates say {operation_type} —
        # render the classified operation there rather than a literal
        # placeholder. Synced with wrapper _format_match (PATCH-160 tier).
        "operation_type": operation or "(unclassified operation)",
        "sibling_list": sibling_rendered,
        "today_date": today_str,
    }

    class _SafeDict(dict):
        """Falls back to literal {key} for any missing placeholder so the
        template renders something readable instead of raising KeyError."""

        def __missing__(self, key):
            return "{" + key + "}"

    try:
        rendered = template.format_map(_SafeDict(substitutions))
    except (ValueError, IndexError):
        rendered = template

    return PillarToolResponse(
        decision=DecisionPayload(
            action=response.get("action", "fire_red_box"),
            body=rendered,
            details={
                "rule_id": rule.get("id"),
                "severity": rule.get("severity"),
                # Always None for the shipped ruleset since 2026-07-30, and
                # kept only so the response shape does not change under
                # existing clients. This was the one call site that read
                # `exception_phrase_template` anywhere — a PASSTHROUGH, never a
                # consumer: no code matched an operator message against it, so
                # it published the fictional confirmation phrase to API clients
                # exactly as the red box published it to operators. Removing
                # the key from the rules removed the fiction at the source. Do
                # not repopulate this without wiring a reader first.
                "exception_phrase_template": rule.get("exception_phrase_template"),
            },
        ),
        stats=StatsPayload(
            pillar_id=PILLAR_ID,
            decision_class="fired",
            fingerprint=f"{rule.get('id')}:pre_tool_call",
        ),
    )


def _no_match(*, decision_class: str, detail: str | None = None) -> PillarToolResponse:
    details: dict[str, Any] = {}
    if detail:
        details["reason"] = detail
    return PillarToolResponse(
        decision=DecisionPayload(action="no_match", details=details),
        stats=StatsPayload(pillar_id=PILLAR_ID, decision_class=decision_class),
    )
