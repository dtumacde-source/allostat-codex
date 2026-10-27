"""Wrapper-local innate-rule evaluation — PATCH-143 v1.2.0.

Ported from server-side `allostat_server/pillars/innate_enforcer.py` to
move refusal-teeth evaluation client-side. The architectural reason:
the server round-trip loses races with tool execution under any
latency (Cloudflare blip, VPS load, network jitter, cert renewal,
etc.). Even if the wrapper's `pre-tool-use.py` did `return 2` on a
server-returned `fire_red_box` decision, the decision arrives AFTER
Claude Code has already proceeded to execute the tool in many real
sessions.

The corrected design (audit Phase 4 A1, operator correction
2026-05-21): wrapper-local rule evaluation happens BEFORE the
server dispatch round-trip. If a rule fires, the wrapper writes the
red-box message to stderr and `return 2`s — Claude Code interprets
exit-2 from PreToolUse as a hard tool refusal. The tool never runs.
No race.

Server-side `innate_enforcer.py` keeps evaluating the same rules
(for now) for telemetry and pattern-observer correlation. PATCH-145
in a later release strips the server-side blocking pretense and
makes the server path observation-only.

This module is a faithful port of the server's matching logic:
  - YAML rule loader (mirrors `_load_rules`)
  - Gitignore-style path matcher (mirrors `_path_match`)
  - match_any iteration over command_pattern + file_pattern entries
    against the FULL command text (round 10, 2026-07-24: the commit-message /
    inertness redaction layer was deleted — the guard matches raw text; see
    docs/round10_fulltext_matching.md)
  - match_all evaluation with operation + context-flags

The two divergences from server-side:
  1. Module loads YAML from `<plugin>/rules/innate/` instead of a
     server-deploy-relative path. The bundle ships the YAML files.
  2. Template substitution uses a safe-replace (unknown placeholders
     stay literally in the output) instead of `str.format()` (which
     would raise KeyError and crash the hook). Server-side uses a
     more elaborate template system; this is a deliberate
     simplification.

Audit ref:
  - v1.1.1 audit A1 finding (operator-local audit doc; not shipped).
  - Operator correction 2026-05-21: "the agent didn't refuse the
    command to stop the destructive edit. the command came too late
    and allostat lost the race."
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------- rule loading ----------


def _bundled_rules_dir() -> Path | None:
    """Locate the bundled `rules/innate/` directory.

    Walks up one level from this file's location (`<plugin>/lib/`) to find
    the sibling `<plugin>/rules/innate/` directory. The bundle's
    `make_bundle.py` ships rules/ alongside lib/, so installed plugins find
    it at this relative path.

    Returns None when not present — the hook silently skips innate-rule
    evaluation (matches server-side fallback). The dispatch round-trip
    still happens, so users see degraded-but-functional behavior.
    """
    here = Path(__file__).resolve()
    candidate = here.parent.parent / "rules" / "innate"
    if candidate.is_dir():
        return candidate
    return None


def _load_rules(rules_dir: Path) -> list[dict[str, Any]]:
    """Load every non-LEGACY rule from rules_dir. Failures skip silently.

    Pre-release item 55: vendored `*.json` rules load FIRST — stdlib
    json, no PyYAML dependency. `make_bundle._compile_innate_rules_to_json`
    writes them adjacent to the YAML at bundle time, so on stock Python
    without PyYAML the refusal teeth stay armed. `*.yaml` is the dev /
    pre-bundle fallback (YAML stays the source of truth; the JSON is
    regenerated from it on every bundle build).

    Faithfully mirrors server-side `_load_rules`. Each rule gets an `id`
    derived from the filename stem if the file doesn't carry an explicit
    `id` field.
    """
    # Safety-engine completeness (deep-audit 2026-07-02): a PARTIAL JSON bundle
    # (some vendored *.json corrupt or missing vs the *.yaml source of truth)
    # must NOT arm only the subset that happened to parse — that silently runs
    # the destructive-action guards with fewer rules than intended. Fall back
    # to the complete YAML source unless JSON covers every expected rule.
    json_rules = _load_rules_from_json(rules_dir)
    expected = _expected_rule_stems(rules_dir)
    # Completeness is judged on FILE STEMS (not rule ids — a rule may carry an
    # explicit id != stem): a JSON bundle is complete only if every expected
    # rule file has a parseable JSON counterpart.
    json_stems = _parseable_stems(rules_dir, "*.json")
    if expected and expected <= json_stems:
        return json_rules  # JSON bundle is complete — use it

    # Incomplete/empty JSON. Arbitration is by AUTHORITY, not by count.
    #
    # This used to read:
    #     return yaml_rules if len(yaml_rules) >= len(json_rules) else json_rules
    #
    # which decided the constitution by counting files. Two ways that fails:
    #
    #   1. ATTACK. Drop 13 junk `.json` files into rules/innate/. They inflate
    #      `expected` (it is the union of both globs) so the completeness gate
    #      above fails, and then `12 >= 13` is False — so all twelve real YAML
    #      rules are DISCARDED, never even parsed. The junk is then dropped by
    #      the manifest gate, leaving a working set of ZERO. The junk needs no
    #      valid content and no manifest id; the count alone does the work.
    #
    #   2. IT DEFEATED ITS OWN STATED PURPOSE. On stock Python without PyYAML
    #      and a PARTIAL bundle, `_load_rules_from_yaml` returns [], so
    #      `0 >= 8` is False and the eight-rule partial JSON set was armed —
    #      exactly the "must NOT arm only the subset that happened to parse"
    #      the comment above forbids.
    #
    # YAML is the source of truth and the JSON is regenerated from it on every
    # bundle build (see this function's docstring). So once JSON is known
    # incomplete, the question is not "which is bigger" but "is there a source
    # of truth present at all". If YAML files are DECLARED, they decide —
    # even if PyYAML cannot parse them, in which case the result is an empty
    # ruleset that reports itself degraded and arms the hardcoded destructive
    # fallback. That is fail-safe. Silently running an arbitrary subset is not.
    yaml_declared = {
        path.stem
        for path in rules_dir.glob("*.yaml")
        if "_LEGACY_" not in path.name
    }
    if yaml_declared:
        return _load_rules_from_yaml(rules_dir)
    return json_rules  # no YAML at all — the vendored JSON is all there is


def _expected_rule_stems(rules_dir: Path) -> set[str]:
    """Union of non-LEGACY rule stems across both *.json and *.yaml, i.e. the
    full set of rules that SHOULD be armed regardless of which format supplies
    each one."""
    stems: set[str] = set()
    for pattern in ("*.json", "*.yaml"):
        for path in rules_dir.glob(pattern):
            if "_LEGACY_" in path.name:
                continue
            stems.add(path.stem)
    return stems


def _parseable_stems(rules_dir: Path, pattern: str) -> set[str]:
    """Stems of non-LEGACY files matching `pattern` that parse as a JSON dict
    (i.e. actually contribute a usable rule)."""
    stems: set[str] = set()
    for path in rules_dir.glob(pattern):
        if "_LEGACY_" in path.name:
            continue
        try:
            if isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                stems.add(path.stem)
        except (OSError, json.JSONDecodeError):
            continue
    return stems


def _load_rules_from_json(rules_dir: Path) -> list[dict[str, Any]]:
    """Load vendored non-LEGACY `*.json` rules (stdlib-only, no PyYAML).

    Returns [] when no parseable JSON rules exist — caller falls back to
    the YAML source of truth.
    """
    rules: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.json")):
        if "_LEGACY_" in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # C01 (2026-08-03): captured per-file, not just skipped — the
            # expected-minus-seen diff in _build_working_set names the id
            # this file would have supplied and needs the reason.
            _LOAD_FAILURES.append({
                "file": path.name,
                "problem": f"could not be parsed ({type(exc).__name__})",
            })
            continue
        if not isinstance(data, dict):
            _LOAD_FAILURES.append({
                "file": path.name,
                "problem": "parsed but is not a mapping",
            })
            continue
        if "id" not in data:
            data["id"] = path.stem
        rules.append(data)
    return rules


def _load_rules_from_yaml(rules_dir: Path) -> list[dict[str, Any]]:
    """Load non-LEGACY `*.yaml` rules. Requires PyYAML; returns [] when
    PyYAML is absent (item 55: SessionStart surfaces that degradation
    loudly via `rules_available`)."""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; innate rules cannot load")
        _LOAD_DIAGNOSTICS.set("PyYAML is not installed, so no rule file can be parsed")
        # C01: loader-wide condition — answers for every expected id below.
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
            # Recorded, not just skipped. A silent `continue` here is how the
            # destructive guard could vanish without anything saying so.
            logger.warning("innate rule %s could not be loaded: %s", path.name, exc)
            _LOAD_DIAGNOSTICS.set(f"{path.name} could not be parsed ({type(exc).__name__})")
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
        rules.append(data)
    return rules


_RULES_CACHE: list[dict[str, Any]] | None = None

# Round-7: RAW loaded rules (every file that parsed, before the manifest-
# identity gate) cached separately from `_RULES_CACHE` (the GATED working
# set _get_rules() returns). `ruleset_degraded()` needs the raw list — a
# shadow file, a duplicate, or a hash-mismatched edit is exactly what the
# working-set gate REMOVES, so reporting on the gated set could never see
# the thing it is supposed to name.
# Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep tier.
_RAW_RULES_CACHE: list[dict[str, Any]] | None = None

# 0B.4. The one rule whose absence must NOT be silent.
#
# Every failure path in the loaders above degrades quietly: PyYAML missing
# returns [], a corrupted file is skipped by `continue`, a non-dict document is
# skipped, and a missing rules directory yields []. Every one of those ends with
# `match_pre_tool_call` returning None, which the hook reads as "no rule fired"
# and allows the command. So a single unparseable YAML file silently removes the
# destructive-command guard, and nothing anywhere says so.
#
# The operator's directive is that a corrupted ruleset must REFUSE destructive
# actions while leaving everything else alone. Refusing everything would break
# every session over a typo; refusing nothing is what we had. So: destructive
# fails CLOSED against a small hardcoded matcher, and every other rule goes
# INERT.
DESTRUCTIVE_RULE_ID = "innate-02"

# R3 (2026-07-20) — THE MINIMUM ENFORCEABLE SCHEMA FOR innate-02.
#
# 0B.4 above answered "is the destructive rule PRESENT?" by looking for its id.
# Codex showed that presence is not validity: a rule file that is valid YAML,
# carries `id: innate-02`, and has `trigger: {}` loads cleanly, satisfies the
# health check, matches nothing, and leaves the fallback disarmed because the
# id was found. Run against the real hook with `rm -rf`:
#
#     HOOK_EXIT=0    DEGRADED=(False, '')    MATCH=None
#
# All 13 of the original 0B.4 tests passed, because every one of them corrupts
# the file in a way that fails to PARSE — syntax damage, a missing file, a YAML
# list, a missing directory. None covered the shape that parses. A truncated
# write, a partial sync, or a bad merge produces exactly that shape.
#
# So the question the health check asks is now "is it ENFORCEABLE?", and these
# constants are the answer's definition. Severity and response action are
# included because a rule that matches `rm -rf` and then responds `log_only` is
# not a guard either — it detects and permits.
_ENFORCEABLE_DESTRUCTIVE_SEVERITIES = frozenset({"lethal", "critical"})
_BLOCKING_RESPONSE_ACTIONS = frozenset({"hard_pause"})

# P1-7 (2026-07-20 audit) — THE GENERAL CONTRACT, for all twelve rules.
#
# R3 gave innate-02 a load-time contract. The audit found that the other eleven
# rules — three of them lethal — still armed on the presence of an `id` alone,
# and that the health signal SessionStart shows the operator was a COUNT
# (`rules_available` = "at least one rule loaded"), not a coverage check. So a
# truncated write that leaves innate-09 present-but-empty loads cleanly, the
# count stays positive, and the operator is told the safety net is whole while a
# lethal rule guards nothing.
#
# This is the MINIMUM every rule needs to be able to fire and to act. It is
# deliberately permissive about SHAPE, because the twelve rules legitimately
# differ — some match a command regex, some a file glob, some a phrase or a
# signal, one uses match_all only. Rejecting a real rule would weaken protection
# by flagging a working guard, so the contract only catches what unambiguously
# cannot fire (empty/absent trigger, no matchable entry, a command regex that
# does not compile or is corrupt) or cannot act (missing/blank response action,
# unrecognised severity). `test_every_shipped_rule_is_enforceable` pins that.
_KNOWN_SEVERITIES = frozenset({"lethal", "critical", "high", "medium", "low"})

# The innate rules are the fixed constitutional layer — "innate" means hardwired,
# not user-extensible — so the expected set is a manifest, not a count.
# `test_the_manifest_matches_the_shipped_rules` pins it to disk, so adding or
# removing a rule is a deliberate edit here and never a silent drift.
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
    """SHA-256 of the rule's NORMALIZED content — the loaded mapping (after
    the loader's id-from-stem default), serialized as sort-keys compact JSON.
    Normalization makes the YAML source and its vendored JSON compilation
    hash identically. Advisor correction 2 (2026-07-23): ID-set equality
    catches ADDING a shadow rule but not EDITING the canonical one; shadowing,
    duplication, and editing are three routes to 'a rule that is not the real
    rule', and the content pin closes all three.

    `default=str` (hash-stability note): innate-11/12 carry an unquoted YAML
    date scalar (`added_at: 2026-05-02`), which `yaml.safe_load` parses as a
    `datetime.date` object — not a string, and not natively JSON-serializable
    (`json.dumps` raises `TypeError` without a `default`). The vendored JSON
    compiler (`make_bundle.py::_compile_innate_rules_to_json`) already renders
    that same field as `default=str`, i.e. `str(date(2026, 5, 2))` ==
    `"2026-05-02"`. Using the same `default=str` here makes the YAML-loaded
    date object and the JSON-loaded string of that date serialize to the
    IDENTICAL bytes, so the two source formats hash identically as intended —
    omitting it would both crash on load and break YAML/JSON parity.
    Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep tier."""
    return hashlib.sha256(
        json.dumps(_canonicalize_keys(rule), sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# Generated by scripts/gen_innate_manifest.py from the shipped rules dir.
# Regenerate ONLY as part of a deliberate constitution change, in the same
# commit as the rule edit. Pinned + mirrored server-side; parity-tested.
EXPECTED_INNATE_RULE_HASHES: dict[str, str] = {
    # 2026-08-28 remediation: local prompt/JSON authority is unsupported until
    # the platform provides a same-user-executor-proof capability boundary.
    "innate-01": "429230ae9a3158fcf9a7a732e160be07d92236796a4c70a231dceb53f74eb9ab",
    # 2026-08-01: re-pinned for the destructive-operation registry (advisor
    # H-03) — find -delete/shred/dd/truncate, interleaved-flag git deletes,
    # and named aws/gh/wrangler/stripe/redis destructive operations.
    "innate-02": "f0a0bb61060c579bd4eb92fbb1afeafc82abb798ac7e3ac4b04ae6b8bbf990b7",
    "innate-03": "1ff64c569af6b8d692524125aef755e7a9c0ae7d585aa4873028f6ac711d13d1",
    "innate-04": "8322b4dc89931e8a803801b804417a3a852f6ceab9a184eadf07a752d24aa5b4",
    "innate-05": "d657f3473e30bb2c37c01697d087cd0c8d34c0587ff3f4baba15296b07725afd",
    "innate-06": "c4f114cf62742061e333b63292cd7327e20eac5b597cf930c120ef9a735009d9",
    "innate-07": "793488571903147d9b951bab5aa61705cb74265076681454103ce75a77e58c50",
    "innate-08": "bee935b1b2eb8f27b958c4b970bb6e7441dce7c39a7f7ddfc287cea1c70d6d76",
    "innate-09": "2f13b4f770be176ffdc5482755ef9805d13ebd8bbf18690c5646f29bc49799cb",
    # 2026-08-03: re-pinned for F02 — innate-10's name/description narrowed to
    # the shapes the classifier actually recognizes (operator ruling: narrow
    # the promise rather than chase universal coverage), and two trigger
    # operations removed that no classifier branch could ever emit
    # (file_share_change, dashboard_access_modify). Deliberate constitution
    # change; mirrored server-side in the same commit.
    "innate-10": "5813fb7ac4f3ca17cdd37b29a104866a10d6aee377620b4ca5650b58316fe72c",
    "innate-11": "87f750f50d575bd8b5fe6feb8223d83c4c0c953dcf35da25d1929115ad30d2d9",
    "innate-12": "e27b1a2a71b4a7915ac79cda6d20bde6be0b7f2e39c6077fc2930889a9b5c563",
}


def _uncompilable_command_patterns(trigger: dict) -> list[str]:
    """Reasons any `command_pattern` in `trigger.match_any` cannot match.

    Compiling is necessary and not sufficient: a YAML double-quoted scalar
    interprets backslash escapes, so `"\\bX\\b"` written with one backslash
    becomes literal control characters that compile and match nothing (the
    mistake made and caught during R3b). Both cases are reported.
    """
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


# Round-7 inversion (advisor 2026-07-23): the schema each runtime consumer
# honors, one entry per field the matcher may carry. Validity means EVERY
# supplied field proves out against this schema — not "one field is usable".
# `description` is the original known-inert annotation.
#
# Escalation-clause schema completion (controller resolution, 2026-07-23):
# running this schema's first cut against the shipped YAML found FIVE more
# annotation keys that are load-bearing content for the signal-dispatch
# consumer (the hypothalamic-axis regulator), not for this module's own
# matcher, on 7 of the 12 shipped rules:
#   innate-03 (action), innate-05 (threshold, x6), innate-06 (check, x2),
#   innate-07 (action), innate-08 (action, x3), innate-09 (platforms, check),
#   innate-12 (condition).
# Per the plan's own escalation clause ("surface it, then extend the schema
# deliberately in the same commit with a test pinning it") these five join
# `description` as recognized-and-validated fields — the SAME evidence-based
# widening round 6 already did for `signal`/`phrase` (a rule is not
# "unenforceable" because it is legitimately not a Bash/file rule). The
# schema stays CLOSED: this is now the COMPLETE key set derived from what
# ships; anything else still rejects. See
# test_the_extended_schema_is_complete_and_still_closed, which pins each
# newly admitted key's valid AND invalid shape and re-confirms the schema is
# closed against a genuinely unknown key.
#
# `description` is the only key with no type at all beyond "a string" (pure
# comment). Everything else validates against the TYPE actually observed in
# the shipped YAML for that key — a rule is not "proven" merely because the
# key is recognized.
# Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep tier.
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
    which excused a malformed sibling the runtime went on to consume
    ({file_pattern: "**/.env*", operation: {bad: shape}} half-ran). A rule is
    the unit of trust; a partially corrupted entry degrades, it does not
    half-run. The signal/phrase keys remain usable matchers for the eight
    non-Bash/file rules enforced via the signal-dispatch surface (see
    test_every_shipped_rule_is_enforceable); the annotation keys those same
    rules carry alongside (action/check/condition/platforms/threshold — see
    `_entry_field_problem`'s docstring) validate but are not themselves
    usable matchers, same as `description`.

    Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep tier."""
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

    Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep tier."""
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


def validate_innate_rule(rule: Any) -> list[str]:
    """Return the reasons `rule` cannot serve as an enforceable innate rule.

    Empty list means enforceable. Applies to EVERY rule, not just the
    destructive one; `validate_destructive_rule` layers stricter,
    innate-02-specific requirements on top of this.
    """
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


def ruleset_degraded() -> tuple[bool, str]:
    """(degraded, reason) for the WHOLE innate ruleset — the operator's signal.

    Degraded when any expected rule is absent, unenforceable, claimed by more
    than one file, not in the manifest at all, or does not hash-match its
    manifest pin. The reason names every offender so the SessionStart banner
    is actionable rather than merely alarming — a guard that cries wolf
    without saying why is a guard the operator learns to ignore.

    Reads the RAW loaded rules (`_get_raw_rules()`), not the gated working
    set `_get_rules()` returns — a shadow file, a duplicate, or a hash-
    mismatched edit is exactly what the working-set gate removes, so this
    check would go blind to the very things it exists to report if it read
    the gated set instead.

    This is the coverage check that replaces the old count (`rules_available`),
    which read a truncated lethal rule as a healthy set because the OTHER rules
    still loaded.
    """
    rules = _get_raw_rules()
    if not rules:
        return True, _LOAD_DIAGNOSTICS.reason or "no innate rules could be loaded"

    by_id: dict[str, list[dict]] = {}
    for r in rules:
        if isinstance(r, dict):
            by_id.setdefault(str(r.get("id")), []).append(r)
    problems: list[str] = []

    # Round-7 (advisor 2026-07-23, corrections 1+2): the constitution is a
    # fixed manifest of CONTENT, not just IDs — health means every expected
    # ID present, sole-claimant, with its pinned hash. Missing, unknown,
    # duplicate, and EDITED each degrade. Round 6 checked only "missing",
    # which reported green while a shadow file downgraded the lethal
    # secret-write rule (order-based shadowing) — and ID-set equality alone
    # would still report green after an in-place edit of the canonical file.
    for rid in sorted(EXPECTED_INNATE_RULE_IDS):
        claimants = by_id.get(rid, [])
        if not claimants:
            problems.append(f"{rid} is missing")
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
    for rid in sorted(set(by_id) - set(EXPECTED_INNATE_RULE_IDS)):
        problems.append(
            f"{rid} is not in the innate manifest — the constitution is "
            "fixed and non-extensible"
        )

    for rid in sorted(by_id):
        for rule in by_id[rid]:
            errs = validate_innate_rule(rule)
            if errs:
                problems.append(f"{rid} cannot enforce: {'; '.join(errs)}")

    if problems:
        return True, "; ".join(problems)
    return False, ""


def validate_destructive_rule(rule: Any) -> list[str]:
    """Return the reasons `rule` is not an enforceable destructive guard.

    Empty list means enforceable. Every reason is phrased to be readable in a
    refusal message, because it lands in front of an operator whose command was
    just blocked and who needs to know which file to fix.
    """
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
                # Round-6 P1-3 mirror: an entry with no usable matcher key at
                # all (the reviewer's `{"description": "..."}` shape) must not
                # be silently skipped past the compile checks below and left
                # invisible to both the uncompilable list AND the matcher
                # count -- that is exactly how it used to read as harmless.
                if _matcher_entry_problem(entry) is not None:
                    continue
                pattern = entry.get("command_pattern")
                if isinstance(pattern, str) and pattern:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        # NOT swallowed. The matcher's `except re.error: pass`
                        # is precisely how a broken pattern becomes a silently
                        # absent guard.
                        uncompilable.append(f"{pattern!r} ({exc})")
                    else:
                        # Compiling is necessary and not sufficient. YAML
                        # double-quoted scalars interpret backslash escapes, so
                        # `"\bTRUNCATE\b"` (one backslash) becomes a pattern
                        # containing two literal BACKSPACE characters. It
                        # compiles perfectly and can never match anything.
                        #
                        # Found by making exactly that mistake while editing
                        # this rule on 2026-07-20. The pattern must be written
                        # `"\\bTRUNCATE\\b"` in YAML. Nothing detected it: it
                        # compiled, so the uncompilable check passed, and it
                        # counted as a matcher, so the coverage check passed.
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
        # entry is usable -- one scalar/empty/unsupported entry kills the
        # whole conjunction at runtime (_match_all_satisfied), same as
        # validate_innate_rule above.
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
    is PROVEN, not assumed — health reporting (ruleset_degraded) says the set
    is untrustworthy, this gate makes the matcher act that way.

      - id not in EXPECTED_INNATE_RULE_IDS -> dropped. Whatever its filename
        sorts as, a non-manifest rule can never match, so first-match order
        cannot be used to shadow a canonical rule.
      - content hash != the id's pin -> dropped. Advisor corrections 1+2:
        identity, never severity, decides who acts. A severity tie-break is
        gameable (a lethal-but-NARROW impostor wins the tie and silently
        removes coverage); an edited canonical file has the right id, sole
        claim, and valid schema, and still must not act. Only the rule that
        IS the manifest rule runs; if no claimant matches, the id is
        unsatisfied and the degraded fallback arms instead.
      - duplicates: only hash-matching copies survive, and those are
        byte-identical by construction -> dedupe to one. No arbitration axis
        exists for an attacker to game.
      - fails validate_innate_rule -> dropped (belt and braces: a pinned
        manifest rule always validates — pinned by test — but this gate must
        not depend on that).

    Replaces _reject_unenforceable_destructive_rule: innate-02 keeps its
    stricter validate_destructive_rule bar on top of the general one.
    """
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


