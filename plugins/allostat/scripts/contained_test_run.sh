#!/usr/bin/env bash
#
# Process-external containment for the wrapper suite.
#
# WHY THIS EXISTS, AND WHY IT IS NOT MORE PYTHON
# ----------------------------------------------
# Three review rounds produced three in-process containment fixes -- session
# fixtures, then conftest hard-setting, then a sitecustomize shim delivered over
# PYTHONPATH -- and three escapes, every one of them in a lane Python does not
# own:
#
#   1. Collection. Pytest imports test modules before session fixtures run. At
#      import time the ambient home is live, a planted bearer is findable, the
#      endpoint resolves to production, and socket.connect is still native.
#   2. Interpreter startup flags. The guard is present for an ordinary `python`
#      and absent under -S, -E and -I, which skip the site machinery entirely.
#   3. Non-Python children. The suite runs bash, PowerShell, cmd and git
#      subprocesses. A PYTHONPATH shim does not reach them at all.
#
# Those are not three oversights. They are one fact: in-process containment
# cannot be complete by construction. The process can un-hook itself and a child
# can decline to load the hook. Every additional Python-layer patch buys one
# lane and leaves the class intact.
#
# So the boundary moved out of the process. This runner puts the entire suite
# inside a network namespace that has loopback and nothing else -- no default
# route, no non-loopback interface. A non-loopback connect fails at the kernel
# with ENETUNREACH before a packet exists. The test process cannot opt out: it
# has no route to create and no network attached to its namespace.
#
# THE SECOND HALF: A DENIAL THAT IS DISTINGUISHABLE FROM SUCCESS
# --------------------------------------------------------------
# The in-process guard raises ConnectionRefusedError. Production networking code
# catches that as an ordinary network failure, hooks catch the resulting
# MCPClientError and return success. So a guard could block egress while the test
# that attempted it stayed green, and no record of the denial existed anywhere.
# A guard whose denial is indistinguishable from success is not evidence.
#
# The kernel counts every routeless send in this namespace: Ip.OutNoRoutes in
# /proc/net/snmp, Ip6OutNoRoutes in /proc/net/snmp6. Those counters are per
# namespace, they live in the kernel rather than in the test process, and this
# script reads them AFTER the run. If anything attempted egress, the session
# fails -- even if pytest exited 0, and even if application code swallowed the
# error. Counting rather than trusting is the entire point.
#
# An earlier revision of this runner blackholed traffic through a dummy device
# and counted its TX packets. That worked, and made every blocked connect wait
# for a TCP timeout: the suite went from three minutes to over an hour. Counting
# routeless sends gives the same evidence with the failure staying instantaneous
# (measured: 0.01s for two blocked connects, versus ~3s each).
#
# The in-process guard in conftest.py stays as a fast local signal. It is NOT
# the boundary and must not be described as one.
#
# USAGE
#   contained_test_run.sh [pytest args...]
#   contained_test_run.sh --self-test     # prove the counter is not dead
#
# EXIT CODES
#   0   suite passed AND zero egress attempts
#   1   suite failed
#   2   egress was attempted (session fails regardless of the suite result)
#   97  containment could not be established -- never falls back to an
#       uncontained run, because a run that is not contained proves nothing

set -uo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ALLOSTAT_TEST_PYTHON:-python3}"

_fail_uncontained() {
    echo "CONTAINMENT_ESTABLISHED=0" >&2
    echo "containment could not be established: $1" >&2
    echo "Refusing to run the suite uncontained. A green run outside containment" >&2
    echo "is not evidence of anything -- that is what this runner exists to end." >&2
    exit 97
}

command -v unshare >/dev/null 2>&1 || _fail_uncontained "unshare not available"
command -v ip >/dev/null 2>&1 || _fail_uncontained "iproute2 (ip) not available"

