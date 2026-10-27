#!/usr/bin/env python3
"""Regenerate the launcher-contract regions in the shipped install docs.

The behavioral sentences describing the emitted hook command are RENDERED from
`codex_wiring`'s constants rather than authored, so documentation cannot drift
from the thing it describes (L01's closure — see
`wrapper/tests/_launcher_doc_contract.py` for why a phrase heuristic was the
wrong lever). `test_documented_claims_match_behavior.py` fails if a shipped
file's generated region differs from fresh output.

Usage:
    python wrapper/scripts/regen_launcher_docs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRAPPER / "lib"))
sys.path.insert(0, str(WRAPPER / "tests"))

from _launcher_doc_contract import regenerate  # noqa: E402


def main() -> int:
    changed = regenerate()
    if not changed:
        print("generated regions already current")
        return 0
    for path in changed:
        print(f"regenerated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