# Deliberately hardcoded, deliberately blunt, deliberately not YAML. This is the
# fallback for the case where the ruleset itself cannot be trusted, so it must
# not depend on the ruleset, on PyYAML, or on any file being readable. Broad
# beats precise here: when the guard is degraded the right bias is to
# over-confirm, and the operator has a named override for the false positives.
_FALLBACK_DESTRUCTIVE_PATTERNS = (
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)",
    r"\brmdir\s+/s\b",
    r"\bgit\s+push\b[^|;&]*\s(--force\b|-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+checkout\s+--\s",
    r"\bgit\s+worktree\s+remove\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b(?![^;]*\bwhere\b)",
    r"\bmkfs(\.|\s)",
    r"\bdd\s+[^|;&]*\bof=/dev/",
    r"\bshred\b",
    r"\bremove-item\b[^|;&]*-recurse",
    r"\bformat\s+[a-zA-Z]:",
    r"\bshutdown\b",
    r"\breboot\b",
)

_FALLBACK_DESTRUCTIVE_MESSAGE = """\
⛔ DESTRUCTIVE COMMAND REFUSED — the innate ruleset is DEGRADED

The command and target are intentionally omitted from diagnostics.
This fallback is intentionally blunt and may over-refuse.

How to proceed:
  * Repair or reinstall the ruleset, then retry only after reviewing the action.
  * No local confirmation or environment phrase releases this refusal.

The refusal is recorded to .allostat/observations.jsonl as
innate_guard_degraded_refusal, so a degraded ruleset is visible in forensics
rather than being inferred from an absence.
"""