# R6 FOLLOW-UP (2026-07-21) -- `-r` IS FOR THE UNPRIVILEGED PATH ONLY, AND IT
# MADE THE PRIVILEGE DROP IT WAS PAIRED WITH IMPOSSIBLE.
#
# `-r` (--map-root-user) exists so a NON-root caller can create the namespace
# at all: it writes a single-entry uid_map and makes the caller look like root
# inside it. That is what the ordinary local WSL run needs.
#
# Under real root it is actively harmful. `unshare -rn` as root writes the
# uid_map `0 0 1` and sets /proc/self/setgroups to `deny`, so INSIDE the
# namespace exactly one uid exists. The CI runner's own uid (1001 on
# ubuntu-latest) is unmapped, and setgroups is forbidden. The R6 drop --
# `setpriv --reuid=<runner> --regid=<gid> --clear-groups` -- therefore cannot
# succeed, and setpriv exits 127 before pytest is ever exec'd. Measured in WSL2
# (kernel 6.18.33.2, util-linux 2.41.3):
#
#   $ sudo unshare -rn cat /proc/self/uid_map   ->  "0  0  1"
#   $ sudo unshare -rn cat /proc/self/setgroups ->  "deny"
#   $ sudo unshare -rn bash -c 'setpriv --reuid=1000 --regid=1000 \
#       --clear-groups --no-new-privs -- id'
#     setpriv: setresuid failed: Invalid argument       (rc=127)
#   $ sudo unshare  -n bash -c 'setpriv --reuid=1000 --regid=1000 \
#       --clear-groups --no-new-privs -- id'
#     uid=1000 gid=1000 groups=1000                     (rc=0)
#
# `chown -R` into that namespace fails for the same reason (EINVAL on an
# unmapped uid), and it was silenced by `|| true` -- so even a fixed reuid
# would have handed the dropped identity a root-owned 0700 HOME.
#
# So the whole contained wrapper-suite gate -- the only process-external
# containment the 2153-test suite has -- exited 127 on every CI run without
# ever collecting a test. A gate that cannot produce a result on its subject is
# not a gate. Root already has CAP_SYS_ADMIN and needs no uid mapping, so it
# creates the namespace with `-n` alone and every uid on the host stays mapped.
if [ "$(id -u)" = "0" ]; then
    UNSHARE_FLAGS="-n"
else
    UNSHARE_FLAGS="-rn"
fi

# Probe with the flags actually used, not with a different set that happens to
# work. "Some namespace can be created" is not the claim this line makes.
unshare "$UNSHARE_FLAGS" true 2>/dev/null \
    || _fail_uncontained "network namespaces are unavailable"

SELF_TEST=0
if [ "${1:-}" = "--self-test" ]; then
    SELF_TEST=1
    shift
fi

exec unshare "$UNSHARE_FLAGS" bash -s -- "$WRAPPER_DIR" "$PYTHON_BIN" "$SELF_TEST" "$@" <<'INNER'
set -uo pipefail
WRAPPER_DIR="$1"; PYTHON_BIN="$2"; SELF_TEST="$3"
shift 3

# Loopback stays up: loopback-targeted transport tests are legitimate and must
# keep working, or containment would be indistinguishable from "break the suite
# until it stops trying". Nothing else is brought up, and no route is added --
# that absence IS the containment.
ip link set lo up || { echo "containment setup failed: lo" >&2; exit 97; }

# Refuse if anything other than loopback is reachable. Cheap, and it catches a
# future edit that helpfully adds an interface.
if ip route show | grep -q .; then
    echo "containment setup failed: a route exists in the namespace" >&2
    exit 97
fi

# R5 (2026-07-20) -- THE INSTRUMENT HAD THE DEFECT IT WAS BUILT TO DETECT.
#
# The previous version of this function returned 0 when /proc/net/snmp could not
# be opened, and 0 again when the field was not present. Both are "I could not
# read the counter", and both were reported as the number zero -- which every
# caller below reads as "nothing attempted egress".
#
# So the check that proves nothing escaped answered CLEAN when it was blind.
# That is the same fail-open shape as the four defects this sprint was convened
# to close, sitting in the thing doing the measuring. A trusted zero has to come
# from a counter that was actually read.
#
# It now refuses. Missing file, missing field, unparseable value: exit 97, the
# same "containment could not be established" code as a namespace that will not
# come up, because a measurement that did not happen is not a clean measurement.
_no_route_count() {
    "$PYTHON_BIN" "$WRAPPER_DIR/scripts/noroute_counter.py"
}

# Command substitution keeps the function's exit status, so a refusal to read
# propagates instead of becoming an empty string that arithmetic treats as 0.
_read_counter_or_die() {
    local value
    value="$(_no_route_count)" || exit 97
    case "$value" in
        ''|*[!0-9]*)
            echo "containment counter returned a non-numeric value: '$value'" >&2
            exit 97
            ;;
    esac
    printf '%s' "$value"
}

