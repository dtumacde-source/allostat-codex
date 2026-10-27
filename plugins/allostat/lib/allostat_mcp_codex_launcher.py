# allostat-launcher-ownership: owner=allostat kind=allostat-codex-hook-launcher format=2
"""Allostat hook launcher — the one command Codex ever runs.

    python -P -m allostat_mcp_codex_launcher <event> --harness codex

Why this module exists (H01, 2026-08-05): Codex hands hook commands to the
user's ACTIVE shell — PowerShell for most Windows users, cmd or a POSIX shell
for others — and no single quoting style parses identically across all of them
(a leading double-quoted executable path, for one, is a string rather than an
invocation under PowerShell). Instead of speaking every shell's language, the
command line above contains only bare tokens drawn from [A-Za-z0-9_.-], which
every shell AND the old whitespace-splitting Codex runtime (<=0.145) parse
identically. Every machine-specific path lives HERE, in an installed file —
never on the command line.

Why the NAME and the `-P` (2026-08-05 launcher audit, second round): the first
version of this module was called `allostat_hook`, and `python -m` searches
the current working directory AHEAD of user site-packages. A project
containing `allostat_hook.py` therefore won the lookup and ran ITS code
through Codex's globally trusted hook command. Two independent defenses now:

  1. `-P` (equivalently PYTHONSAFEPATH) removes the cwd/script directory from
     `sys.path` entirely, so no working directory can win. It is emitted
     UNCONDITIONALLY: the flag exists in CPython 3.11+, and 3.11 is already
     this package's minimum supported interpreter (`install.py::MIN_PYTHON`,
     `wrapper/pyproject.toml`), so the emitted command is one identical string
     on every supported Python — never a version-gated grammar, which is the
     exact construction two prior audits held this branch for.
  2. This module's name is one no project would plausibly place at its root,
     so the collision surface is gone rather than merely defended.

Because `-P` also hides this file's OWN directory, the launcher must not rely
on implicit sibling imports; it deliberately uses only the standard library.

Two copies of this module exist:

  - The repo/bundle copy (this file): HOOKS_DIR is None and the hooks
    directory is resolved from this file's own location (lib/../hooks) —
    never from argv, never from the environment.
  - The installed copy: generated into the user's Python user site-packages by
    the Codex installer (see codex_wiring.render_installed_launcher), with
    HOOKS_DIR baked to the installed hooks directory and the ownership
    sentinel below carrying a format version.

Dispatch runs the target hook script as its own process under THIS interpreter
(sys.executable), preserving the hook's stdin/stdout/stderr contract and exit
code exactly as when Codex invoked the script directly. Failure is loud: an
unknown event, a missing hooks directory, or a missing script prints one
diagnostic line to stderr and exits nonzero — never silence.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# --- ownership sentinel (H02) ------------------------------------------------
#
# The ownership record is the FIRST LINE of this file — a strict key/value
# comment with a format version, never matched as a loose substring, so a file
# that merely mentions Allostat is still treated as foreign and left alone.
# Format 2 makes the POSITION part of the grammar: exactly one record in the
# whole file, in the leading comment block before the first executable token,
# at column zero. (A mid-file record can sit inside a bracketed continuation
# of a foreign file and still tokenize as a column-zero comment — the exact
# forgery the 2026-08-05 remediation audit reproduced.) The installer
# replaces a user-site module only when `codex_wiring.read_launcher_ownership`
# accepts it, and `uninstall.py` consults the same single reader before
# removing anything. Keep the header line in sync with
# `codex_wiring.LAUNCHER_OWNERSHIP_LINE`; a test pins them equal.
ALLOSTAT_LAUNCHER_SENTINEL = "allostat-codex-hook-launcher"
ALLOSTAT_LAUNCHER_FORMAT = 2

HOOKS_DIR = None  # BAKED AT INSTALL — the installer substitutes this line.

EVENT_SCRIPTS = {
    "session-start": "session-start.py",
    "stop": "stop.py",
    "pre-tool-use": "pre-tool-use.py",
    "user-prompt-submit": "user-prompt-submit.py",
}

# There is deliberately NO environment-read anywhere in this module (a test
# pins the absence of the token itself). The first design signalled "this is
# the installer's proof run" through ALLOSTAT_LAUNCHER_DRY_RUN, and any
# well-formed value — pre-existing, leaked, copied, or externally supplied —
# silently turned every hook into a compile-only no-op (M02, 2026-08-05
# remediation audit, reproduced). A switch whose function is to silently
# convert every hook into a no-op has no place in the production launcher,
# however hard it is to flip: install-time diagnostics live in the
# `--install-proof` argv entrypoint below, which Codex's exact-form hook
# commands can never reach.

_USAGE = (
    "usage: python -P -m allostat_mcp_codex_launcher <event> --harness codex "
    "| --self-check | --install-proof <nonce32> <event>"
)


def _resolve_hooks_dir() -> Path:
    if HOOKS_DIR:
        return Path(HOOKS_DIR)
    return Path(__file__).resolve().parent.parent / "hooks"


def _fail(message: str, code: int) -> int:
    print(f"allostat_mcp_codex_launcher: {message}", file=sys.stderr)
    return code


def _compile_script(script: Path) -> str | None:
    """Compile `script` without running it. Returns None on success, or a
    one-line diagnostic.

    This is what makes the install-time check mean something (M01): the prior
    self-check confirmed four filenames existed and that a temp file could be
    created, so a hook that could not even parse still reported "installed".
    Compiling is the strongest assertion available without side effects."""
    try:
        source = script.read_text(encoding="utf-8")
    except OSError as exc:
        return f"unreadable: {exc}"
    try:
        compile(source, str(script), "exec")
    except SyntaxError as exc:
        return f"does not compile: line {exc.lineno}: {exc.msg}"
    except ValueError as exc:
        return f"does not compile: {exc}"
    return None


def self_check() -> int:
    """Prove this installed module can actually fire — used by the installer's
    post-write outside-in check ("installed" must mean "runs").

    Verifies the baked hooks directory exists, that all four hook scripts are
    present, and that every one of them COMPILES under this interpreter. It
    still executes no hook: compiling is the strongest assertion available
    without side effects."""
    hooks_dir = _resolve_hooks_dir()
    if not hooks_dir.is_dir():
        return _fail(f"hooks directory missing: {hooks_dir}", 3)
    missing = [s for s in EVENT_SCRIPTS.values() if not (hooks_dir / s).is_file()]
    if missing:
        return _fail(f"hook scripts missing from {hooks_dir}: {', '.join(missing)}", 3)
    for script_name in sorted(EVENT_SCRIPTS.values()):
        problem = _compile_script(hooks_dir / script_name)
        if problem is not None:
            return _fail(f"hook {script_name} {problem}", 3)
    try:
        fd, name = tempfile.mkstemp(prefix="allostat-hook-selfcheck-")
        os.close(fd)
        os.unlink(name)
    except OSError as exc:
        return _fail(f"temp marker write failed: {exc}", 3)
    print("allostat_mcp_codex_launcher self-check ok")
    return 0


def install_proof(rest: list[str]) -> int:
    """The install-time diagnostic — a SEPARATE entrypoint, fed by argv.

    Why separate (M02): the previous design signalled "this is the installer's
    proof run" through an environment variable the runtime then had to decide
    whether to trust — and any well-formed value silently turned every real
    hook into a no-op. Codex configs carry only exact-form event commands
    (`matches_canonical_hook_command` is string equality, and refuses this
    flag), so this path is unreachable from hook dispatch, and the event path
    itself carries no diagnostic branch at all. The nonce is a correlation
    token only: the installer generates it per proof run and requires it
    echoed, so stale, cached, or replayed output can never satisfy a fresh
    proof. It grants nothing — this entrypoint dispatches nothing, writes
    nothing, and touches no state.

    What it proves (M01, second closure 2026-08-06): the event's script
    exists, compiles, and IMPORTS — the module itself, in a child process
    that reproduces hook-run path semantics exactly (hooks dir first on
    sys.path, no cwd entry, same interpreter), with dispatch suppressed by
    the hooks' own `__main__` guard because the module is imported under a
    different name. Python supplies the import semantics rather than this
    proof modelling them: from-imports with missing members, missing
    qualified submodules, conditional and unreachable imports, and import
    side effects are all judged correctly BY CONSTRUCTION, because the thing
    being tested is the thing that ships. (The first closure reduced imports
    to root names via an AST walk — a model of the module, with a reviewer-
    demonstrated false success and false refusal; the model is gone.) The
    hooks' crash-armor fail-soft is honored exactly as a real event honors
    it: a module-level exit 0 — including the armor swallowing a broken
    sibling import — is the success it is at runtime. Module-level failure
    of any other kind is named loudly.

    On success, prints ONE stdout line the installer parses and validates
    field by field:

        allostat-launcher-proof<TAB>format=1<TAB>nonce=..<TAB>event=..
            <TAB>python=<hex><TAB>module=<hex><TAB>hooks=<hex>

    Path fields are os.fsencode()-hex so no console codepage can corrupt the
    installer's identity comparison — the whole point is that equality here
    is byte-exact or it is refusal."""
    if len(rest) != 2:
        return _fail(_USAGE, 2)
    nonce, event = rest
    if len(nonce) != 32 or not all(c in "0123456789abcdef" for c in nonce):
        return _fail(_USAGE, 2)
    script_name = EVENT_SCRIPTS.get(event)
    if script_name is None:
        return _fail(
            f"unknown hook event {event!r} (expected one of: "
            f"{', '.join(sorted(EVENT_SCRIPTS))})",
            2,
        )
    hooks_dir = _resolve_hooks_dir()
    script = hooks_dir / script_name
    if not script.is_file():
        return _fail(f"hook script missing: {script}", 3)
    problem = _compile_script(script)
    if problem is not None:
        return _fail(f"hook {script_name} {problem}", 3)
    prober = (
        "import importlib.util, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    '_allostat_hook_import_proof', sys.argv[2]\n"
        ")\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "try:\n"
        "    spec.loader.exec_module(module)\n"
        "except SystemExit as exc:\n"
        "    if exc.code in (0, None):\n"
        "        raise SystemExit(0)\n"
        "    print('SystemExit: %r' % (exc.code,), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "except BaseException as exc:\n"
        "    print('%s: %s' % (type(exc).__name__, exc), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "raise SystemExit(0)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-P", "-c", prober, str(hooks_dir), str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail(f"hook {script_name} import probe failed to run: {exc}", 3)
    if proc.returncode != 0:
        detail = " ; ".join(
            line.strip() for line in proc.stderr.splitlines() if line.strip()
        ) or f"import probe exited {proc.returncode}"
        return _fail(f"hook {script_name} import failed: {detail}", 3)
    fields = "\t".join(
        (
            "allostat-launcher-proof",
            "format=1",
            f"nonce={nonce}",
            f"event={event}",
            f"python={os.fsencode(sys.executable).hex()}",
            f"module={os.fsencode(str(Path(__file__).resolve())).hex()}",
            f"hooks={os.fsencode(str(hooks_dir.resolve())).hex()}",
        )
    )
    print(fields)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _fail(_USAGE, 2)
    if args[0] == "--self-check":
        return self_check()
    if args[0] == "--install-proof":
        return install_proof(args[1:])
    event = args[0]
    script_name = EVENT_SCRIPTS.get(event)
    if script_name is None:
        return _fail(
            f"unknown hook event {event!r} (expected one of: "
            f"{', '.join(sorted(EVENT_SCRIPTS))} or --self-check)",
            2,
        )
    # Diagnostics stay ASCII-only: a Windows console pipe decodes stderr as the
    # ANSI code page, and a mangled repair hint is a failed repair hint.
    script = _resolve_hooks_dir() / script_name
    if not script.is_file():
        return _fail(
            f"hook script missing: {script} - re-run the Allostat installer "
            f"(or install.py --refresh) to repair",
            3,
        )
    proc = subprocess.run([sys.executable, str(script), *args[1:]])
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