# Round-6 P1-3 follow-up (final-review MUST-FIX #1): `destructive_guard_degraded`
# now arms this fallback in TWO distinct situations, and only one of them means
# innate-02 itself is unavailable:
#
#   1. innate-02 itself failed to load / doesn't validate -> "could not be
#      loaded" is true, and no innate rule is covering destructive commands.
#   2. innate-02 loaded and validated CLEANLY, but a SIBLING lethal rule (e.g.
#      innate-01) is what's degraded, so `ruleset_degraded()` says the whole
#      set is untrustworthy and this fallback arms as a precaution. Here
#      innate-02 did NOT fail to load -- saying so would be false, and the
#      real culprit named in `reason` would be buried under a wrong claim.
#
# Both variants must also drop the old "every other innate rule is INERT"
# overclaim: even in situation 1, the other eleven rules loaded fine and keep
# running -- only destructive-command coverage is affected.
_FALLBACK_WHY_SELF_BROKEN = (
    "Why this fired: the destructive-command rule ({rule_id}) could not be "
    "loaded ({reason}). Rather than allow a destructive command with no "
    "guard in place, the regulator falls back to a built-in pattern set and "
    "refuses."
)
_FALLBACK_SCOPE_SELF_BROKEN = (
    "{rule_id} is the rule affected -- every other innate rule that loaded "
    "successfully keeps running normally. This fallback exists solely to "
    "cover the destructive-command gap {rule_id} left."
)
_FALLBACK_WHY_SIBLING_DEGRADED = (
    "Why this fired: the innate ruleset is degraded ({reason}). The "
    "destructive-command rule ({rule_id}) itself loaded and validated "
    "cleanly, but while any part of the ruleset is unreliable this built-in "
    "pattern set arms as a precaution and refuses destructive-looking "
    "commands whether or not {rule_id} would also have caught this one."
)
_FALLBACK_SCOPE_SIBLING_DEGRADED = (
    "Only the rule named in the reason above is unenforceable -- {rule_id} "
    "and the rest of the innate ruleset continue to run normally. This "
    "fallback is an extra precaution layered on top while the ruleset is "
    "untrusted."
)


