"""Read the kernel's routeless-send counters, or refuse. (R5, 2026-07-20)

## Why this is a file and not a heredoc

It was a heredoc inside `contained_test_run.sh`, and being un-runnable on its own
meant being un-testable on its own. The only way to exercise its failure paths
was to break `/proc` underneath a live run, which nobody was going to do, so its
failure paths were never exercised — and they were wrong.

## What was wrong

The previous implementation returned `0` when `/proc/net/snmp` could not be
opened, and `0` again when the expected field was absent. Both of those mean *I
could not read the counter*, and both were reported as the number zero, which
every caller reads as *nothing attempted egress*.

The check that proves nothing escaped answered CLEAN when it was blind. That is
the same fail-open shape as the four defects the sprint was convened to close,
sitting inside the instrument doing the measuring.

## The contract

Print a single integer — the sum of IPv4 and IPv6 routeless sends in the current
network namespace — or exit 97 having explained why not. There is no third
outcome, and in particular there is no path that produces a number this module
did not actually read.

Exit 97 is the runner's "containment could not be established" code, chosen
deliberately: a measurement that did not happen is not a clean measurement, and
it should stop the run for the same reason a namespace that will not come up
stops the run.
"""
from __future__ import annotations

import os
import sys

# Module-level so tests can point them at fixtures. Deliberately NOT read from
# the environment: an ambient variable that redirects the egress counter would
# be the same class of defect as the two ambient switches deleted this sprint,
# in the one place whose whole job is to be trustworthy.
SNMP_PATH = "/proc/net/snmp"
SNMP6_PATH = "/proc/net/snmp6"
IF_INET6_PATH = "/proc/net/if_inet6"

REFUSAL_EXIT_CODE = 97


class CounterUnreadable(Exception):
    """The counter could not be read. Never convertible to a number."""


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        raise CounterUnreadable(
            f"{path} could not be read ({type(exc).__name__}: {exc})"
        ) from exc


def ipv4_no_routes() -> int:
    """`Ip.OutNoRoutes` from /proc/net/snmp's two-line column format."""
    lines = _read_lines(SNMP_PATH)
    for index, line in enumerate(lines):
        if line.startswith("Ip:") and index + 1 < len(lines):
            names = line.split()[1:]
            values = lines[index + 1].split()[1:]
            if "OutNoRoutes" not in names:
                continue
            position = names.index("OutNoRoutes")
            try:
                return int(values[position])
            except (IndexError, ValueError) as exc:
                raise CounterUnreadable(
                    f"{SNMP_PATH} Ip.OutNoRoutes is unparseable ({exc})"
                ) from exc
    raise CounterUnreadable(f"{SNMP_PATH} has no Ip: OutNoRoutes field")


def ipv6_no_routes() -> int:
    """`Ip6OutNoRoutes` from /proc/net/snmp6's name-value format.

    One absence here is legitimate and it is the only one: a kernel or namespace
    with no IPv6 stack has no snmp6 to read. That is VERIFIED against
    /proc/net/if_inet6 rather than assumed, and it is announced on stderr,
    because "there is nothing to read" and "I could not read it" must never
    again produce the same answer. Anything else — the file exists but cannot be
    opened, or exists without the field — is a refusal.
    """
    if not os.path.exists(SNMP6_PATH):
        if not os.path.exists(IF_INET6_PATH):
            print("IPV6_UNAVAILABLE=1", file=sys.stderr)
            return 0
        raise CounterUnreadable(
            f"{SNMP6_PATH} is absent but IPv6 is configured ({IF_INET6_PATH} exists)"
        )
    for line in _read_lines(SNMP6_PATH):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "Ip6OutNoRoutes":
            try:
                return int(parts[1])
            except ValueError as exc:
                raise CounterUnreadable(
                    f"{SNMP6_PATH} Ip6OutNoRoutes is unparseable ({exc})"
                ) from exc
    raise CounterUnreadable(f"{SNMP6_PATH} has no Ip6OutNoRoutes field")


def total_no_routes() -> int:
    return ipv4_no_routes() + ipv6_no_routes()


def main() -> int:
    try:
        print(total_no_routes())
    except CounterUnreadable as exc:
        print(f"COUNTER_UNREADABLE: {exc}", file=sys.stderr)
        return REFUSAL_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