# THE CANARY -- runs on EVERY invocation, not only under --self-test.
#
# The self-test proved the counter was live, and then the ordinary path trusted
# a zero from a counter nobody had proven was live that run. A caller could
# obtain a trusted clean result simply by omitting the flag. So the proof is now
# part of every run: deliberately attempt one routeless send, require the
# counter to move, and take the post-canary reading as the baseline so the
# canary's own packets are never attributed to the suite.
CANARY_START="$(_read_counter_or_die)"
"$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import socket
socket.setdefaulttimeout(1)
for family, target in (
    (socket.AF_INET, ("192.0.2.1", 9)),
    (socket.AF_INET6, ("2001:db8::1", 9)),
):
    try:
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.sendto(b"allostat-containment-canary", target)
        s.close()
    except OSError:
        pass
PY
CANARY_END="$(_read_counter_or_die)"
if [ "$CANARY_END" -le "$CANARY_START" ]; then
    echo "CONTAINMENT_CANARY_FAIL=1" >&2
    echo "A deliberate routeless send did not move the kernel denial counter" >&2
    echo "($CANARY_START -> $CANARY_END). The instrument is dead, so a zero from" >&2
    echo "this run would mean nothing. Refusing to report a clean result from a" >&2
    echo "counter that cannot count." >&2
    exit 97
fi
echo "CONTAINMENT_CANARY_DELTA=$(( CANARY_END - CANARY_START ))"

# Baseline is taken AFTER the canary, so its packets are excluded by
# construction rather than by subtracting a number someone has to remember.
BEFORE="$CANARY_END"
echo "CONTAINMENT_ESTABLISHED=1"
echo "CONTAINMENT_BASELINE_NOROUTE=$BEFORE"

# PRE-EXEC ISOLATION -- the credential half of the same lesson.
#
# The network namespace closes egress. It does not stop test code from READING
# the operator's installed bearer, and the window where that happens cannot be
# closed by a fixture: pytest imports every test module before any session
# fixture runs. Measured directly (test_ambient_bearer_isolation.py), a child
# pytest with an ambient installed profile observes, at MODULE IMPORT:
#
#     bearer   = <the real installed token>
#     endpoint = https://mcp.allostat.ai/mcp
#
# A fixture cannot fix that, because the window is open before fixtures exist.
# The launch environment can, because it exists before the interpreter does.
# Same correction as the network lane: stop asking the process to contain
# itself, and set the conditions from outside it.
ISOLATED_HOME="$(mktemp -d)"
export HOME="$ISOLATED_HOME"
export USERPROFILE="$ISOLATED_HOME"
# The variables the production client ACTUALLY resolves, in its own order.
# Setting anything else is decorative, which is what went wrong once already.
export ALLOSTAT_MCP_URL="http://127.0.0.1:9/mcp"
export ALLOSTAT_MCP_ENDPOINT="http://127.0.0.1:9/mcp"
# R4: the installer origin. `update_check.py` held a hardcoded production URL
# with no override, and it was every one of the 96 routeless sends measured on
# 2026-07-20 -- named as `installer.allostat.ai:443` by child instrumentation.
export ALLOSTAT_INSTALLER_BASE_URL="http://127.0.0.1:9"
unset ALLOSTAT_MCP_TOKEN
echo "CONTAINMENT_ISOLATED_HOME=$ISOLATED_HOME"

# R4 -- NAME the attempts, in every process, not just the parent.
#
# The kernel counter says HOW MANY reached for a non-loopback destination. It
# cannot say WHICH HOST, and `egress_attribution.py` only ever saw the parent
# pytest process, so the 96 attempts measured on 2026-07-20 were attributed to
# 24 tests without a single hostname among them.
#
# The recorder lives in `tests/_egress_guard/sitecustomize.py` -- the module the
# conftest already puts on PYTHONPATH, which every child Python process imports
# at interpreter startup. It also patches `getaddrinfo`, which that guard did
# not: it patched `connect`, and name resolution is not `connect`. That gap IS
# the fifth escape lane.
#
# The runner's only job here is to name the log and truncate it.
#
# This was briefly a separate `usercustomize` module, chosen to avoid colliding
# with that sitecustomize. It never ran: a virtualenv sets
# ENABLE_USER_SITE=False, so `usercustomize` is not imported at all. Recorded
# because the first check of it was vacuous -- it imported the module explicitly
# and then asked whether the module had been imported.
#
# Records only. Blocking is the namespace's job, and mocking DNS to drive the
# counter to zero would hide the finding rather than fix it.
# F5 -- create the egress-probe log SECURELY. A fixed, predictable default name
# that is then truncated (`: >`) follows a pre-planted symlink at that path on
# write (CWE-377/59), and it is chown'd to the dropped uid below besides. A
# caller-supplied path is still honored (the runner's own tests set one); only
# the DEFAULT changes -- mktemp gives an O_EXCL, unpredictable name that will
# not follow a pre-existing symlink, and it is removed on exit.
if [ -n "${ALLOSTAT_EGRESS_PROBE_LOG:-}" ]; then
    export ALLOSTAT_EGRESS_PROBE_LOG