class _LoadDiagnostics:
    """Why the ruleset is in the state it is in.

    Kept because "no rules loaded" and "rules loaded, destructive one is fine"
    were previously indistinguishable at the call site -- both were an empty
    match. An absence is not a signal unless something records it.
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
_DROPPED_RULES: list[dict[str, str]] = []


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

    Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep
    tier (test_pillar_wrapper_server_drift_parity).
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


def _get_raw_rules() -> list[dict[str, Any]]:
    """Return every rule that PARSED, before the manifest-identity gate.

    This is the pre-fix `_get_rules()` body verbatim. Kept as its own cached
    accessor (Round-7) because `ruleset_degraded()` reports on shadowing,
    duplication, and hash-mismatch — the exact conditions `_build_working_set`
    removes — so it must see the raw, ungated list; `_get_rules()` below
    layers the gate on top of this for every caller that actually MATCHES.
    """
    global _RAW_RULES_CACHE
    if _RAW_RULES_CACHE is None:
        # C01: the failures list describes THIS load — reset with the cache.
        _LOAD_FAILURES.clear()
        rules_dir = _bundled_rules_dir()
        if not rules_dir:
            _LOAD_DIAGNOSTICS.set("no bundled rules directory was found")
            _LOAD_FAILURES.append({
                "file": "*",
                "problem": "no bundled rules directory was found",
            })
        _RAW_RULES_CACHE = _load_rules(rules_dir) if rules_dir else []
    return _RAW_RULES_CACHE


def _get_rules() -> list[dict[str, Any]]:
    """Return the GATED working set the matcher may consult; load on first call.

    Round-7: gating moved from `_reject_unenforceable_destructive_rule`
    (innate-02-only) to `_build_working_set` (every rule, manifest-identity —
    see its docstring). `match_pre_tool_call`, `rules_available`, and
    `_destructive_guard_status` all want THIS — the set that will actually be
    consulted — not the raw list `_get_raw_rules()` returns.
    """
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = _build_working_set(_get_raw_rules())
    return _RULES_CACHE


def destructive_guard_degraded() -> tuple[bool, str]:
    """(degraded, reason). True when the destructive rule is not enforceable.

    Checks the rule's ENFORCEABILITY, not merely its presence.

    The presence check came first, and it was the right instinct applied one
    level too shallow: every loader failure path in this module is silent and
    several are partial, so eleven rules can load while the twelfth -- the one
    that matters -- is skipped by a `continue`. Looking for the id caught that.

    What it did not catch is a file that loads fine and still guards nothing.
    `id: innate-02` with `trigger: {}` is valid YAML, passes a presence check,
    matches no command, and leaves this function answering "healthy" -- so the
    fallback stays disarmed and `rm -rf` runs. That is the same shape as the
    other four ID-versus-validity defects found this month: an identifier was
    accepted as proof of the thing it names.

    Validation runs here as well as at load time. The redundancy is deliberate:
    load-time rejection is what keeps a broken rule out of the matcher, and
    this check is what stays correct if a future path ever populates the cache
    without going through the loader.

    Round-6 P1-3: when innate-02 ITSELF validates clean, this still defers to
    the whole-ruleset coverage check (`ruleset_degraded`) rather than
    returning healthy outright. A different lethal rule (innate-01, say)
    corrupted by the same partial sync or bad merge that could just as
    easily have hit innate-02 is exactly the signal `ruleset_degraded`
    already raises for the SessionStart banner; without this, the banner
    would tell the operator their safety net is questionable while the one
    backstop that runs on every command stayed disarmed because it only ever
    checked its own rule's validity.
    """
    degraded, reason, _ = _destructive_guard_status()
    return degraded, reason


def _destructive_guard_status() -> tuple[bool, str, bool]:
    """(degraded, reason, self_broken) -- `self_broken` is True only when
    innate-02 ITSELF is the reason the guard is degraded (missing, malformed,
    or unenforceable), and False when innate-02 loaded and validated cleanly
    but the whole ruleset is degraded because of a SIBLING rule.

    Split out of `destructive_guard_degraded` (final-review MUST-FIX #1) so
    `_degraded_destructive_fallback` can pick a truthful message variant
    without duplicating this decision or changing the public 2-tuple
    contract other callers (and `test_the_degradation_fixture_really_degrades`)
    rely on.
    """
    rules = _get_rules()
    for rule in rules:
        if isinstance(rule, dict) and rule.get("id") == DESTRUCTIVE_RULE_ID:
            problems = validate_destructive_rule(rule)
            if not problems:
                degraded, reason = ruleset_degraded()
                return degraded, reason, False
            return True, (
                f"{DESTRUCTIVE_RULE_ID} is present but not enforceable: "
                + "; ".join(problems)
            ), True
    if not rules:
        return True, _LOAD_DIAGNOSTICS.reason or "no innate rules could be loaded", True
    return True, (
        f"{len(rules)} rule(s) loaded but {DESTRUCTIVE_RULE_ID} was not among "
        f"them: {_LOAD_DIAGNOSTICS.reason or 'its file was unreadable or unparseable'}"
    ), True


_SESSION_SCRIPT_MESSAGE = """\
⛔ RUNNING A SCRIPT THIS SESSION WROTE — innate-02 fired

Command and script path are intentionally omitted from refusal output.

Why this fired: innate-02 reads the command it is given. This command runs a
file that was written or modified earlier in THIS session, so what actually
executes is text the guard never saw. A script containing `rm -rf` over four
paths runs unguarded; the same `rm -rf` typed inline is refused. The only
difference is indirection, and the blast radius is identical.

How to proceed:

  Read the script back first. If it does nothing destructive, that is a
  five-second check; if it does, this box just earned its keep.

  No local confirmation or environment phrase releases this refusal in the
  partial guard build.

LIMITATION, stated plainly: this covers scripts written in the SAME session.
It does not and cannot cover indirection in general — a pre-existing script, a
`curl | sh`, a base64 payload, a Makefile target, an `eval`. String matching at
PreToolUse cannot see through arbitrary indirection. This guard is a seatbelt,
not a sandbox, and it should not be trusted as one.

No model-writable consent state is produced by this partial build.
"""

# Interpreters whose argument is a program to run, not data to read.
_SCRIPT_INTERPRETERS = (
    "bash", "sh", "zsh", "ksh", "dash", "fish",
    "python", "python3", "py", "perl", "ruby", "node", "deno",
    "powershell", "pwsh", "cmd", "source",
)


def _owner_enabled(rule_id: str) -> bool:
    """A broken preference reader must not disable the regular rule matcher."""
    try:
        from guard_settings import enabled
        return enabled(rule_id) is not False
    except Exception:
        return True


def session_written_script_match(
    command: str | None,
    session_written_paths: set[str] | frozenset[str] | None,
) -> "InnateRuleMatch | None":
    """innate-02 for the ISSUE-005 class: executing a script this session wrote.

    Measured on the bench VM, minutes apart in one session, identical blast
    radius: `bash /tmp/cl.sh` (the script held `rm -rf` over four paths) ran
    unguarded, while `rm -rf ~/.claude/projects/-tmp` inline was blocked and
    demanded an override. The only difference is whether the destructive text
    appears in the command string the hook receives.

    Worth being precise about why this matters: it was not an evasion attempt.
    The agent had been moving shell into script files all session because
    PowerShell mangled WSL quoting. So the bypass sits on the ORDINARY path for
    anyone working Windows->WSL — the guard silently degrades to advisory
    exactly when an agent does the normal thing.

    SCOPE, deliberately narrow: a file written or modified earlier in the same
    session, executed by an interpreter. General indirection analysis is not
    attempted and is documented as out of reach (see the message text) rather
    than half-built — a guard trusted beyond its coverage is worse than one
    whose limits are written down.

    Returns None when nothing matches, so callers can chain it.
    """
    if not _owner_enabled(DESTRUCTIVE_RULE_ID) or not command or not session_written_paths:
        return None

    normalized_command = command.replace("\\", "/")
    lowered = normalized_command.lower()
    for raw in session_written_paths:
        if not raw:
            continue
        candidate = str(raw).replace("\\", "/")
        basename = candidate.rsplit("/", 1)[-1]
        if not basename:
            continue
        # The path (or its basename) must appear AND an interpreter must be
        # invoking something — `cat /tmp/cl.sh` and `rm /tmp/cl.sh` are not
        # executions and must not fire a lethal gate.
        if candidate.lower() not in lowered and basename.lower() not in lowered:
            continue
        interpreter_execution = any(
            re.search(rf"(?:^|[\s;&|`(]){re.escape(exe)}(?:\.exe)?\s", command, re.IGNORECASE)
            for exe in _SCRIPT_INTERPRETERS
        )
        direct_spellings = (candidate, f"./{candidate}", f"./{basename}", basename)
        direct_execution = any(
            re.search(
                rf"(?:^|[;&|`(]\s*)[\"']?{re.escape(spelling)}"
                rf"(?=$|[\s;&|)`\"'])",
                normalized_command,
                re.IGNORECASE,
            )
            for spelling in direct_spellings
        )
        if not interpreter_execution and not direct_execution:
            continue
        overrides_active = _parse_overrides_set(_os_environ_overrides())
        return InnateRuleMatch(
            rule_id=DESTRUCTIVE_RULE_ID,
            rule_name="Executing a script written earlier this session",
            severity="lethal",
            response_action="hard_pause",
            formatted_message=_SESSION_SCRIPT_MESSAGE.format(
                command=(command or "")[:500],
                script=candidate,
            ),
            overridden=("*" in overrides_active or DESTRUCTIVE_RULE_ID in overrides_active),
        )
    return None


def _degraded_destructive_fallback(command: str | None) -> "InnateRuleMatch | None":
    """Refusal for a destructive command when the destructive rule is unavailable.

    Returns None when the guard is healthy or the command does not look
    destructive, so callers can use it as their final `return`.
    """
    if not _owner_enabled(DESTRUCTIVE_RULE_ID):
        return None
    degraded, reason, self_broken = _destructive_guard_status()
    if not degraded or not _fallback_destructive_match(command):
        return None
    overrides_active = _parse_overrides_set(_os_environ_overrides())
    if self_broken:
        why_tpl, scope_tpl = _FALLBACK_WHY_SELF_BROKEN, _FALLBACK_SCOPE_SELF_BROKEN
    else:
        why_tpl, scope_tpl = _FALLBACK_WHY_SIBLING_DEGRADED, _FALLBACK_SCOPE_SIBLING_DEGRADED
    return InnateRuleMatch(
        rule_id=DESTRUCTIVE_RULE_ID,
        rule_name="Destructive command refused (innate ruleset degraded)",
        severity="lethal",
        response_action="hard_pause",
        formatted_message=_FALLBACK_DESTRUCTIVE_MESSAGE.format(
            rule_id=DESTRUCTIVE_RULE_ID,
        ),
        overridden=("*" in overrides_active or DESTRUCTIVE_RULE_ID in overrides_active),
    )


def _fallback_destructive_match(command: str | None) -> bool:
    """Blunt built-in destructive matcher, used only when the rule is degraded."""
    if not command:
        return False
    haystack = command.lower()
    for pattern in _FALLBACK_DESTRUCTIVE_PATTERNS:
        try:
            if re.search(pattern, haystack):
                return True
        except re.error:
            # A broken pattern must not disable the guard it belongs to.
            continue
    return False


def reset_rules_cache() -> None:
    """Test helper — force rule reload on next lookup."""
    global _RULES_CACHE, _RAW_RULES_CACHE
    _RULES_CACHE = None
    _RAW_RULES_CACHE = None


def rules_available() -> bool:
    """True when at least one innate rule is loaded (refusal teeth armed).

    Pre-release item 55: SessionStart consults this and emits the loud
    degraded-mode banner when ZERO rules loaded (rules dir missing, or
    no vendored *.json AND PyYAML not importable). Destructive-action
    guards silently absent must never be silent.
    """
    return bool(_get_rules())


# ---------- matching helpers ----------


def _path_match(path: str, pattern: str, case_sensitive: bool = False) -> bool:
    """Match path against pattern with gitignore-style `**/` semantics.

    Mirrors server-side `_path_match`. Standard fnmatch supplemented with:
    a `**/` prefix in the pattern matches zero OR more leading directory
    components. So `**/.env*` matches both `.env` (root-level) and
    `config/.env` (nested).

    Closes Finding #7 (sprint 2026-05-15): rule 01 secrets-protection
    patterns are written `**/.env*`, `**/credentials*` etc. Pure fnmatch
    required `**` to consume AT LEAST one directory; root-level secrets
    files silently bypassed the rule.

    M17 (fixpass 2026-07-01): platform-deterministic matching — separators
    normalized to `/`, case folded only when case_sensitive is False, match
    via fnmatchcase (fnmatch.fnmatch normcased both args, making the flag a
    no-op on Windows). Default case-INSENSITIVE, in lockstep with the
    server-side matcher (cross-side parity).
    """
    hay = path.replace("\\", "/")
    needle = pattern.replace("\\", "/")
    if not case_sensitive:
        hay = hay.lower()
        needle = needle.lower()
    if fnmatch.fnmatchcase(hay, needle):
        return True
    if needle.startswith("**/"):
        if fnmatch.fnmatchcase(hay, needle[3:]):
            return True
    return False




def _match_command_pattern(entry: dict, command: str) -> bool:
    """Check if entry's `command_pattern` regex matches command."""
    pat = entry.get("command_pattern")
    if not pat or not command:
        return False
    flags = 0 if entry.get("case_sensitive", True) else re.IGNORECASE
    try:
        return bool(re.search(pat, command, flags=flags))
    except re.error:
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
    entry (rules 01/03) keeps `_match_file_pattern`'s bypass-when-
    unclassified semantics, and signal entries stay on the dispatch
    surface. Annotation keys (`platforms`, `check`) refine the CLASSIFIER's
    vocabulary rather than this equality test: the classifier only emits
    `webhook_send_with_payload` when the arguments actually carry a
    communication payload, and platform evidence is part of how a tool
    name classifies to `post_to_social` at all (see
    _classify_semantic_operation in hooks/pre-tool-use.py and
    audits/20260803_send_and_permission_operation_inventory.md).

    `operation=None` (unclassifiable call) does NOT fire — same strictness
    as `_match_all_satisfied`. This family guards NAMED semantic
    operations; treating unknown as matching would fire two lethal rules
    on every unclassified tool call.

    Mirrored verbatim server-side (pillars/innate_enforcer.py) — lockstep
    tier (test_pillar_wrapper_server_drift_parity).
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
    fail-safe; match_all stays strict). Mirrored verbatim server-side
    (pillars/innate_enforcer.py) — drift-parity lockstep tier.
    """
    if isinstance(required_op, (list, tuple, set)):
        return operation in required_op
    return required_op == operation


def _target_exists_or_unknown(file_path: str) -> bool:
    """True when the target exists OR its existence cannot be determined.

    FAIL-SAFE, and the asymmetry is the whole point: only a definitive
    "not found" is allowed to relax a lethal guard. Anything else — permission
    denied, an invalid path, a filesystem error — counts as "it might be
    there", so the rule keeps firing.

    `Path.exists()` is deliberately NOT used here. It swallows every OSError
    and returns False, so an existing-but-unreadable secrets file would read as
    a create and walk straight through innate-01 — a strictly worse defect than
    the false positive this gate exists to remove. `NotADirectoryError` is
    treated as absent because a target whose parent is a file genuinely cannot
    exist. Pinned by
    tests/test_innate01_creates_are_not_overwrites.py::test_an_unreadable_target_still_refuses.
    """
    try:
        os.stat(file_path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError, TypeError):
        return True


def _match_file_pattern(
    entry: dict,
    file_path: str,
    operation: str | None,
) -> bool:
    """Check if entry's `file_pattern` glob matches file_path, with
    optional `operation` gating and optional `target_must_exist` gating."""
    pat = entry.get("file_pattern")
    if not pat or not file_path:
        return False
    required_op = entry.get("operation")
    # Parity with server innate_enforcer.py — BYPASS the op gate when
    # `operation` is falsy (None), so an op-gated file_pattern rule still fires
    # on a path match. Using `required_op is not None` here made the wrapper
    # (the PRIMARY refusal path) under-fire vs the server when a file-mutating
    # tool isn't in pre-tool-use's _op_map and operation resolves to None
    # (ultraswarm HIGH). match_all keeps the strict `is not None` form, matching
    # the server's _match_all_satisfied.
    #
    # W2-D refinement (audit H-09): None now means "genuinely unknown tool"
    # only — pre-tool-use's _op_map classifies known read-only tools
    # (Read/Grep/Glob/NotebookRead) as a first-class "read" operation, which
    # takes the truthy-mismatch branch here (gate applies, mutating-op-gated
    # rules do NOT fire on reads). The fail-safe bypass stays reserved for
    # tools the hook cannot classify. `required_op` may be a string or a
    # list of alternatives (H-08) — see _operation_matches.
    if required_op and operation and not _operation_matches(required_op, operation):
        return False
    case_sensitive = entry.get("case_sensitive", False)
    if not _path_match(file_path, pat, case_sensitive=case_sensitive):
        return False
    # `target_must_exist` (2026-07-30, advisor fix C): opt-in, and only
    # innate-01 opts in. That rule is named "never OVERWRITE secrets" and its
    # why_lethal is about losing existing keys, but it gated on glob+operation
    # alone — so creating a brand-new credential file, the routine case, was
    # refused with the same lethal hard-pause as clobbering a live one. A guard
    # that is loudest where there is no risk teaches its operator to disable it.
    # Checked LAST so the stat only happens on a path the rule already matched.
    if entry.get("target_must_exist") and not _target_exists_or_unknown(file_path):
        return False
    return True


def _match_all_satisfied(
    entries: list[Any],
    operation: str | None,
    context_flags: set[str],
) -> bool:
    """Return True iff EVERY entry in `entries` is satisfied.

    Each entry may carry an `operation:` (a string the supplied `operation`
    must equal, or a list it must be a member of — W2-D H-08) and/or
    `context:` (must be present in `context_flags`). Unlike the match_any
    file gate, an op-gated entry here is NOT satisfied by operation=None
    (strict form, unchanged). Mirrors server-side.

    Round-7 type-guard (defense in depth, not the primary fix — the primary
    fix is `_match_all_entry_problem` rejecting this at validation time so a
    malformed entry never reaches the working set): a malformed entry that
    somehow reaches this runtime (e.g. `context: []`, unhashable against a
    `set`) makes the WHOLE conjunction UNSATISFIED rather than raising
    `TypeError` or matching vacuously.
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


