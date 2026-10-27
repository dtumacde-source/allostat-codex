#!/usr/bin/env python3
"""Generate `EXPECTED_INNATE_RULE_HASHES` from the shipped rules dir.

Loads every non-LEGACY rule the same way `_load_rules` does — YAML is the
source of truth, `id`-from-stem default applied — hashes each with
`innate_rules._rule_content_hash`, and prints the dict literal sorted by rule
id for pasting into both `innate_rules.py` and (Task 5) `innate_enforcer.py`.

Also verifies YAML/JSON parity: if a vendored `*.json` compilation exists
alongside a `*.yaml` source (`make_bundle.py::_compile_innate_rules_to_json`,
gitignored build artifacts — normally absent from a dev checkout), it must
hash IDENTICALLY to its YAML source. A divergence here means the bundle
compiler produced content that isn't equivalent to what it compiled from —
a bundle-compiler finding to surface, not a hash to fudge — so the script
exits non-zero and prints the mismatching id(s) instead of a dict literal.

Regenerate ONLY as part of a deliberate constitution change, in the same
commit as the rule edit that motivated it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WRAPPER_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = WRAPPER_ROOT / "rules" / "innate"
sys.path.insert(0, str(WRAPPER_ROOT / "lib"))

import innate_rules  # noqa: E402


def main() -> int:
    yaml_rules = innate_rules._load_rules_from_yaml(RULES_DIR)
    by_id = {r["id"]: r for r in yaml_rules if isinstance(r, dict) and r.get("id")}

    mismatches: list[str] = []
    for path in sorted(RULES_DIR.glob("*.json")):
        if "_LEGACY_" in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            mismatches.append(f"{path.name}: could not parse ({exc})")
            continue
        if not isinstance(data, dict):
            mismatches.append(f"{path.name}: not a JSON mapping")
            continue
        if "id" not in data:
            data["id"] = path.stem
        rid = data.get("id")
        yaml_rule = by_id.get(rid)
        if yaml_rule is None:
            mismatches.append(f"{path.name}: id {rid!r} has no YAML counterpart")
            continue
        json_hash = innate_rules._rule_content_hash(data)
        yaml_hash = innate_rules._rule_content_hash(yaml_rule)
        if json_hash != yaml_hash:
            mismatches.append(
                f"{rid}: YAML hash {yaml_hash} != vendored JSON hash {json_hash} "
                f"({path.name})"
            )

    if mismatches:
        print("ERROR: YAML/JSON hash divergence found:", file=sys.stderr)
        for m in mismatches:
            print(f"  - {m}", file=sys.stderr)
        return 1

    print("EXPECTED_INNATE_RULE_HASHES: dict[str, str] = {")
    for rid in sorted(by_id):
        print(f'    "{rid}": "{innate_rules._rule_content_hash(by_id[rid])}",')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
