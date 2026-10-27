"""Attribute egress attempts to individual tests. Diagnostic, never a boundary.

`contained_test_run.sh` reports how many routeless sends a whole session made.
That is the right shape for a gate -- one number, impossible to argue with -- and
the wrong shape for fixing anything, because "96" does not tell you which tests
to look at.

This plugin reads the same kernel counters (`Ip.OutNoRoutes` in /proc/net/snmp,
`Ip6OutNoRoutes` in /proc/net/snmp6) around each test and reports every test that
moved them. It is loaded explicitly with `-p` for diagnostic runs and is not part
of the suite's normal configuration, because a measurement that runs by default
eventually gets mistaken for a control.

WHY THE COUNTER SEES THINGS THE IN-PROCESS GUARD DOES NOT. The guard patches
`socket.socket.connect`/`connect_ex`. A packet only reaches the routing layer if
nothing intercepted the call, so anything counted here BYPASSED the guard:

  * name resolution -- `getaddrinfo` is not `connect`, so DNS was never guarded
    at all;
  * anything imported and executed at COLLECTION time, before the session
    fixture installs the patch;
  * children launched with -S/-E/-I, which never import sitecustomize;
  * non-Python children, which have no Python guard to install.

Usage:
    pytest -p scripts.egress_attribution ...
    (inside contained_test_run.sh, where the namespace makes the sends routeless)

Outside a namespace with no default route the counters do not move, and the
plugin says so rather than reporting a clean run.
"""
from __future__ import annotations

from pathlib import Path

_SNMP = Path("/proc/net/snmp")
_SNMP6 = Path("/proc/net/snmp6")


def _ipv4_no_routes() -> int:
    try:
        lines = _SNMP.read_text().splitlines()
    except OSError:
        return 0
    for index, line in enumerate(lines):
        if line.startswith("Ip:") and index + 1 < len(lines):
            names = line.split()[1:]
            values = lines[index + 1].split()[1:]
            if "OutNoRoutes" in names:
                return int(values[names.index("OutNoRoutes")])
    return 0


def _ipv6_no_routes() -> int:
    try:
        for line in _SNMP6.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "Ip6OutNoRoutes":
                return int(parts[1])
    except OSError:
        pass
    return 0


def _count() -> int:
    return _ipv4_no_routes() + _ipv6_no_routes()


class _Attribution:
    def __init__(self) -> None:
        self.session_start = _count()
        self.offenders: list[tuple[str, int]] = []
        self._pending = 0

    def pytest_runtest_setup(self, item):
        self._pending = _count()

    def pytest_runtest_teardown(self, item):
        delta = _count() - self._pending
        if delta > 0:
            self.offenders.append((item.nodeid, delta))

    def pytest_terminal_summary(self, terminalreporter):
        total = _count() - self.session_start
        write = terminalreporter.write_line
        write("")
        write("=== egress attribution (kernel routeless-send counters) ===")
        if total == 0:
            write("no egress attempts recorded for the whole session")
            write(
                "NOTE: a zero here is only meaningful inside a namespace with no "
                "default route. Outside one, these counters never move and this "
                "plugin cannot tell containment from a working network."
            )
            return
        attributed = sum(count for _, count in self.offenders)
        write(f"session total: {total} routeless send(s)")
        write(f"attributed to individual tests: {attributed}")
        write(
            f"unattributed (collection / import time / session fixtures): "
            f"{total - attributed}"
        )
        if self.offenders:
            write("")
            write("tests that attempted egress:")
            for nodeid, count in sorted(
                self.offenders, key=lambda pair: -pair[1]
            ):
                write(f"  {count:5d}  {nodeid}")


class _NameResolution:
    """Record every hostname the suite asks to resolve, in-process.

    Measured 2026-07-20: one `getaddrinfo` costs exactly 4 routeless packets and
    one `connect` costs 1, which is how 96 packets resolved to 24 tests doing one
    lookup each. Knowing the COUNT identified the lane; knowing the NAME is what
    tells you whether the endpoint redirect is working.

    This only sees the parent process. Lookups inside hook subprocesses are
    counted by the kernel but not named here -- the counter is the boundary
    evidence, this is a convenience for the common case.
    """

    def __init__(self) -> None:
        self.names: dict[str, int] = {}
        self._real = None

    def pytest_configure(self, config):
        import socket

        self._real = socket.getaddrinfo
        recorder = self.names
        real = self._real

        def getaddrinfo(host, *args, **kwargs):
            key = str(host)
            recorder[key] = recorder.get(key, 0) + 1
            return real(host, *args, **kwargs)

        socket.getaddrinfo = getaddrinfo

    def pytest_unconfigure(self, config):
        if self._real is not None:
            import socket

            socket.getaddrinfo = self._real

    def pytest_terminal_summary(self, terminalreporter):
        write = terminalreporter.write_line
        write("")
        write("=== name resolution attempted from the pytest process ===")
        if not self.names:
            write("none")
            return
        for name, count in sorted(self.names.items(), key=lambda pair: -pair[1]):
            write(f"  {count:5d}  {name}")


def pytest_configure(config):
    config.pluginmanager.register(_Attribution(), "allostat_egress_attribution")
    config.pluginmanager.register(_NameResolution(), "allostat_name_resolution")