else
    ALLOSTAT_EGRESS_PROBE_LOG="$(mktemp "${TMPDIR:-/tmp}/allostat_egress_probe.XXXXXX.jsonl")" \
        || { echo "containment setup failed: could not mktemp the egress-probe log" >&2; exit 97; }
    export ALLOSTAT_EGRESS_PROBE_LOG
    trap 'rm -f "$ALLOSTAT_EGRESS_PROBE_LOG"' EXIT
fi
echo "CONTAINMENT_EGRESS_PROBE_LOG=$ALLOSTAT_EGRESS_PROBE_LOG"

# R4. Two tests exist to attempt a non-loopback connect and prove the in-process
# child guard refuses it. Their egress attempt IS the assertion, so counting it
# as unintended egress would make a correct suite permanently fail this gate.
#
# They are deselected by MARKER, not by node id, and the marker is pinned by
# `test_egress_allowance_is_pinned` -- putting it on a new test turns that test
# red. An allowance nobody can enumerate is a waiver, and the whole subject of
# this sprint is waivers that hid defects. Announced on every run so it is never
# a silent exemption.
echo "CONTAINMENT_DESELECTED_MARKER=attempts_egress (2 deliberate-egress controls; see test_egress_allowance_is_pinned)"

if [ "$SELF_TEST" = "1" ]; then
    # Non-vacuous proof: deliberately attempt egress and require the counter to
    # move. A containment check that cannot detect a real attempt is decorative,
    # and this project has shipped four guards that could not fail.
    "$PYTHON_BIN" - <<'PY'
import socket
socket.setdefaulttimeout(3)
for target in (("1.1.1.1", 443), ("8.8.8.8", 53)):
    try:
        socket.create_connection(target)
        print("SELF_TEST_REACHED", target)
    except OSError as exc:
        print("SELF_TEST_BLOCKED", target, type(exc).__name__)
PY
    STATUS=0
else
    cd "$WRAPPER_DIR" || exit 97

    # R6 -- DROP PRIVILEGES BEFORE PYTEST.
    #
    # The GitHub job runs every step under sudo, because that image restricts
    # unprivileged user namespaces and `unshare -rn` as the runner user can be
    # refused. That is a real constraint on creating the namespace. It is not a
    # reason to RUN THE SUITE as root.
    #
    # Root bypasses DAC, so the permission-semantics tests
    # (test_h07_sandbox_config_inaccessible.py, test_uninstall.py, the C-01
    # credential-hardening checks) prove their property for root rather than for
    # a customer. For a job whose thesis is "a run that is not contained is not
    # evidence", running it under an identity no customer has is the same
    # category of error.
    #
    # So: create and configure the namespace as root (already done above), then
    # drop to an unprivileged identity with no ability to regain it before the
    # suite starts. The OUTER process keeps its privileges and reads the counter
    # afterwards, which is why this drop happens here and not at the top.
    #
    # When already unprivileged (the ordinary local WSL case) this is a no-op.
    if [ "$(id -u)" = "0" ] && [ -n "${ALLOSTAT_TEST_DROP_TO_UID:-}" ]; then
        if ! command -v setpriv >/dev/null 2>&1; then
            echo "containment setup failed: setpriv is required to drop privileges" >&2
            exit 97
        fi
        _DROP_UID="${ALLOSTAT_TEST_DROP_TO_UID}"
        _DROP_GID="${ALLOSTAT_TEST_DROP_TO_GID:-$_DROP_UID}"
        # The isolated home must be reachable by the identity that will use it,
        # and FAILING TO HAND IT OVER IS FATAL. `mktemp -d` creates 0700 root-
        # owned, so a swallowed chown leaves the dropped identity with a HOME it
        # cannot read -- the suite then runs against an unreadable profile and
        # whatever it proves is not what the job claims. `|| true` here also hid
        # the namespace defect above for a full sprint: the chown was failing
        # with EINVAL on every CI run and said nothing.
        if ! chown -R "$_DROP_UID:$_DROP_GID" "$ISOLATED_HOME"; then
            echo "containment setup failed: could not give the isolated HOME" >&2
            echo "($ISOLATED_HOME) to $_DROP_UID:$_DROP_GID. A HOME the dropped" >&2
            echo "identity cannot read is not a usable privilege drop." >&2
            exit 97
        fi
        if [ -n "${ALLOSTAT_EGRESS_PROBE_LOG:-}" ]; then
            chown "$_DROP_UID:$_DROP_GID" "$ALLOSTAT_EGRESS_PROBE_LOG" 2>/dev/null || true
        fi
        # F4 -- emit the drop marker FROM the dropped process, never before it.
        # A bare `echo "...=$_DROP_UID"` here is printed even when setpriv exits
        # 127 without dropping, so the CI "did not run as root" proof was
        # satisfiable by a FAILED drop. The marker now reports `id -u` from
        # inside the setpriv-exec'd command, so only a process that actually
        # dropped can produce it.
        setpriv --reuid="$_DROP_UID" --regid="$_DROP_GID" --clear-groups \
                --no-new-privs -- \
                sh -c 'echo "CONTAINMENT_DROPPED_TO_UID=$(id -u)"; exec "$@"' _ \
                "$PYTHON_BIN" -m pytest -p no:cacheprovider \
                -m "not attempts_egress" "$@"
        STATUS=$?
        # setpriv exits 127 when it cannot drop (e.g. an unmapped uid inside the
        # namespace -- the exact R6 defect). That is not a suite result: refuse
        # with the same "containment could not be established" code as any other
        # setup failure rather than reporting a run that never dropped.
        if [ "$STATUS" -eq 127 ]; then
            echo "CONTAINMENT_DROP_FAILED=1" >&2
            echo "setpriv could not drop to $_DROP_UID (exit 127); the suite did" >&2
            echo "NOT run under the unprivileged identity. Refusing to report a" >&2
            echo "result from a run that never dropped privileges." >&2
            exit 97
        fi
    else
        if [ "$(id -u)" = "0" ]; then
            # Loud, because a root run silently invalidates a named set of tests.
            echo "CONTAINMENT_RUNNING_AS_ROOT=1" >&2
            echo "pytest is running as root; permission-semantics tests prove" >&2
            echo "their property for root, not for a customer. Set" >&2
            echo "ALLOSTAT_TEST_DROP_TO_UID to an unprivileged uid to fix this." >&2
        fi
        "$PYTHON_BIN" -m pytest -p no:cacheprovider \
            -m "not attempts_egress" "$@"
        STATUS=$?
    fi