# ---------- public API ----------


@dataclass
class InnateRuleMatch:
    """A fired innate rule's decision payload for the wrapper to act on."""
    rule_id: str
    rule_name: str
    severity: str
    response_action: str  # typically "hard_pause"
    formatted_message: str  # ready-to-emit red-box text
    # Transition-only flag for the operator's explicit innate-09 suspension.
    # Other rules cannot be downgraded by this legacy environment input.
    overridden: bool = False


def is_refusal_severity(severity: str) -> bool:
    """Return True if a fired-rule severity should trigger wrapper-side
    `exit(2)` refusal at PreToolUse.

    Severities of `lethal` and `critical` refuse. Lower severities
    (`high`, `medium`, etc.) currently surface as red-box chrome only
    — they emit but do not refuse — preserving the existing v1.1.1
    behavior for non-lethal rules. Future PATCH may broaden refusal
    to all severities.

    Cross-sweep (2026-07-21): `.strip()`. This used to lowercase without
    stripping, so `"lethal "` — a shape the vendored-JSON compile path
    reproduces verbatim, since JSON preserves the trailing space a YAML plain
    scalar would have eaten — compared unequal to `"lethal"` and the rule
    degraded from hard refusal to advisory chrome while the tool RAN.
    """
    return (severity or "").strip().lower() in ("lethal", "critical")


