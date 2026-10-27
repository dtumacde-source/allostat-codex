#!/usr/bin/env bash
#
# Negative controls for process-external containment.
#
# These cover the exact lanes that defeated three rounds of in-process
# containment. Under OS-level denial they pass trivially, and that IS the
# result being reported -- not a hard-won win. The claim is that the lanes
# stopped being lanes, and the way to show it is to drive each one and watch it
# fail to escape.
#
#   -S   skips the site machinery, so sitecustomize is never imported
#   -E   ignores PYTHONPATH, so the guard directory is not even on the path
#   -I   isolated mode: both of the above, plus cwd removed from sys.path
#   non-Python child: a shell reaching out directly, which no Python hook has
#                     ever been able to touch
#
# Each control asserts TWO things, because either one alone can lie:
#   * the connection did not succeed
#   * the in-process guard was genuinely ABSENT, so the block came from the
#     namespace and not from the thing we are trying to prove unnecessary
#
# Without the second assertion a control passes for the wrong reason and the
# whole exercise is decorative -- which is precisely how four guards in this
# project shipped unable to fail.
#
# Run under contained_test_run.sh's namespace, or standalone via unshare.

set -uo pipefail

PYTHON_BIN="${ALLOSTAT_TEST_PYTHON:-python3}"
FAILURES=0
TARGET_HOST="1.1.1.1"
TARGET_PORT="443"

_report() {
    if [ "$2" = "0" ]; then
        echo "  PASS  $1"
    else
        echo "  FAIL  $1"
        FAILURES=$((FAILURES + 1))
    fi
}

_probe_source() {
    cat <<PY
import socket, sys
socket.setdefaulttimeout(3)

# Detecting the in-process guard: it replaces socket.socket.connect with a
# closure defined in _egress_guard/sitecustomize.py's _install(). So the
# discriminators are the replacement's module and qualname.
#
# The FIRST version of this check asked whether the string
# "ProductionNetworkAccessInTests" appeared in repr(socket.socket.connect).
# It never does -- a closure's repr is "<function _install.<locals>.connect at
# 0x...>", and the exception class name appears nowhere in it. So guard_present
# was ALWAYS False, the "blocked, but the guard was loaded" branch below was
# unreachable, and every control reported the strong result unconditionally.
#
# That is a guard that cannot fail, written into the file whose own header warns
# that a control passing for the wrong reason "is precisely how four guards in
# this project shipped unable to fail." Found by review, 2026-07-20. Measured
# after the fix: guard loaded -> module "sitecustomize", qualname
# "_install.<locals>.connect"; guard absent -> the builtin method_descriptor.
_connect = socket.socket.connect
guard_present = (
    getattr(_connect, "__module__", "") == "sitecustomize"
    and "_install" in getattr(_connect, "__qualname__", "")
)
try:
    socket.create_connection(("${TARGET_HOST}", ${TARGET_PORT}))
    outcome = "REACHED"
except OSError as exc:
    outcome = "BLOCKED:" + type(exc).__name__
print(outcome, "guard_present=" + str(guard_present))
PY
}

echo "Negative controls -- containment must hold where the Python guard cannot reach"
echo

# The guard directory is put on PYTHONPATH deliberately, so that -S/-E/-I are
# tested against the most favourable possible conditions for the guard. If it
# were absent the controls would prove nothing about those flags.
GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tests/_egress_guard" && pwd)"
export PYTHONPATH="$GUARD_DIR${PYTHONPATH:+:$PYTHONPATH}"

