#!/bin/sh
# Allostat cross-platform hook launcher.
#
# WHY THIS FILE EXISTS (2026-08-07): hooks.json used to spawn bare `python`,
# which does not exist on default Ubuntu/Debian or macOS (python3-only).
# Every hook then died AT SPAWN, silently: no state dir, no observations, no
# memory injection, no handoffs — while the remote HTTP MCP server still
# connected, so the install LOOKED healthy. A memory written by an agent was
# never surfaced again. Confirmed live in the Linux bench VM; the operator's
# Windows machine never saw it because Windows Python registers `python.exe`.
# (.sh name: the bundle publish gate ships only allowlisted file types, and
# the repo's *.sh gitattributes rule pins the LF endings sh requires.
# hooks.json invokes this file as `sh "<path>" <name>` — interpreter-
# prefixed, so NO executable bit is required: a Windows-side git add
# records mode 644, and a bare "<path>" invocation would then EACCES on
# every POSIX clone — dead hooks all over again.)
#
# Claude Code executes shell-form hook commands via sh on macOS/Linux and
# via Git Bash on Windows (PowerShell only when Git Bash is absent — that
# rare path fails VISIBLY as a hook error, never silently). So one POSIX sh
# script covers every mainstream platform. It is deliberately NOT a cmd
# polyglot: cmd.exe requires CRLF line endings while the sh half requires
# LF, so a single file cannot serve both — and cmd is not a Claude Code
# hook path. LF endings are pinned by .gitattributes; a CRLF copy of this
# file breaks sh with "\r: command not found" (the repo has hit that class
# twice: *.sh rule, cron AR-04).
#
# Interpreters are resolved by EXECUTION PROBE, not `command -v` alone:
# macOS ships a CLT-stub /usr/bin/python3 and Windows ships a Store-stub
# python3.exe, both of which LOOK present but cannot run. The probe rejects
# stubs and falls through. `exec` hands the process over so the hook's exit
# code — including the PreToolUse blocking contract (exit 2 + stderr) —
# propagates untouched.
#
# If NO interpreter works, session-start emits a static additionalContext
# JSON so the agent itself tells the operator the install is broken; other
# hooks stay silent (one loud line per session, not one per tool call).
# The silent-death mode is the bug; never recreate it.
#
# Usage: run_hook <hook-name>   (runs <hook-name>.py beside this file)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_NAME="$1"
[ -n "$HOOK_NAME" ] || exit 0
shift 2>/dev/null || true
for PY in python3 python; do
    if "$PY" -c "import sys" >/dev/null 2>&1; then
        exec "$PY" "$SCRIPT_DIR/$HOOK_NAME.py" "$@"
    fi
done
if [ "$HOOK_NAME" = "session-start" ]; then
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"[allostat] BROKEN INSTALL: no working Python 3 interpreter on PATH. Allostat is installed but its hooks cannot run - memory, continuity, and regulation are OFFLINE this session. Surface this to the operator verbatim, and tell them to install Python 3 (Windows: https://python.org; macOS: xcode-select --install or brew install python3; Debian/Ubuntu: sudo apt install python3), then restart Claude Code."}}'
fi
exit 0