# Severities the ruleset is allowed to declare. A value outside this set is not
# a milder severity; it is an unreadable one, and `_effective_severity` below
# refuses to read it as permission.
_KNOWN_SEVERITIES: frozenset[str] = frozenset(
    {"lethal", "critical", "high", "medium", "low", "info"}
)


def _effective_severity(rule: dict) -> str:
    """The severity a fired rule is ENFORCED at.

    Cross-sweep (2026-07-21) — the same defect class R3 closed for innate-02,
    applied to every other lethal rule. `_format_match` used to read severity as
    `rule.get("severity", "unknown")`: a rule whose severity key was lost to a
    truncated write, or padded by the JSON compile path, resolved to
    not-a-refusal, so `pre-tool-use.py` took the `emit_additional_context`
    branch — red box printed, TOOL RAN — and the observation it wrote recorded
    `refused: false`, indistinguishable from a working soft rule. innate-01
    (secrets), innate-03 (legacy archival) and innate-04 (canonical
    verification) are all `severity: lethal` + `action: hard_pause` with no
    `_degraded_destructive_fallback` behind them, so nothing else would catch it.

    Two normalisations, both fail-closed:

      1. whitespace and case are stripped, so `"lethal "` enforces as `lethal`;

      2. a severity that is missing, empty, non-string or unrecognised cannot be
         trusted to say how serious the rule is, so the rule's own RESPONSE
         ACTION decides instead — the declaration `_BLOCKING_RESPONSE_ACTIONS`
         already defines as blocking. A rule that unambiguously declares
         `hard_pause` is enforced as `lethal` rather than permitted.

    A rule whose severity IS recognised is returned unchanged. That is
    deliberate and it is what keeps this change small: innate-11 and innate-12
    are `high` + `hard_pause` on purpose (nudges), and they stay chrome-only.
    """
    raw = rule.get("severity")
    severity = raw.strip().lower() if isinstance(raw, str) else ""
    if severity in _KNOWN_SEVERITIES:
        return severity

    response = rule.get("response")
    action = response.get("action") if isinstance(response, dict) else None
    if isinstance(action, str) and action.strip().lower() in _BLOCKING_RESPONSE_ACTIONS:
        logger.warning(
            "innate rule %s declares a blocking response (%s) but its severity "
            "is %r, which is not one of %s; enforcing it as lethal rather than "
            "letting an unreadable severity permit the call",
            rule.get("id", "unknown"), action, raw, sorted(_KNOWN_SEVERITIES),
        )
        return "lethal"

    return severity or "unknown"


