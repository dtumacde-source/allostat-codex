#!/usr/bin/env python3
"""Cross-project recall-silo sweep script (ι).

Operator runs this periodically (or wraps it as a cron entry) to scan
every project's `.allostat/silos/` and surface fingerprints recurring
across N≥2 projects — candidates for umbrella-tier rule promotion.

Usage:
  python scripts/cross_project_silo_sweep.py [--threshold N]

Output: operator-facing surface text to stdout.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow imports from sibling lib.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from cross_project_silos import (  # noqa: E402
    DEFAULT_CROSS_PROJECT_THRESHOLD,
    format_matches_surface,
    sweep_all_classes,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Allostat MCP wrapper — cross-project recall-silo sweep. "
            "Walks every Claude-Code project's .allostat/silos/ and "
            "surfaces fingerprints recurring across ≥threshold projects."
        )
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_CROSS_PROJECT_THRESHOLD,
        help="Minimum project count for a fingerprint to surface (default 2).",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=None,
        help="Override the Claude-Code projects root directory.",
    )
    args = parser.parse_args()

    matches = sweep_all_classes(
        projects_root=args.projects_root,
        threshold=args.threshold,
    )
    print(format_matches_surface(matches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
