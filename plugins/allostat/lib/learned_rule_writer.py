"""Learned-rule writer — write-side of the pattern-observer learning loop.

The pattern-observer detects a recurring operator behavior, proposes a
rule, the operator approves it via /allostat-promote, and THIS module
persists the approved proposal as a learned-rule YAML under
`<state_dir>/rules/learned/`. Before this module shipped there were zero
references to `rules/learned/` anywhere — the loop could detect + propose
but never persist, so the Pro-tier "it learns" feature never actually
remembered anything.

Loader contract
---------------
The persisted YAML must be consumable by the SAME loader that reads
`rules/innate/*.yaml` (`innate_rules._load_rules`): a top-level dict that
`yaml.safe_load` parses, carrying at minimum `id` (the loader derives one
from the filename stem if absent, but we set it explicitly). We mirror the
innate-rule field shape — `id / name / tier / severity / status /
description` — and add a `behavior` directive (the operator-approved
default) plus a `provenance` block linking the rule back to the proposal
that produced it (proposal hash, fingerprint, occurrence count).

Idempotency
-----------
Filename is derived deterministically from the proposal hash, and the YAML
body is hand-emitted in a fixed field order, so re-persisting the same
proposal yields the same path AND byte-identical content (no duplicate
files, no churn). This matters because /allostat-promote may be re-run.

Public API
----------
  write_learned_rule(state_dir, proposal) -> Path
      Persist one approved inner-proposal dict; returns the written path.

The `proposal` argument is the inner proposal dict the server emits —
identically what `proposal_review.PendingProposal.to_proposal_dict()`
returns. Filesystem-only; no network.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_state import (  # noqa: E402
    resolve_project_root,
    resolve_state_dir,
)

LEARNED_RULES_SUBDIR = ("rules", "learned")

# How many hex chars of the proposal hash go into the id / filename. 8 is
# enough to be collision-safe for one operator's pattern set while staying
# human-scannable in `ls rules/learned/`.
_HASH_PREFIX_LEN = 8


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def learned_rules_dir(state_dir: Path) -> Path:
    """Path to `<state_dir>/rules/learned/` (not created here)."""
    return state_dir.joinpath(*LEARNED_RULES_SUBDIR)


def _infer_severity(proposal: dict[str, Any]) -> str:
    """Map proposal confidence to a rule severity.

    An N=2 in-session-override (operator explicitly chose this direction
    twice in one session — the fast-promotion lane) is a higher-confidence
    signal than a slow N=4 cross-session drift, so it lands at `high`.
    Everything else is `medium`. Learned rules are never `lethal` — that
    tier is reserved for innate safety rules, not inferred preferences.
    """
    flags = proposal.get("flags") or []
    if isinstance(flags, list) and "fast-promotion-lane" in flags:
        return "high"
    return "medium"


def _rule_id(proposal: dict[str, Any]) -> str:
    """Deterministic, human-scannable rule id derived from the hash."""
    h = str(proposal.get("hash") or "")[:_HASH_PREFIX_LEN]
    return f"learned-{h}"


def _yaml_escape(value: str) -> str:
    """Quote a scalar for safe single-line YAML emission.

    We always double-quote string scalars and escape embedded quotes +
    backslashes. This avoids YAML special-character pitfalls (colons,
    leading symbols) without depending on yaml.safe_dump's key ordering,
    which is what lets the output stay byte-stable across runs.
    """
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _render_yaml(rule: dict[str, Any]) -> str:
    """Hand-emit the learned-rule YAML in a fixed field order.

    Fixed order + explicit quoting = byte-stable output, so re-persisting
    the same proposal never churns the file. The nested `provenance` block
    is emitted with two-space indentation.
    """
    prov = rule["provenance"]
    lines = [
        "# Learned rule — persisted by lib/learned_rule_writer.py from an",
        "# operator-approved pattern-observer proposal. Do not hand-edit the",
        "# provenance block; it links this rule back to the source pattern.",
        f"id: {_yaml_escape(rule['id'])}",
        f"name: {_yaml_escape(rule['name'])}",
        f"tier: {_yaml_escape(rule['tier'])}",
        f"severity: {_yaml_escape(rule['severity'])}",
        f"status: {_yaml_escape(rule['status'])}",
        f"added_at: {_yaml_escape(rule['added_at'])}",
        f"behavior: {_yaml_escape(rule['behavior'])}",
        f"description: {_yaml_escape(rule['description'])}",
        "provenance:",
        f"  source: {_yaml_escape(prov['source'])}",
        f"  proposal_hash: {_yaml_escape(prov['proposal_hash'])}",
        f"  fingerprint: {_yaml_escape(prov['fingerprint'])}",
        f"  class: {_yaml_escape(prov['class'])}",
        f"  subject: {_yaml_escape(prov['subject'])}",
        f"  direction: {_yaml_escape(prov['direction'])}",
        f"  occurrence_count: {int(prov['occurrence_count'])}",
        f"  sessions_spanning: {int(prov['sessions_spanning'])}",
        f"  first_seen: {_yaml_escape(prov['first_seen'])}",
        f"  last_seen: {_yaml_escape(prov['last_seen'])}",
        "",
    ]
    return "\n".join(lines)


def _existing_added_at(path: Path) -> str | None:
    """Return the `added_at` already recorded in a learned-rule file, if any.

    Used to keep re-promotion byte-idempotent: a fresh timestamp on every write
    otherwise changes the file each run and defeats the content-match skip
    (ultraswarm)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("added_at:"):
                return line.split(":", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def _build_rule(proposal: dict[str, Any], added_at: str | None = None) -> dict[str, Any]:
    """Assemble the learned-rule dict from an inner proposal dict.

    `added_at` pins the promotion timestamp when re-writing an existing rule
    (idempotency); when None a fresh timestamp is stamped for a new rule."""
    hash_ = str(proposal.get("hash") or "")
    subject = str(proposal.get("subject") or "")
    direction = str(proposal.get("direction") or "")
    class_ = str(proposal.get("class") or "")
    wording = str(proposal.get("suggested_rule_wording") or "").strip()
    subject_human = subject.replace("-", " ") or "operator workflow"

    # The behavioral directive is the operator-approved wording. Fall back
    # to a synthesized line if the proposal carried none.
    behavior = wording or f"Apply learned default for {subject_human} ({direction})."

    return {
        "id": _rule_id(proposal),
        "name": f"Learned: {subject_human}",
        "tier": "learned",
        "severity": _infer_severity(proposal),
        "status": "active",
        "added_at": added_at or _utc_now_iso(),
        "behavior": behavior,
        "description": (
            f"Operator-approved learned default for \"{subject_human}\". "
            f"{behavior} Promoted from a pattern-observer proposal seen "
            f"{int(proposal.get('occurrence_count') or 0)} time(s)."
        ),
        "provenance": {
            "source": "pattern-observer",
            "proposal_hash": hash_,
            "fingerprint": str(proposal.get("fingerprint") or ""),
            "class": class_,
            "subject": subject,
            "direction": direction,
            "occurrence_count": int(proposal.get("occurrence_count") or 0),
            "sessions_spanning": int(proposal.get("sessions_spanning") or 0),
            "first_seen": str(proposal.get("first_seen") or ""),
            "last_seen": str(proposal.get("last_seen") or ""),
        },
    }


def write_learned_rule(state_dir: Path, proposal: dict[str, Any]) -> Path:
    """Persist an approved proposal as a learned-rule YAML.

    `proposal` is the inner proposal dict (the server's shape;
    PendingProposal.to_proposal_dict() returns it). Creates
    `<state_dir>/rules/learned/` if missing. Idempotent: same proposal →
    same path, byte-identical content. Returns the written Path.

    Raises ValueError if the proposal is not a dict or carries no hash
    (the hash is the stable filename key — without it the rule can't be
    written deterministically).
    """
    if not isinstance(proposal, dict):
        raise TypeError(f"proposal must be a dict, got {type(proposal).__name__}")
    hash_ = proposal.get("hash")
    if not hash_:
        raise ValueError("proposal missing required 'hash' field")

    out_dir = learned_rules_dir(state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    subject = str(proposal.get("subject") or "rule")
    # Filename: <subject-slug>-<hashprefix>.yaml. Subject slug keeps the
    # dir human-browsable; the hash prefix guarantees uniqueness + stability.
    slug = subject[:32].strip("-") or "rule"
    filename = f"{slug}-{str(hash_)[:_HASH_PREFIX_LEN]}.yaml"
    out_path = out_dir / filename

    # Idempotency: preserve the ORIGINAL added_at when the rule already exists,
    # so re-promoting the same proposal is byte-identical (a fresh timestamp
    # each write otherwise churns the file and defeats the skip below).
    rule = _build_rule(proposal, added_at=_existing_added_at(out_path))

    body = _render_yaml(rule)
    # Idempotent write: skip the rewrite if the content already matches, so
    # re-running /allostat-promote leaves the file (and its mtime) untouched.
    if out_path.is_file():
        try:
            if out_path.read_text(encoding="utf-8") == body:
                return out_path
        except OSError:
            # Best-effort: if the existing file can't be read, fall through
            # and rewrite it from scratch (the write below is authoritative).
            pass
    out_path.write_text(body, encoding="utf-8")
    return out_path


# ---------- reading (the wire that was missing) ----------

# Severities a learned rule may carry into context injection. Learned rules
# are GUIDANCE, structurally: they are surfaced as standing defaults in
# SessionStart additionalContext and can never reach the refusal path (only
# innate rules feed match_pre_tool_call). Anything outside this set — e.g. a
# hand-edited `lethal` — is clamped to `medium` on read.
_ALLOWED_LEARNED_SEVERITIES = frozenset({"high", "medium", "low"})


def read_learned_rules(state_dir: Path) -> list[dict[str, Any]]:
    """Load active learned rules from `<state_dir>/rules/learned/*.yaml`.

    Deep-audit 2026-07-02 (advisor greenlit, release-gating): this loader is
    the previously-missing terminal half of the learning loop — promote wrote
    rules that nothing ever read. SessionStart now injects what this returns.

    Robust to a missing PyYAML (falls back to a flat-line parser sufficient
    for our own fixed-order writer format) and to malformed files (skipped).
    Only `status: active` rules return. Severity clamped to guidance tiers.
    """
    rules_dir = learned_rules_dir(state_dir)
    if not rules_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        data = _parse_learned_rule_file(path)
        if not data:
            continue
        if str(data.get("status", "active")).strip().lower() != "active":
            continue
        sev = str(data.get("severity", "medium")).strip().lower()
        if sev not in _ALLOWED_LEARNED_SEVERITIES:
            sev = "medium"
        out.append({
            "id": str(data.get("id") or path.stem),
            "name": str(data.get("name") or path.stem),
            "severity": sev,
            "behavior": str(data.get("behavior") or "").strip(),
            "path": str(path),
        })
    return out


def _parse_learned_rule_file(path: Path) -> dict[str, Any] | None:
    """Parse one learned-rule YAML. PyYAML when available; otherwise a flat
    `key: value` line parser (our writer's fixed format — top-level scalar
    fields only, which is all read_learned_rules needs)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import yaml  # noqa: E402
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None
    except ImportError:
        pass
    except Exception:
        return None
    data: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith(" "):
            continue  # skip comments + nested (provenance) block lines
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("'\"")
    return data or None


def render_learned_defaults_block(rules: list[dict[str, Any]], cap: int = 20) -> str:
    """Render the SessionStart injection block for active learned rules.
    Empty string when there are none (inject nothing)."""
    rules = [r for r in rules if r.get("behavior")]
    if not rules:
        return ""
    lines = [
        "=== Allostat learned defaults (operator-approved via /allostat-promote) ===",
        "Standing operator preferences promoted from observed patterns. Apply",
        "them as defaults in this session. They are guidance, never blockers.",
        "",
    ]
    for r in rules[:cap]:
        behavior = r["behavior"][:200]
        lines.append(f"- [{r['id']}] {behavior}")
    overflow = len(rules) - cap
    if overflow > 0:
        lines.append(f"- ... ({overflow} more in rules/learned/)")
    return "\n".join(lines)


# ---------- CLI ----------


def _cli(argv: list[str] | None = None) -> int:
    """python "$ALLOSTAT_PLUGIN_DIR/lib/learned_rule_writer.py" --proposal-json '<json>'

    --proposal-json JSON   inner proposal dict to persist (the shape the
                           server emits / proposal_review hands you)

    Resolves the active project's state dir from cwd, writes the learned
    rule, and prints the resulting path.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="learned_rule_writer")
    parser.add_argument(
        "--proposal-json",
        required=True,
        help="inner proposal dict as a JSON string",
    )
    args = parser.parse_args(argv)

    try:
        proposal = json.loads(args.proposal_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: --proposal-json is not valid JSON: {e}", file=sys.stderr)
        return 2

    project_root = resolve_project_root() or Path.cwd()
    state_dir = resolve_state_dir(project_root)

    try:
        path = write_learned_rule(state_dir, proposal)
    except (ValueError, TypeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"ERROR: could not write learned rule: {e}", file=sys.stderr)
        return 1

    print(f"learned rule written -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