def match_pre_tool_call(
    tool_name: str,
    command: str | None = None,
    file_path: str | None = None,
    operation: str | None = None,
    context_flags: set[str] | None = None,
    sibling_list: list[str] | None = None,
    *, confirmed_destructive: bool = False,
) -> InnateRuleMatch | None:
    """Evaluate all innate rules against the pre-tool-call attributes.

    Returns the FIRST matching rule's formatted decision; None if no rule
    fires. Mirrors server-side `_match_pre_tool_call_to_rule`'s iteration
    order (rules are sorted alphabetically by YAML filename = numeric
    prefix order = rule 01 first, rule 12 last).

    Parameters:
      tool_name:     e.g. "Bash", "Write", "Edit", "NotebookEdit"
      command:       Bash command string, if tool_name=="Bash"
      file_path:     File-path argument, if applicable
      operation:     "create" | "modify" | "delete" — wrapper-classified
      context_flags: Set of filesystem-context flags (rule 04 inputs)
      sibling_list:  Sibling folder names for rule 04's {sibling_list}
                     placeholder (C-06: local match now receives real
                     filesystem context, so the red box can name folders)
    """
    rules = _get_rules()
    if not rules:
        # Everything else is INERT when the ruleset cannot be loaded -- but the
        # destructive fallback below still applies. A regulator that refuses
        # every action on a malformed rule file is a regulator the operator
        # turns off.
        return _degraded_destructive_fallback(command)
    if context_flags is None:
        context_flags = set()

    # R3 transition bridge: preserve only the operator's temporary, explicit
    # innate-09 suspension while the independently enforced scoped send guard
    # is reviewed. It is not a public remedy and it cannot suspend any other
    # rule, including destructive confirmation.
    overrides_env = _os_environ_overrides()
    overrides_active = _parse_overrides_set(overrides_env)

    # Round-10 (operator decision 2026-07-24): the redaction / neutralization
    # layer is DELETED. innate-02 matches the FULL, un-redacted command text.
    #
    # Rounds 7-9 were all the same failure: "prove this text is inert, then
    # remove it before matching" got the inertness proof wrong for some shell
    # or interpreter construct (pipe consumers, heredocs, commit-message
    # substitution, the Perl comment stripper, the git ext-diff read-only
    # proof), and the removed-but-live text sailed past the matcher. Matching
    # the full text makes that entire bypass class STRUCTURALLY impossible —
    # there is no removed-before-matching step left to get wrong.
    #
    # Cost: over-confirmation when a destructive pattern appears as DATA (a
    # commit message describing an `rm -rf` fix, a grep FOR the pattern).
    # Measured across 32,919 real commands: ~2/day inside guard-development
    # work, 0 in every other project; each is a fail-closed confirm-and-
    # proceed, never a crash or a silent allow. See
    # audits/20260724_task0_redaction_falsepositive_measurement.md and
    # design/20260724_boundary4_round10_fold_decision_plan.md.
    safe_command = command

    for rule in rules:
        rule_id = rule.get("id", "")

        # Retired by operator policy: sending is authorized by the host, not
        # blocked by the memory plugin. Keep the resource for old bundle parity.
        if rule_id == "innate-09" or not _owner_enabled(rule_id):
            continue
        # Internal second pass after exact cooperative confirmation. Never
        # suppress another rule, or the degraded-ruleset fallback below.
        if rule_id == DESTRUCTIVE_RULE_ID and confirmed_destructive is True:
            continue

        trigger = rule.get("trigger", {})

        # match_any family
        match_any = trigger.get("match_any") or []
        matched = False
        if isinstance(match_any, list):
            for entry in match_any:
                if not isinstance(entry, dict):
                    continue
                if _match_command_pattern(entry, safe_command or ""):
                    matched = True
                    break
                if _match_file_pattern(entry, file_path or "", operation):
                    matched = True
                    break
                # C02 (2026-08-03): pure `operation:` entries — rules
                # 09/10's send/grant/payment vocabulary, previously consumed
                # by no matcher anywhere.
                if _match_operation_entry(entry, operation):
                    matched = True
                    break

        # match_all family
        if not matched:
            match_all = trigger.get("match_all") or []
            if isinstance(match_all, list) and match_all:
                if _match_all_satisfied(match_all, operation, context_flags):
                    matched = True

        if not matched:
            continue

        # PATCH-185 (2026-05-23): innate-03 LEGACY-rename exemption.
        #
        # innate-03's edit_protection fires on any Edit to a `_LEGACY_*`
        # file. Correct for an Edit on a long-archived file (preventing
        # rollback-history corruption). Wrong during a LEGACY rename
        # workflow — `mv classifier.py _LEGACY_classifier.py` then
        # immediately `Edit _LEGACY_classifier.py` to prepend the
        # deprecation header — the rule conflated the freshly-renamed
        # file with the long-archived file class. Operator's
        # self-observation report flagged this firing 3x during a routine
        # rename op (PATCH-183.4 LEGACY rename of classifier.py + excerpt.py).
        #
        # Exemption: when innate-03 matches AND the file exists on disk
        # AND its mtime is within ALLOSTAT_LEGACY_RENAME_EXEMPTION_SECONDS
        # (default 60s) of now, the file is presumed freshly-renamed and
        # the rule is skipped. Long-archived LEGACY files have mtimes
        # hours/days/weeks old and still get blocked.
        #
        # ALLOSTAT_LEGACY_RENAME_EXEMPTION_SECONDS=0 disables the
        # exemption (restores pre-PATCH-185 strict behavior).
        if rule_id == "innate-03" and _is_freshly_renamed_legacy(
            file_path, _legacy_rename_exemption_seconds()
        ):
            continue

        # Transition-only innate-09 suspension; no other rule can be marked.
        match_obj = _format_match(
            rule,
            command=command,
            file_path=file_path,
            tool_name=tool_name,
            operation=operation,
            sibling_list=sibling_list,
        )
        if _override_active_for_rule(rule_id, overrides_active):
            match_obj.overridden = True
        return match_obj

    # 0B.4, LAST RESORT. Nothing in the loaded ruleset fired. If the
    # destructive rule is unavailable and the command looks destructive, refuse
    # -- because the loaded set could not have fired for it.
    #
    # ORDERING MATTERS, and the first version of this got it wrong: the check
    # ran BEFORE the rule loop, so a ruleset that had loaded destructive
    # coverage under a different id (vendored JSON rules when PyYAML is absent)
    # was pre-empted, and the fallback answered in place of a working rule.
    # `test_item55_pyyaml_absent_refusal_teeth.py` caught it. Whatever actually
    # loaded gets first say; the fallback only speaks into a silence.
    return _degraded_destructive_fallback(command)


