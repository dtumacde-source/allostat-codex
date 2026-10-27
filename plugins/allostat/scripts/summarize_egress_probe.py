"""Turn the child-process egress probe log into a host -> tests table. (R4)

The kernel counter answers "how many". This answers "which host, from which
test, in which process" — the question that stayed open when 96 attempts were
attributed to 24 tests without a single hostname among them.

Usage: summarize_egress_probe.py <probe.jsonl> [--json]
"""
from __future__ import annotations

import collections
import json
import sys


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from a killed child. Recorded, not hidden.
                print(f"WARN: unparseable probe line: {line[:120]!r}",
                      file=sys.stderr)
    return rows


def _short_test(nodeid: str | None) -> str:
    if not nodeid:
        return "<no test context — collection or session scope>"
    # pytest's PYTEST_CURRENT_TEST is "path::test (call)"; drop the phase.
    return nodeid.rsplit(" (", 1)[0]


def summarize(rows: list[dict]) -> dict:
    by_host: dict[str, dict] = collections.defaultdict(
        lambda: {"count": 0, "kinds": collections.Counter(),
                 "tests": collections.Counter(), "ports": collections.Counter()}
    )
    for row in rows:
        host = row.get("host") or "<unknown>"
        entry = by_host[host]
        entry["count"] += 1
        entry["kinds"][row.get("kind", "?")] += 1
        entry["tests"][_short_test(row.get("test"))] += 1
        if row.get("port") is not None:
            entry["ports"][str(row["port"])] += 1
    return by_host


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    path = argv[0]
    as_json = "--json" in argv

    rows = load(path)
    if not rows:
        print("no non-loopback attempts recorded")
        return 0

    by_host = summarize(rows)

    if as_json:
        print(json.dumps(
            {h: {"count": e["count"], "kinds": dict(e["kinds"]),
                 "tests": dict(e["tests"]), "ports": dict(e["ports"])}
             for h, e in by_host.items()},
            indent=2, sort_keys=True,
        ))
        return 0

    print(f"{len(rows)} non-loopback attempt(s) across {len(by_host)} host(s)\n")
    for host, entry in sorted(by_host.items(), key=lambda kv: -kv[1]["count"]):
        kinds = ", ".join(f"{k}x{v}" for k, v in entry["kinds"].most_common())
        ports = ",".join(p for p, _ in entry["ports"].most_common(4))
        print(f"  {entry['count']:5d}  {host}   [{kinds}]"
              + (f"  ports={ports}" if ports else ""))
        for test, count in entry["tests"].most_common(12):
            print(f"           {count:4d}  {test}")
        if len(entry["tests"]) > 12:
            print(f"           ... and {len(entry['tests']) - 12} more test(s)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