fi

AFTER="$(_read_counter_or_die)"

# Counter regression is fatal. A kernel counter is monotonic within a namespace,
# so AFTER < BEFORE means something is wrong with the reading itself -- a reset,
# a different namespace, a misparse -- and the subtraction below would produce a
# NEGATIVE delta that the `-gt 0` test reads as "clean".
if [ "$AFTER" -lt "$BEFORE" ]; then
    echo "CONTAINMENT_COUNTER_REGRESSION=1" >&2
    echo "the denial counter went backwards ($BEFORE -> $AFTER), so this run's" >&2
    echo "egress measurement is not trustworthy in either direction." >&2
    exit 97
fi

DELTA=$(( AFTER - BEFORE ))
echo "CONTAINMENT_EGRESS_ATTEMPTS=$DELTA"

# Name what the counter counted, while the evidence is still on disk.
if [ -n "${ALLOSTAT_EGRESS_PROBE_LOG:-}" ] && [ -s "$ALLOSTAT_EGRESS_PROBE_LOG" ]; then
    echo "--- hosts the suite tried to resolve or reach ---"
    "$PYTHON_BIN" "$WRAPPER_DIR/scripts/summarize_egress_probe.py" \
        "$ALLOSTAT_EGRESS_PROBE_LOG" || true
fi

if [ "$SELF_TEST" = "1" ]; then
    if [ "$DELTA" -gt 0 ]; then
        echo "SELF_TEST_PASS: the kernel recorded $DELTA routeless send(s)"
        exit 0
    fi
    echo "SELF_TEST_FAIL: deliberate egress produced no counter movement." >&2
    echo "The denial record is dead; a zero count from a real run would mean nothing." >&2
    exit 2
fi

if [ "$DELTA" -gt 0 ]; then
    echo "EGRESS ATTEMPTED: the kernel recorded $DELTA routeless send(s) from the" >&2
    echo "test process or one of its children, to a non-loopback destination." >&2
    echo "The session fails regardless of the suite result (pytest exit $STATUS)," >&2
    echo "because application code catching the error and returning success is" >&2
    echo "exactly the case this counter exists to catch." >&2
    exit 2
fi

exit "$STATUS"
INNER