def _os_environ_overrides() -> str:
    """Read the legacy transition input used only for innate-09.

    Module-level indirection so tests can monkeypatch via patching this
    helper rather than os.environ (which is global + leaky between tests).
    """
    import os
    return os.environ.get("ALLOSTAT_INNATE_OVERRIDES", "")


def _parse_overrides_set(raw: str) -> set[str]:
    """Parse the legacy transition input without granting any authority.

    Empty string → empty set (no overrides active).
    "*" → set containing "*" (matches every rule_id).
    "innate-01,innate-02" → {"innate-01", "innate-02"}.
    Whitespace around entries is stripped.
    """
    if not raw or not raw.strip():
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _override_active_for_rule(rule_id: str, overrides: set[str]) -> bool:
    """Only an explicit innate-09 value keeps the temporary bridge active."""
    return rule_id == "innate-09" and "innate-09" in overrides


def _legacy_rename_exemption_seconds() -> int:
    """Read ALLOSTAT_LEGACY_RENAME_EXEMPTION_SECONDS from env. PATCH-185.

    Default 60s. 0 disables the exemption (restores strict pre-PATCH-185
    behavior). Negative or non-integer values fall back to the default
    rather than crashing.

    Module-level indirection mirrors `_os_environ_overrides` so tests
    can monkeypatch via this helper rather than os.environ directly.
    """
    import os
    raw = os.environ.get("ALLOSTAT_LEGACY_RENAME_EXEMPTION_SECONDS", "")
    if not raw:
        return 60
    try:
        value = int(raw)
    except ValueError:
        return 60
    return value if value >= 0 else 60


def _is_freshly_renamed_legacy(
    file_path: str | None, exemption_seconds: int
) -> bool:
    """True iff file_path is a freshly-renamed LEGACY file. PATCH-185.

    Exemption applies when:
      - exemption_seconds > 0 (disabled when 0)
      - file_path is provided and points at an extant file on disk
      - file mtime is within exemption_seconds of now

    Non-existent files return False (preserves pre-PATCH-185 behavior:
    rule fires on path-pattern alone when file doesn't exist yet).
    """
    if exemption_seconds <= 0 or not file_path:
        return False
    import time
    try:
        st = Path(file_path).stat()
    except (OSError, ValueError):
        return False
    age_seconds = time.time() - st.st_mtime
    return age_seconds <= exemption_seconds


def _safe_substitute(template: str, **kwargs: str) -> str:
    """Replace `{name}` placeholders for kwargs only. Unknown placeholders
    remain literal in the output (visible to operator as "{var}" — clearly
    placeholder, not a crash).

    Used in preference to `str.format()` because rule message templates
    may carry placeholders this code doesn't yet supply (e.g., {target}
    for some rule types). `str.format()` raises KeyError on unknown
    placeholders, which would crash the hook subprocess.

    PATCH-160 (2026-05-22): substitution surface synced with server-side
    `innate_enforcer._check_pre_tool_call_response` so the wrapper-side
    refusal path (PATCH-143 made it primary) renders the SAME placeholders
    the server-side telemetry path renders. Pre-PATCH-160 the wrapper
    supplied only 4 placeholders (command, target, reversibility,
    short_command) while server supplied 10 — Rule 01 secrets-protection
    template rendered `{file_path}` and `{filename}` literally on operator
    screens. Closes audit Phase 3 FUNC-2 (HIGH).
    """
    if not template or "{" not in template:
        return template
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", v)
    return template


def _format_match(
    rule: dict,
    command: str | None,
    file_path: str | None,
    *,
    tool_name: str | None = None,
    operation: str | None = None,
    sibling_list: list[str] | None = None,
) -> InnateRuleMatch:
    """Build an InnateRuleMatch with template substitution applied.

    PATCH-160 (2026-05-22): supplies the full 10-placeholder set the
    server-side renderer uses, so the same rule template renders the
    same message across both code paths. Keyword-only args allow future
    callers to pass additional context (tool_name, operation, sibling_list)
    while older callers (positional only) keep working unchanged.
    """
    from datetime import date as _date
    from pathlib import Path as _Path
    response = rule.get("response", {})
    message_template = response.get("message", "(rule fired; no message defined)")

    # Cross-sweep (2026-07-21): NOT `rule.get("severity", "unknown")`. See
    # `_effective_severity` — a lenient default here silently demoted a lethal
    # rule to chrome and let the tool run.
    severity = _effective_severity(rule)
    cmd_text = command or ""
    short_command = cmd_text if len(cmd_text) <= 60 else cmd_text[:57] + "..."
    reversibility = (
        "hard to undo" if severity in ("lethal", "critical") else "potentially destructive"
    )

    # PATCH-160: full placeholder set matching server-side renderer.
    filename = (_Path(file_path).name if file_path else "")
    sibling_rendered = ""
    if sibling_list:
        sibling_rendered = ", ".join(str(s) for s in sibling_list if s)
    if not sibling_rendered:
        sibling_rendered = "(filesystem context not yet provided)"
    today_str = _date.today().isoformat()

    formatted = _safe_substitute(
        message_template,
        # legacy 4-placeholder set (preserved for backward-compat)
        command=command or "(no command)",
        target=file_path or cmd_text or "(unspecified)",
        reversibility=reversibility,
        short_command=short_command or "this command",
        # PATCH-160 additions — matching server-side
        file_path=file_path or "",
        filename=filename,
        tool_name=tool_name or "",
        operation=operation or "",
        # C02 (2026-08-03): rules 09/10's templates say {operation_type} —
        # render the classified operation there rather than a literal
        # placeholder. Synced with server _check_pre_tool_call_response
        # (PATCH-160 tier).
        operation_type=operation or "(unclassified operation)",
        sibling_list=sibling_rendered,
        today_date=today_str,
    )

    return InnateRuleMatch(
        rule_id=rule.get("id", "unknown"),
        rule_name=rule.get("name", ""),
        severity=severity,
        response_action=response.get("action", "hard_pause"),
        formatted_message=formatted,
    )