# PREFLIGHT: prove the guard DETECTOR works before trusting anything it says.
#
# Every control below reports `guard_present=False`, and for -S/-E/-I that is the
# correct answer -- those flags genuinely skip sitecustomize. Which means a
# detector hard-wired to False produces identical output to a working one, and
# the whole suite of controls reads exactly the same whether the second
# assertion functions or not. That is how the first version of this file shipped
# broken and looked right.
#
# So: run an ORDINARY interpreter, where the guard MUST load, and require the
# detector to say True. If it cannot see a guard that is definitely there, every
# `guard_present=False` below is uninformative and the run is aborted rather
# than reported.
detector_check="$("$PYTHON_BIN" -c "$(_probe_source)" 2>&1)"
case "$detector_check" in
    *guard_present=True*)
        echo "  ok    guard detector verified: it reports True when the guard IS loaded"
        echo
        ;;
    *)
        echo "  ABORT guard detector is broken -- it did not see the in-process guard" >&2
        echo "        under an ordinary interpreter, where the guard must be loaded." >&2
        echo "        Probe output: $detector_check" >&2
        echo "        Every guard_present=False below would be uninformative, so these" >&2
        echo "        controls cannot distinguish namespace denial from guard denial." >&2
        # DIAGNOSTICS (2026-07-25). This aborted on its first-ever real-runner
        # execution, and the abort message names the SYMPTOM without the state
        # needed to explain it -- so the first reader has to guess, and a guess
        # is what produced this whole class of defect. The guard is loaded via
        # PYTHONPATH (exported just above), so the informative facts are which
        # interpreter ran, what it actually had on sys.path, and whether the
        # guard directory resolved to something that exists.
        echo "        --- diagnostics ---" >&2
        echo "        PYTHON_BIN : $PYTHON_BIN" >&2
        echo "        whoami     : $(id -un 2>/dev/null) (uid $(id -u 2>/dev/null))" >&2
        echo "        cwd        : $PWD" >&2
        echo "        GUARD_DIR  : $GUARD_DIR" >&2
        echo "        guard file : $([ -f "$GUARD_DIR/sitecustomize.py" ] && echo present || echo MISSING)" >&2
        echo "        PYTHONPATH : ${PYTHONPATH:-<unset>}" >&2
        "$PYTHON_BIN" -c 'import sys,os;print("        exe        :",sys.executable);print("        sys.path   :",sys.path);print("        env PYTHONPATH:",os.environ.get("PYTHONPATH","<unset>"));import importlib.util as u;s=u.find_spec("sitecustomize");print("        sitecustomize resolves to:", getattr(s,"origin","<not found>"))' >&2 2>&1 || true
        echo "NEGATIVE_CONTROLS=ABORT" >&2
        exit 1
        ;;
esac

for flag in "-S" "-E" "-I"; do
    output="$("$PYTHON_BIN" "$flag" -c "$(_probe_source)" 2>&1)"
    status=1
    case "$output" in
        BLOCKED:*guard_present=False) status=0 ;;
        BLOCKED:*guard_present=True)
            echo "    (blocked, but the in-process guard was loaded under $flag --"
            echo "     this control cannot distinguish namespace denial from guard denial)"
            ;;
        REACHED*) echo "    (the connection SUCCEEDED -- containment is not holding)" ;;
    esac
    _report "python $flag  ->  $output" "$status"
done

# A non-Python child. No PYTHONPATH shim has ever reached one of these, and the
# suite runs bash, PowerShell, cmd and git subprocesses.
if command -v bash >/dev/null 2>&1; then
    if bash -c "exec 3<>/dev/tcp/${TARGET_HOST}/${TARGET_PORT}" 2>/dev/null; then
        _report "non-Python child (bash /dev/tcp)  ->  REACHED" 1
    else
        _report "non-Python child (bash /dev/tcp)  ->  BLOCKED" 0
    fi
fi

# Loopback must still work, or "containment" is just a broken suite.
if "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import socket
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
socket.create_connection(server.getsockname(), timeout=3)
PY
then
    _report "loopback still reachable" 0
else
    _report "loopback still reachable" 1
fi

echo
if [ "$FAILURES" -gt 0 ]; then
    echo "NEGATIVE_CONTROLS=FAIL ($FAILURES)"
    exit 1
fi
echo "NEGATIVE_CONTROLS=PASS"
exit 0
