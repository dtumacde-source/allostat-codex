"""Codex install/uninstall — marker-fenced block writer for ~/.codex/config.toml.

The Codex adapter (1.4.56) routes hook events + the MCP server into Codex via a
block in `~/.codex/config.toml`. This module owns that block: it writes it on
install and removes it on uninstall, symmetrically, using a single pair of fence
markers so the two operations can never drift apart.

Design invariants (advisor 2026-07-06 installer asks):

  1. MARKER-FENCED, never a TOML reformat. We only ever add/remove the text
     BETWEEN the fences; every other line in config.toml is byte-preserved. A
     partial or hand-edited config is never corrupted — worst case the block is
     re-appended fresh. (install is idempotent: any existing fenced block is
     replaced, not duplicated.)
  2. ENV-ONLY TOKEN, structurally. render_block() accepts only the env-var NAME
     (token_env_var) — there is no parameter to pass a raw secret, so the
     installer physically cannot write a plaintext token into config.toml. Codex
     reads the bearer from the environment via `bearer_token_env_var`.
  3. WRAPPER-ONLY on uninstall. remove_block() strips exactly this managed block
     (the MCP registration + the four hooks). It touches nothing else — no user
     memory, no state, no other config. Removing the block fully disables the
     Codex integration; the user's files are never in scope here.

Emission (2026-08-05, H01 closure — the launcher grammar): every hook `command`
line is the CANONICAL INTERSECTION GRAMMAR —
`python -P -m allostat_mcp_codex_launcher <event> --harness codex` — every
token bare, drawn from [A-Za-z0-9_.-]; one form for every shell Codex may hand
the command to (cmd with or without DelayedExpansion, PowerShell, POSIX) AND
for the old whitespace-splitting runtime (<=0.145). No machine path, no
quoting, and NO version probe on the emit path; machine paths live in the
launcher module the installer generates into the user's site-packages
(`render_installed_launcher`), which the `-P` keeps a project directory from
shadowing. Do not hand-write that command anywhere: `canonical_hook_command()`
is the single source, and the shipped documentation is RENDERED from it
(`wrapper/tests/_launcher_doc_contract.py`).

The per-shell quoting/refusal machinery below (`_codex_command_token`,
`_windows_short_path`, the quote-aware recognition) is RETAINED for
recognizing and migrating LEGACY configs — it is never used to emit.
`codex_cli_version()` remains only for the update notice.

All functions are pure text transforms (no I/O) except read_config/write_config,
`render_installed_launcher` (reads the launcher source shipped beside this
module), and the codex version probe (`_default_version_probe`, update-notice
only). Output is verified to parse as TOML by the tests (tomllib).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
import tomllib

# The fence pair. Stable forever — install and uninstall both key on these, and
# they double as a human "managed, do not edit" signal. Never change the text of
# an already-shipped marker or old installs become un-uninstallable.
_BEGIN = "# >>> allostat (managed block — `allostat uninstall` removes this; do not edit) >>>"
_END = "# <<< allostat <<<"

# The four harness-agnostic hooks the Codex adapter wires (same wrapper hooks
# Claude Code uses, invoked with --harness codex).
_HOOKS = ("SessionStart", "Stop", "PreToolUse", "UserPromptSubmit")
_HOOK_SCRIPT = {
    "SessionStart": "session-start.py",
    "Stop": "stop.py",
    "PreToolUse": "pre-tool-use.py",
    "UserPromptSubmit": "user-prompt-submit.py",
}

_DEFAULT_SERVER_URL = "https://mcp.allostat.ai/mcp"
_DEFAULT_TOKEN_ENV_VAR = "ALLOSTAT_CODEX_MCP_TOKEN"

# C-01: Codex documents hook timeouts in seconds.  Fifteen seconds is the
# backstop above the wrapper's internal ~12s MCP retry budget, so a hung network
# call cannot block a Codex turn indefinitely.
_HOOK_TIMEOUT_SECONDS = 15

# --- the canonical intersection grammar (H01 closure, 2026-08-05) ------------
#
# ONE command per hook, identical for every shell and every Codex generation:
#
#     python -P -m allostat_mcp_codex_launcher <event> --harness codex
#
# Every token is bare and drawn from _INTERSECTION_CHARS, so cmd (/V:ON or
# /V:OFF), PowerShell 5.1/7, POSIX shells, and the <=0.145 whitespace-splitting
# runtime all parse it identically (measured GO — hub
# audits/20260805_codex_launcher_gate_measured.md: 18/18 real-shell cells plus
# real codex 0.146 on hostile roots). Machine paths never appear on the command
# line — they live in the installed launcher module. Emission, validation,
# migration, docs pins, and the matrix suite ALL derive the command from
# canonical_hook_command; no caller hand-writes the string (M01's structural
# fix: nothing runtime-resolved exists for two probes to disagree about).
#
# `-P` and the module NAME are the second-round H01 closure (2026-08-05
# launcher audit). `python -m` searches the current working directory ahead of
# user site-packages, so the original short name `allostat_hook` could be
# shadowed by a project file of the same name, which then executed through
# Codex's globally trusted hook command. `-P` removes cwd from `sys.path`
# outright, and the long owned name removes the collision surface itself.
#
# `-P` is emitted UNCONDITIONALLY and that is load-bearing: the flag exists in
# CPython 3.11+, and 3.11 is already this package's minimum supported
# interpreter, so one identical string is correct on every supported Python.
# A version-CONDITIONAL flag would rebuild the exact discriminator construction
# two prior audits held this branch for. `test_min_python_floor_permits_
# unconditional_safe_path` pins the floor so lowering it fails the suite
# instead of silently splitting the grammar.

CANONICAL_PYTHON_TOKENS = ("python", "py", "python3")
#: Emitted between the interpreter token and `-m`. Bare, inside the
#: intersection set, and unconditional — see the note above.
CANONICAL_SAFE_PATH_FLAG = "-P"
#: The lowest interpreter version on which CANONICAL_SAFE_PATH_FLAG exists.
#: Must stay <= the package's MIN_PYTHON for the flag to be unconditional.
SAFE_PATH_FLAG_MIN_PYTHON = (3, 11)
_CANONICAL_MODULE = "allostat_mcp_codex_launcher"
_EVENT_TOKEN = {
    "SessionStart": "session-start",
    "Stop": "stop",
    "PreToolUse": "pre-tool-use",
    "UserPromptSubmit": "user-prompt-submit",
}
_INTERSECTION_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.- "
)


def canonical_hook_command(hook: str, *, python_token: str = "python") -> str:
    """The one command line emitted for `hook`, in the intersection grammar.

    python_token is the interpreter name the installer selected and PROVED by
    outside-in self-check (install/codex/install.py) — always a member of
    CANONICAL_PYTHON_TOKENS, never a path."""
    if python_token not in CANONICAL_PYTHON_TOKENS:
        raise ValueError(
            f"python_token must be one of {CANONICAL_PYTHON_TOKENS}, got {python_token!r}"
        )
    if hook not in _EVENT_TOKEN:
        raise ValueError(
            f"unknown hook {hook!r} (expected one of {tuple(_EVENT_TOKEN)})"
        )
    command = (
        f"{python_token} {CANONICAL_SAFE_PATH_FLAG} -m {_CANONICAL_MODULE} "
        f"{_EVENT_TOKEN[hook]} --harness codex"
    )
    stray = set(command) - _INTERSECTION_CHARS
    if stray:  # unreachable today; a tripwire should the constants ever drift
        raise AssertionError(f"canonical command left the intersection set: {stray!r}")
    return command


def matches_canonical_hook_command(command: str, hook: str) -> bool:
    """Exact-form validation: True only when `command` IS the canonical line
    for `hook` under one of the allowed interpreter tokens. String equality
    against the same constant emission uses — no tokenizer, no version probe,
    nothing runtime-resolved (the M01 closure by construction)."""
    if hook not in _EVENT_TOKEN:
        return False
    return any(
        command == canonical_hook_command(hook, python_token=token)
        for token in CANONICAL_PYTHON_TOKENS
    )


def proof_hook_command(hook: str, *, python_token: str = "python", nonce: str) -> str:
    """The install-proof invocation for `hook` — same grammar, same alphabet,
    same constants as `canonical_hook_command`, plus the `--install-proof`
    flag and the per-run nonce. Never written into any config (the exact-form
    validator refuses any command carrying the flag): the installer runs this
    through the shell lane at proof time and validates the identity line the
    launcher prints. Emission stays single-sourced here for the same M01
    reason as the canonical line — no caller hand-writes command strings."""
    if python_token not in CANONICAL_PYTHON_TOKENS:
        raise ValueError(
            f"python_token must be one of {CANONICAL_PYTHON_TOKENS}, got {python_token!r}"
        )
    if hook not in _EVENT_TOKEN:
        raise ValueError(
            f"unknown hook {hook!r} (expected one of {tuple(_EVENT_TOKEN)})"
        )
    if len(nonce) != 32 or not all(c in "0123456789abcdef" for c in nonce):
        raise ValueError("nonce must be 32 lowercase hex characters")
    command = (
        f"{python_token} {CANONICAL_SAFE_PATH_FLAG} -m {_CANONICAL_MODULE} "
        f"--install-proof {nonce} {_EVENT_TOKEN[hook]}"
    )
    stray = set(command) - _INTERSECTION_CHARS
    if stray:  # unreachable today; a tripwire should the constants ever drift
        raise AssertionError(f"proof command left the intersection set: {stray!r}")
    return command


_LAUNCHER_SENTINEL = (
    "HOOKS_DIR = None  # BAKED AT INSTALL — the installer substitutes this line."
)

#: The module file name the installer writes into user site-packages.
LAUNCHER_MODULE_FILENAME = f"{_CANONICAL_MODULE}.py"

# --- ownership sentinel (H02, 2026-08-05; format 2 same day) -----------------
#
# The installer may replace a user-site launcher ONLY when the file already on
# disk carries this exact structured line. Parsing is strict (fixed key order,
# exact values, integer format version) rather than a substring search, so a
# foreign file that merely mentions Allostat is still foreign. The line lives
# in the launcher source itself; `read_launcher_ownership` is the one reader,
# shared by the installer (before writing) and uninstall (before removing).
#
# Format 2 is the LEADING-HEADER grammar, and the position is part of the
# format: exactly one record in the whole file, in the leading comment block
# before the first executable token, at column zero. Format 1 records lived
# mid-file, where the tokenizer also emits column-zero comments INSIDE
# bracketed continuations — which let a valid foreign file carry the record
# as a real comment inside a parenthesized expression and be classified as
# ours (reviewer, 2026-08-05 remediation audit; the same data-loss class as
# the original H02). Mid-file records are therefore no longer ownership,
# whoever wrote them.
LAUNCHER_OWNERSHIP_LINE = (
    "# allostat-launcher-ownership: owner=allostat "
    "kind=allostat-codex-hook-launcher format=2"
)
_OWNERSHIP_RE = re.compile(
    r"^#\s*allostat-launcher-ownership:\s*owner=(?P<owner>[A-Za-z0-9_.-]+)\s+"
    r"kind=(?P<kind>[A-Za-z0-9_.-]+)\s+format=(?P<format>\d+)\s*$"
)
LAUNCHER_OWNER = "allostat"
LAUNCHER_KIND = "allostat-codex-hook-launcher"
LAUNCHER_FORMAT = 2


def _ownership_records(text: str) -> tuple[list[str], int]:
    """(header_records, total_matches) under the strict leading-header grammar.

    Why a token walk rather than a line scan: a line-anchored regex also
    matches inside a triple-quoted string, so a file that merely QUOTES our
    ownership record — a vendored copy, a doc example, a fork's docstring —
    would be mistaken for ours and overwritten (found attacking the first
    version, 2026-08-05 self-review). Why the header rule on top of that: the
    tokenizer emits comments inside bracketed continuations at column zero
    too, so a valid foreign file could carry the record as a REAL comment
    inside a parenthesized expression and still look "module-level" to a
    column check (reviewer, 2026-08-05 remediation audit — reproduced, with
    the foreign bytes destroyed by a successful install). The header block —
    everything before the first token that is not a comment, a blank line, or
    the encoding marker — cannot sit inside any expression, so bracket depth
    is zero there by construction.

    A file that does not tokenize as Python has no comments we can trust, and
    is therefore not ours."""
    import io
    import tokenize

    header: list[str] = []
    total = 0
    in_header = True
    try:
        for token in tokenize.generate_tokens(io.StringIO(text or "").readline):
            if token.type == tokenize.COMMENT:
                if _OWNERSHIP_RE.match(token.string.strip()):
                    total += 1
                    if in_header and token.start[1] == 0:
                        header.append(token.string)
            elif token.type not in (tokenize.NL, tokenize.ENCODING):
                # The first real token — code, a docstring, even an opening
                # bracket — ends the header block for good.
                in_header = False
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return [], 0
    return header, total


def read_launcher_ownership(text: str) -> dict | None:
    """Parse the structured ownership record out of a launcher file's text.

    Returns {"owner", "kind", "format"} ONLY when the file carries EXACTLY ONE
    ownership-shaped record and that record is a real comment in the leading
    header block, at column zero, before the first executable token. A file
    without such a record — or with more than one, anywhere, at any position —
    is foreign, and the caller must refuse to overwrite or remove it: an
    ambiguous claim of ownership is not ownership. This is the only thing
    standing between the installer/uninstaller and someone else's module
    (H02), so the grammar is deliberately an identity test, never a
    resemblance test."""
    header, total = _ownership_records(text)
    if total != 1 or len(header) != 1:
        return None
    match = _OWNERSHIP_RE.match(header[0].strip())
    if match is None:  # unreachable: header entries already matched the regex
        return None
    try:
        record = {
            "owner": match.group("owner"),
            "kind": match.group("kind"),
            "format": int(match.group("format")),
        }
    except ValueError:
        return None
    if record["owner"] != LAUNCHER_OWNER or record["kind"] != LAUNCHER_KIND:
        return None
    return record


# --- legacy launcher identification (H02, uninstall path) --------------------
#
# The pre-record generator (allostat_hook.py era, built de815b8..c75f925,
# never shipped) wrote no ownership record — so the uninstaller used to fall
# back to a raw substring test for the bake sentence, and the reviewer
# demonstrated that deleting a stranger's file over one quoted sentence
# (remediation audit H02). Legacy cleanup now requires the STRUCTURE that
# generator actually emitted, all prongs conjunctive, none of them a
# substring: the file parses; HOOKS_DIR is a baked string assignment; the
# exact bake comment is a REAL comment token; EVENT_SCRIPTS is a dict literal
# with exactly our four event names. A file some person wrote does not have
# this shape unless it IS a copy of our generated module.

_LEGACY_BAKE_COMMENT = "# baked by the Allostat Codex installer"
_LEGACY_EVENT_KEYS = frozenset(
    ("session-start", "stop", "pre-tool-use", "user-prompt-submit")
)


def is_legacy_generated_launcher(text: str) -> bool:
    """Positive structural identification of the pre-record generated
    `allostat_hook.py`. Consulted ONLY for that legacy filename, ONLY by
    uninstall; the current filename is judged solely by
    `read_launcher_ownership`."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return False
    hooks_dir_is_baked_string = False
    event_keys = None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "HOOKS_DIR":
            hooks_dir_is_baked_string = isinstance(
                node.value, ast.Constant
            ) and isinstance(node.value.value, str)
        elif target.id == "EVENT_SCRIPTS" and isinstance(node.value, ast.Dict):
            keys: set | None = set()
            for key in node.value.keys:
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    keys = None
                    break
                keys.add(key.value)
            event_keys = keys
    if not hooks_dir_is_baked_string or event_keys != set(_LEGACY_EVENT_KEYS):
        return False
    import io
    import tokenize

    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if (
                token.type == tokenize.COMMENT
                and token.string.strip() == _LEGACY_BAKE_COMMENT
            ):
                return True
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return False
    return False


def classify_path_kind(path: Path) -> str:
    """What is AT `path`, without following links: "missing" | "file" |
    "symlink" | "directory" | "special". Junctions classify as "symlink" on
    Python 3.12+ (`Path.is_junction`); on 3.11 the method does not exist and
    a junction's lstat mode reads as a directory — either way a junction is a
    refuse-kind, never "file", which is the property callers rely on.

    The shared filesystem primitive under every ownership decision — the
    installer's pre-write classification and uninstall's removal sweep both
    refuse to read or remove through anything that is not a plain regular
    file, and they must agree on what that means: a divergence here is how
    one path stays hardened while the other deletes through a junction.
    install.py keeps a local twin (`_lstat_kind`) for its non-ownership
    plumbing; a parity test pins the two equal kind-for-kind."""
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return "symlink"
        except OSError:
            return "special"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "special"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def is_allostat_launcher(text: str) -> bool:
    """True only for a launcher file this installer owns and understands.

    An unknown FUTURE format version is deliberately NOT ours to overwrite:
    a newer Allostat wrote it, and an older installer clobbering it is the
    same data-loss shape H02 described."""
    record = read_launcher_ownership(text)
    return record is not None and record["format"] <= LAUNCHER_FORMAT


def render_installed_launcher(hooks_dir: str | Path) -> str:
    """The user-site launcher module source with `hooks_dir` baked in.

    Reads the launcher source shipped beside this module and substitutes its
    single HOOKS_DIR sentinel line. The baked value is a repr'd Python string
    literal, so apostrophes and backslashes in an install path can never break
    the generated file. Raises when the sentinel is missing or duplicated —
    source drift must fail the install loudly, never bake into the wrong line —
    and equally when the generated text would not carry the ownership record,
    since an unowned generated file is one the next install would refuse to
    replace."""
    source_path = Path(__file__).resolve().parent / LAUNCHER_MODULE_FILENAME
    source = source_path.read_text(encoding="utf-8")
    if source.count(_LAUNCHER_SENTINEL) != 1:
        raise RuntimeError(
            f"launcher source sentinel missing or duplicated in {source_path}; "
            "cannot bake the hooks directory"
        )
    baked_line = (
        f"HOOKS_DIR = {str(hooks_dir)!r}  # baked by the Allostat Codex installer"
    )
    generated = source.replace(_LAUNCHER_SENTINEL, baked_line)
    if not is_allostat_launcher(generated):
        raise RuntimeError(
            f"launcher source at {source_path} is missing the structured "
            f"ownership record ({LAUNCHER_OWNERSHIP_LINE!r}); refusing to "
            "generate a module the installer could not later recognise as its own"
        )
    return generated

#: CORRECTION (2026-08-05 closure audit, H01): this boundary is NOT a
#: hook-grammar discriminator and the emit path no longer consults any version.
#: Codex hands hook commands to the user's ACTIVE shell
#: (`TurnEnvironment.shell`); `%COMSPEC% /C` / `$SHELL -lc` is only the
#: empty-program fallback — and that architecture exists in BOTH 0.145 and
#: 0.146 (measured against real binaries and upstream source; the previously
#: asserted source-level boundary was wrong). The canonical intersection
#: grammar (`canonical_hook_command`) parses identically on every side, which
#: is why emission needs no discriminator at all. This constant and
#: `codex_splits_on_whitespace` survive ONLY for the update notice and for
#: recognizing LEGACY configs during migration/uninstall.
_CODEX_SHELL_EXEC_MIN = (0, 146, 0)

_CODEX_VERSION_RE = re.compile(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)")


def _default_version_probe() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise OSError("codex not on PATH")
    return subprocess.run(
        [exe, "--version"], capture_output=True, text=True, timeout=20
    ).stdout


def codex_cli_version(probe=None) -> tuple[int, int, int] | None:
    """The installed codex-cli version, or None when it cannot be determined.

    Never raises: a missing binary, a timeout, or unrecognised output all yield
    None, and callers treat None as the OLD behaviour.
    """
    try:
        raw = (probe or _default_version_probe)()
        m = _CODEX_VERSION_RE.search(raw or "")
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def codex_splits_on_whitespace(version: tuple[int, int, int] | None) -> bool:
    """True when this Codex splits hook commands on whitespace and never unquotes.

    Unknown version ⇒ True. Emitting a bare token to a shell-based runtime still
    works for space-free paths; emitting a quoted token to a splitting runtime
    puts literal quote characters into the filename and the hook never launches.
    So the conservative direction is 'assume it splits'.
    """
    if version is None:
        return True
    return version < _CODEX_SHELL_EXEC_MIN


def _toml_literal(s: str) -> str:
    """Emit `s` as a valid TOML string, preferring backslash-free literals.

    - No apostrophe → single-quoted literal `'...'` (the common case; matches the
      official Codex config example and needs no escaping).
    - Apostrophe present → a single-quoted literal would terminate early, so use
      a multi-line literal `'''...'''`, which permits stray `'` and `"` (only a
      run of `'''` terminates it). Still backslash-free.
    - Pathological `'''` in the path → fall back to a TOML basic string with
      backslash/quote escaping (correctness over prettiness).
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in s):
        return json.dumps(s, ensure_ascii=False)
    if "'" not in s:
        return f"'{s}'"
    if "'''" not in s and not s.startswith("'") and not s.endswith("'"):
        return f"'''{s}'''"
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


class CodexPathNotExpressible(ValueError):
    """A path cannot be written into a Codex hook command string.

    Raised instead of emitting a command Codex would silently fail to run. The
    message is operator-facing: the installer surfaces it verbatim and aborts.
    """


def _get_short_name(native_path: str) -> str | None:
    """Raw GetShortPathNameW. Returns None unless it yields a DIFFERENT name.

    The Win32 call only succeeds for a path that already exists, and a volume
    with 8.3 generation disabled returns the long name unchanged — both are
    reported as None so the caller can fall back or fail loudly.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        needed = get_short(native_path, None, 0)
        if not needed:
            return None
        buf = ctypes.create_unicode_buffer(needed)
        if not get_short(native_path, buf, needed):
            return None
        short = buf.value
    except Exception:
        return None
    if not short or short == native_path:
        return None
    return short


def _windows_short_path(value: str) -> str | None:
    """Return a space-free Windows 8.3 rendering of `value`, or None.

    8.3 short names are space-free by construction (`C:\\Program Files` →
    `C:\\PROGRA~1`), which is the only way to name a whitespace-bearing path in a
    command string Codex splits on whitespace.

    GetShortPathNameW only works on paths that EXIST, and the config block is
    written for hook scripts that may not be on disk yet (or at all, in tests).
    So we shorten the longest existing ANCESTOR and re-append the remaining
    components verbatim. The hook filenames Allostat ships contain no spaces, so
    the result is space-free whenever the existing part could be shortened.

    Returns None — never raises — when this is not Windows, nothing in the chain
    exists, or 8.3 generation is disabled on the volume (`fsutil 8dot3name`).
    """
    if os.name != "nt":
        return None
    native = value.replace("/", "\\")
    current = Path(native)
    tail: list[str] = []
    while True:
        try:
            exists = current.exists()
        except OSError:
            return None
        if exists:
            break
        if current.parent == current:
            return None
        tail.append(current.name)
        current = current.parent
    short = _get_short_name(str(current))
    if short is None:
        return None
    for name in reversed(tail):
        short = f"{short}\\{name}"
    return short


def _codex_command_token(value: str, *, splits_on_whitespace: bool = True, windows_shell: bool | None = None) -> str:
    """LEGACY-RECOGNITION layer: `value` as one token the way OLD emitters
    rendered it. Never called on the emit path anymore — the canonical
    launcher grammar carries no paths at all (see canonical_hook_command).
    Retained so migration and uninstall can reproduce and recognize exactly
    what old installs wrote.

    codex-cli 0.145.0 and earlier split the hook `command` on whitespace and kept
    quote characters LITERALLY, so a quoted path corrupted the filename and a
    whitespace-bearing path could not be expressed at all. Those releases got a
    BARE token, retried as the Windows 8.3 short form.

    codex-cli 0.146.0-era emitters modeled a shell runtime and quoted or
    refused characters per shell. CORRECTION (2026-08-05 closure audit, H01):
    the model those emitters assumed — `%COMSPEC% /C` on Windows, `$SHELL -lc`
    on POSIX — was the empty-program FALLBACK only; Codex actually hands hook
    commands to the user's ACTIVE shell (PowerShell for most Windows users),
    in BOTH 0.145 and 0.146, which is why no per-shell quoting model could be
    right and the launcher grammar superseded this path. The behavior below is
    preserved bit-for-bit anyway, because recognizing what old emitters WROTE
    requires reproducing what they DID: bare for [A-Za-z0-9_./:+-]; quoted
    otherwise; refusal (with the recorded reason text) for characters the
    modeled shell could not neutralize inside double quotes — on Windows `"`,
    `%`, and `!` (delayed expansion), on POSIX `"`, `$`, backtick, backslash.

    (A `command` + `args` array was also tested against 0.145: Codex ignores
    `args` silently and reports the hook Completed while running nothing. Never
    emit that form.)
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CodexPathNotExpressible(
            f"Path contains a control character and cannot be used in a Codex "
            f"hook command: {value!r}"
        )

    if not splits_on_whitespace:
        # A shell runtime: determine if we're on Windows or POSIX
        on_windows = windows_shell if windows_shell is not None else (os.name == "nt")

        # Safe character set: only bare these characters
        safe_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:+-")

        # Unquotable characters (remain dangerous even inside double quotes)
        if on_windows:
            # cmd.exe: double quote, % (var expansion), and ! (delayed-expansion
            # !VAR! substitution) are unquotable.
            unquotable = {'"', '%', '!'}
            unquotable_desc = (
                "double quote, percent sign, or exclamation point (cmd.exe expands "
                "%VAR% even in quotes, and substitutes !VAR! even in quotes when "
                "delayed expansion is on — either /V:ON or the DelayedExpansion "
                "registry value)"
            )
        else:
            # POSIX shell: double quote, $, backtick, and backslash are unquotable
            unquotable = {'"', '$', '`', '\\'}
            unquotable_desc = "double quote, dollar sign, backtick, or backslash (all live in POSIX double quotes)"

        # Check for unquotable characters first
        for char in value:
            if char in unquotable:
                raise CodexPathNotExpressible(
                    f"Path contains {unquotable_desc}, which cannot be escaped\n"
                    f"in a {('cmd.exe' if on_windows else 'POSIX shell')} command:\n"
                    f"    {value}\n"
                    f"Fix: install under a path without that character, then re-run the installer."
                )

        # If all characters are in the safe set, return bare
        if all(char in safe_chars for char in value):
            return value

        # Otherwise quote the token (metacharacters like &, |, <, >, ^, space are now safe)
        return f'"{value}"'

    if _codex_token_is_clean(value):
        return value

    short = _windows_short_path(value)
    if short is not None:
        short = short.replace("\\", "/")
        if _codex_token_is_clean(short):
            return short

    offender = "whitespace" if any(c.isspace() for c in value) else "a quote character"
    raise CodexPathNotExpressible(
        f"This Codex splits a hook command on whitespace and never unquotes it, "
        f"so a path containing {offender} cannot be wired:\n"
        f"    {value}\n"
        "Windows 8.3 short-name generation would normally solve this but is "
        "unavailable here (the path may not exist yet, or 8.3 names are disabled "
        "on this volume — check `fsutil 8dot3name query`).\n"
        "Fix: upgrade to codex-cli 0.146.0 or newer, which runs hook commands "
        "through a shell and accepts quoted paths; or install under a path with "
        "no spaces or quotes, or re-enable 8.3 name generation, then re-run the "
        "installer."
    )


def _codex_token_is_clean(value: str) -> bool:
    """True when `value` can be emitted bare AND parsed back symmetrically.

    An APOSTROPHE is permitted: Codex keeps it literally, and `_parse_hook_command`
    falls back to whitespace splitting when shlex chokes on the unbalanced quote,
    so `C:/Users/O'Brien/...` round-trips. 8.3 shortening cannot help there — an
    apostrophe is a legal 8.3 character, so `O'Brien` is left as-is — and refusing
    would lock a legitimately-named user out of the product entirely.

    A DOUBLE quote is still refused: illegal in Windows filenames, and genuinely
    ambiguous to recover on POSIX.
    """
    return not any(char.isspace() for char in value) and '"' not in value


_SANDBOX_HEADER = "[sandbox_workspace_write]"


def has_foreign_sandbox_table(config_text: str) -> bool:
    """True if config.toml declares `[sandbox_workspace_write]` OUTSIDE our fence.

    `sandbox_workspace_write` is a single top-level TOML table, so emitting our
    own while the user already has one produces a duplicate-key file that Codex
    refuses to load — strictly worse than not writing it. When this returns True
    the installer must leave the user's table alone and tell them what to add,
    which also honours the invariant that nothing outside the fences is touched.
    """
    remainder = _strip_existing(config_text)
    return any(line.strip() == _SANDBOX_HEADER for line in remainder.splitlines())


def render_sandbox_table(writable_root: str | Path) -> list[str]:
    """The `[sandbox_workspace_write]` lines granting hooks what they need.

    Codex runs hooks with `writable_roots: []` and `network_access: false`
    unless told otherwise. Allostat's hooks persist state under the install root
    and call the MCP server, so BOTH fail silently without this table.

    Scope is exactly the Allostat state directory — never a wider root, and
    never `sandbox_mode = danger-full-access`. The path is emitted as a TOML
    LITERAL string: a basic string would read `C:\\Users\\...` as an escape and
    `\\U` is a unicode escape, which breaks the file outright.
    """
    # Use the path exactly as given: str(Path) already yields the platform's
    # native separators, and rewriting separators here would corrupt a POSIX
    # path when the installer happens to run on Windows.
    return [
        "",
        _SANDBOX_HEADER,
        "network_access = true",
        f"writable_roots = [{_toml_literal(str(writable_root))}]",
    ]


def render_block(
    *,
    server_url: str = _DEFAULT_SERVER_URL,
    token_env_var: str = _DEFAULT_TOKEN_ENV_VAR,
    sandbox_writable_root: str | Path | None = None,
    python_token: str = "python",
) -> str:
    """Render the managed config.toml block (between the fences, inclusive).

    Every hook `command` is the canonical intersection line from
    `canonical_hook_command()` — identical for ALL Codex generations and every
    shell, carrying no machine path and no character outside the intersection
    set. There is deliberately NO version probe here and NO path parameter:
    the installed launcher module owns the machine paths (H01/M01 closure,
    2026-08-05; supersedes the version-gated bare-vs-quoted emission, whose
    machinery survives in this module only to recognize legacy configs).

    python_token is the installer-selected interpreter name — a member of
    CANONICAL_PYTHON_TOKENS, proven able to fire the installed module by the
    installer's post-write self-check before the config is trusted.

    NOTE the signature: there is NO raw-token parameter, only token_env_var
    (a NAME). The block wires Codex to read the bearer from the environment —
    a plaintext secret can never be written here (env-only-token invariant).
    """
    lines = [
        _BEGIN,
        "[mcp_servers.allostat]",
        f'url = "{server_url}"',
        f'bearer_token_env_var = "{token_env_var}"',
    ]
    for hook in _HOOKS:
        command = canonical_hook_command(hook, python_token=python_token)
        lines += [
            "",
            f"[[hooks.{hook}]]",
            f"[[hooks.{hook}.hooks]]",
            'type = "command"',
            f"command = {_toml_literal(command)}",
            f"timeout = {_HOOK_TIMEOUT_SECONDS}",
        ]
    if sandbox_writable_root is not None:
        lines += render_sandbox_table(sandbox_writable_root)
    lines.append(_END)
    return "\n".join(lines)


# --- unfenced-orphan cleanup (dual-hook self-heal) -------------------------
#
# Before the fences existed (codex-adapter dev slices), the installer wrote
# Allostat's four Codex hooks UNFENCED. The fence-keyed strip below is blind to
# those, so a re-install stacked a fresh fenced set on top of the orphan and
# every Codex event fired TWICE. We now also remove any [[hooks.*]] table group
# whose command invokes one of Allostat's hook scripts with `--harness codex`,
# fenced or not — never touching genuine third-party hooks (different command).

# A top-level Codex hook parent header, e.g. `[[hooks.SessionStart]]` — no
# further dotted key (that would be the nested `.hooks` child, absorbed below).
_HOOK_PARENT_RE = re.compile(r"^\[\[hooks\.[^.\[\]]+\]\]$")
_HOOK_CHILD_RE = re.compile(r"^\[\[hooks\.[^.\[\]]+\.hooks\]\]$")


def _parse_hook_command(stripped: str) -> list[str] | None:
    """Parse one TOML ``command = ...`` line into argv.

    Invalid or multi-line values are not claimed.  Cleanup must prefer leaving
    ambiguous foreign configuration in place over deleting it.
    """
    if not re.match(r"^command\s*=", stripped):
        return None
    try:
        value = tomllib.loads(f"x = {stripped.split('=', 1)[1].strip()}")["x"]
        if not isinstance(value, str):
            return None
        try:
            argv = shlex.split(value, posix=True)
        except ValueError:
            # An unbalanced quote — e.g. the apostrophe in a bare
            # `C:/Users/O'Brien/...` path, which 1.4.74 emits deliberately.
            # Codex itself just splits on whitespace, so do the same rather than
            # decline to recognise a command we wrote. Ownership is still gated
            # by _is_owned_hook_path, so this cannot claim a foreign hook.
            return value.split()
        # Pre-fence Windows documentation emitted unquoted backslash paths.
        # POSIX shlex treats those backslashes as escapes; retry with Windows-
        # compatible tokenization only when that loss is observable.
        if "\\" in value and not any("\\" in token for token in argv):
            argv = shlex.split(value, posix=False)
            argv = [
                token[1:-1]
                if len(token) >= 2
                and token[0] == token[-1]
                and token[0] in {'"', "'"}
                else token
                for token in argv
            ]
        return argv
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return None


def _is_owned_hook_path(raw_path: str) -> bool:
    """Return True only for hook locations Allostat has actually shipped.

    A script basename plus ``--harness codex`` is not an ownership marker: a
    third party can legitimately use the same basename and flag.  Recognized
    paths are restricted to the public plugin/install roots and the historical
    monorepo development root used by the pre-fence adapter.
    """
    normalized = raw_path.replace("\\", "/")
    parts = tuple(part.casefold() for part in PurePosixPath(normalized).parts)
    if len(parts) < 2 or parts[-2] != "hooks":
        return False
    if parts[-1] not in {script.casefold() for script in _HOOK_SCRIPT.values()}:
        return False

    # Current Claude marketplace and standalone Codex install roots.
    if "allostat-mcp" in parts[:-2]:
        return True

    # Earliest public documentation used ~/.claude/plugins/allostat/hooks.
    joined = "/".join(parts)
    if "/.claude/plugins/allostat/hooks/" in f"/{joined}/":
        return True

    # Pre-fence development adapters ran directly from allostat-mono*/wrapper.
    return (
        parts[-3:-1] == ("wrapper", "hooks")
        and any(part.startswith("allostat-mono") for part in parts[:-3])
    )


#: The pre-`-P`, short-named canonical module this branch emitted before the
#: 2026-08-05 launcher audit. Never emitted again; recognized so that
#: `--refresh` migrates such a block and uninstall removes it. (It was never
#: released, but it exists on this branch's own dev machines.)
_SUPERSEDED_MODULES = ("allostat_hook",)


def _is_canonical_hook_argv(argv: list[str]) -> bool:
    """True when argv is the canonical launcher invocation — current form, or
    a superseded Allostat launcher form we still own for migration.

    The module names are Allostat's reserved namespace (like the
    `mcp_servers.allostat` table), so these exact shapes are ours by
    construction — no path check applies or is possible (the canonical line
    carries none)."""
    if not argv or argv[0] not in CANONICAL_PYTHON_TOKENS:
        return False
    rest = argv[1:]
    # Current form carries the unconditional safe-path flag.
    if rest[:1] == [CANONICAL_SAFE_PATH_FLAG]:
        rest = rest[1:]
        modules = (_CANONICAL_MODULE,)
    else:
        # No flag: only a superseded form can legitimately look like this.
        modules = _SUPERSEDED_MODULES
    return (
        len(rest) == 5
        and rest[0] == "-m"
        and rest[1] in modules
        and rest[2] in _EVENT_TOKEN.values()
        and rest[3:] == ["--harness", "codex"]
    )


def _is_allostat_hook_command(stripped: str) -> bool:
    """True only for an owned, exact Codex hook invocation — canonical
    (launcher grammar) or legacy (path-bearing).

    Fenced blocks are owned by their markers.  This predicate is intentionally
    narrow because it is used to delete *unfenced* orphan groups.
    """
    argv = _parse_hook_command(stripped)
    if argv is None or len(argv) < 4:
        return False
    if _is_canonical_hook_argv(argv):
        return True
    if argv[-2:] != ["--harness", "codex"]:
        return False
    return _is_owned_hook_path(argv[-3])


def _filter_hook_parent_group(group: list[str]) -> list[str]:
    """Return `group` with only its Allostat-owned `[[hooks.<Name>.hooks]]` child
    sub-groups removed, keeping the parent header and any third-party siblings.

    A parent `[[hooks.<Name>]]` may hold MULTIPLE nested `[[hooks.<Name>.hooks]]`
    children. Dropping the whole parent because ONE child is ours would delete
    unrelated third-party sibling handlers, so we filter at child granularity:

      - No owned command anywhere in the group → return it byte-for-byte.
      - Otherwise keep the parent header + preamble + every non-owned child, and
        drop each child sub-group that carries an Allostat hook command.
      - If EVERY child is owned (the pure pre-fence orphan shape), return [] so
        the caller drops the now-empty parent too (no stranded `[[hooks.*]]`)."""
    if not any(_is_allostat_hook_command(g.strip()) for g in group):
        return group  # no Allostat residue here — byte-preserve

    # Lines between the parent header and its first child stay with the parent.
    first_child = 1
    while first_child < len(group) and not _HOOK_CHILD_RE.match(group[first_child].strip()):
        first_child += 1
    preamble = group[:first_child]

    # Split the remainder into child sub-groups, each headed by a `.hooks` table.
    children: list[list[str]] = []
    k = first_child
    while k < len(group):
        m = k + 1
        while m < len(group) and not _HOOK_CHILD_RE.match(group[m].strip()):
            m += 1
        children.append(group[k:m])
        k = m

    kept = [c for c in children if not any(_is_allostat_hook_command(g.strip()) for g in c)]
    if not kept:
        # Owned command lives in the preamble (no children) or every child is
        # ours — nothing third-party survives, so drop the whole parent group.
        return []
    result = list(preamble)
    for child in kept:
        result.extend(child)
    return result


def _strip_unfenced_allostat_hooks(config_text: str) -> str:
    """Remove Allostat-owned hook entries, preserving genuine third-party hooks
    and every other line byte-for-byte.

    A parent group runs from a `[[hooks.<Name>]]` header through its nested
    `[[hooks.<Name>.hooks]]` children + keys, up to the next parent header, any
    other top-level table header, or EOF. Removal is at CHILD granularity: only
    the `[[hooks.<Name>.hooks]]` sub-groups whose command is an Allostat hook are
    dropped. A third-party child under the SAME parent survives (the parent is
    retained while any sibling remains); a parent whose children are all ours is
    dropped entirely so no empty `[[hooks.*]]` is stranded. A third-party
    `[[hooks.SessionStart]]` with a different command is left byte-for-byte."""
    lines = config_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if _HOOK_PARENT_RE.match(lines[i].strip()):
            j = i + 1
            while j < n:
                s = lines[j].strip()
                if _HOOK_PARENT_RE.match(s):
                    break
                if s.startswith("[") and not _HOOK_CHILD_RE.match(s):
                    break
                j += 1
            kept = _filter_hook_parent_group(lines[i:j])
            if kept:
                out.extend(kept)
            else:
                # whole group removed — drop the blank line(s) it owned above it
                while out and out[-1].strip() == "":
                    out.pop()
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


_MCP_ALLOSTAT_HEADER = "[mcp_servers.allostat]"


def _has_allostat_orphan(config_text: str) -> bool:
    """True if config_text carries any Allostat Codex residue that cleanup owns.

    Two shapes, either of which is ours to remove: (1) an Allostat hook command
    (fenced or unfenced), and (2) an MCP-only wiring — the `[mcp_servers.allostat]`
    table on its own, with no fence and no hooks (a partially-applied install, or
    one whose hooks were hand-deleted). `allostat` is our reserved MCP namespace,
    so any such table is Allostat's. Detecting the table here is what lets
    remove_block/uninstall SEE an MCP-only install as something to strip — the
    strip side (_strip_orphan_mcp_table) already knew how to remove it. (M-05.)"""
    for line in config_text.splitlines():
        s = line.strip()
        if _is_allostat_hook_command(s):
            return True
        if s == _MCP_ALLOSTAT_HEADER:
            return True
    return False


def _find_fence_end(lines: list[str], start: int) -> int | None:
    """Index of the next _END fence at or after `start`, or None if unterminated."""
    for k in range(start, len(lines)):
        if lines[k].strip() == _END:
            return k
    return None


def _strip_orphan_mcp_table(config_text: str) -> str:
    """Remove an UNFENCED `[mcp_servers.allostat]` table — the residue a partial
    (missing-_END) block leaves once its dangling fence marker is dropped.
    `allostat` is our reserved MCP namespace, so any such table is ours to remove;
    leaving it would collide with the fresh table a re-install appends (a
    duplicate-key TOML error).

    Table scope follows TOML semantics: the table runs from its header until the
    NEXT `[header]` (or EOF) — blank lines and comments interspersed among its
    keys are INSIDE the table, not its end (M-05). The prior version broke on the
    first blank/comment, so a comment among the keys stranded every later key,
    which then re-attached to the PRECEDING third-party table (neighbor
    pollution) and left Allostat residue behind. We remove the header through the
    LAST key line before the next header; any trailing blanks/comments after that
    last key belong to the following table and are preserved. Third-party tables
    and every other line are byte-preserved."""
    lines = config_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == _MCP_ALLOSTAT_HEADER:
            # drop the blank line(s) this table owned above it
            while out and out[-1].strip() == "":
                out.pop()
            # Scan to the next table header; track the last KEY line so trailing
            # blanks/comments (owned by the following table) are not swallowed.
            j = i + 1
            last_key = i  # at minimum remove the header itself
            while j < n:
                s = lines[j].strip()
                if s.startswith("["):
                    break  # next table header ends this table's scope
                if s != "" and not s.startswith("#"):
                    last_key = j  # a key line extends the removed region
                j += 1
            i = last_key + 1  # resume after the last removed key
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _strip_existing(config_text: str) -> str:
    """Remove existing Allostat Codex wiring, returning the remainder.

    Passes, in order: the fenced managed block(s); any orphan `[mcp_servers.allostat]`
    table; then any UNFENCED orphan Allostat hook groups (pre-fence installs — the
    dual-hook self-heal). Tolerant of: no block, one block, (defensively) multiple
    blocks from a buggy prior write, and a PARTIAL block whose closing _END fence is
    missing (an interrupted write or a user who deleted the "do not edit" line). An
    unterminated _BEGIN never swallows to EOF — only the dangling marker is dropped,
    and the orphan passes then clean our tables — so trailing third-party config is
    preserved (M-05). Trims surrounding blank lines the wiring owned so repeated
    install/uninstall cycles don't accrete whitespace. Third-party content is
    byte-preserved.
    """
    lines = config_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == _BEGIN:
            # drop a single trailing blank line we may have added before the block
            while out and out[-1].strip() == "":
                out.pop()
            end = _find_fence_end(lines, i + 1)
            if end is not None:
                i = end + 1  # skip the whole fenced block, inclusive of _END
            else:
                # Missing _END: do NOT latch to EOF (the M-05 corruption). Drop
                # only this dangling marker; the orphan passes below strip our
                # residual tables and every foreign line survives.
                i += 1
            continue
        if stripped == _END:
            # a stray closing fence with no opening _BEGIN — drop the marker only
            i += 1
            continue
        out.append(lines[i])
        i += 1
    text = _strip_orphan_mcp_table("\n".join(out))
    return _strip_unfenced_allostat_hooks(text)


def install_block(config_text: str, block: str) -> str:
    """Idempotently install `block` into config_text.

    Removes any prior managed block first (so re-install replaces, never
    duplicates), then appends the fresh block separated by exactly one blank
    line, with a single trailing newline. Every non-block line is preserved.
    """
    base = _strip_existing(config_text).rstrip("\n")
    if base:
        return base + "\n\n" + block + "\n"
    return block + "\n"


def remove_block(config_text: str) -> tuple[str, bool]:
    """Remove the managed block. Returns (new_text, removed?).

    removed=False means there was no Allostat wiring at all — no fenced block and
    no unfenced orphan hooks (already uninstalled / never installed) — the caller
    treats that as a benign no-op, not an error.
    """
    had = _BEGIN in config_text or _has_allostat_orphan(config_text)
    if not had:
        return config_text, False
    new_text = _strip_existing(config_text).rstrip("\n") + "\n"
    return new_text, True


# --- thin file I/O wrappers (the only impure functions) ---------------------


def read_config(config_path: str | Path) -> str:
    """Read config.toml text, or '' if it doesn't exist yet (fresh Codex)."""
    p = Path(config_path)
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def _config_path_kind(path: Path) -> str:
    """Classify `path` WITHOUT following links — mirrors the standalone
    installer's `_lstat_kind` (install/codex/install.py). Returns one of
    "missing", "file", "directory", "symlink", or "special". A Windows directory
    junction is reported as "symlink" so it is refused on the same footing as a
    POSIX symlink."""
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return "symlink"
        except OSError:
            return "special"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "special"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def write_config(config_path: str | Path, text: str) -> None:
    """Write config.toml text atomically, creating parent dirs if needed.

    Stages to a temp file in the SAME directory, then os.replace() onto the
    destination — a same-filesystem rename, atomic on POSIX and Windows. An
    interrupted write (crash, full disk, kill) can never leave a half-written
    config.toml: the live file is only ever the complete old bytes or the complete
    new bytes. This is the write side of the M-05 fix — the strip side tolerates a
    partial block; this side stops one from being produced. (Default newline
    handling matches the prior Path.write_text — `\\n` → os.linesep on write.)

    REFUSE symlink/junction (and other non-regular) config paths (M-12). A
    config.toml that is a symlink is externally managed — it points at a target
    the operator owns elsewhere. os.replace() onto the link path would swap the
    LINK for a fresh regular file, silently severing that external management and
    leaving the real target stale. The standalone installer already refuses this
    (install/codex/install.py:875-877, which rejects any config whose lstat kind
    is not "missing"/"file"); the unified install/uninstall path must match. Both
    install() and uninstall() route through here, so this one gate covers both.
    """
    p = Path(config_path)
    kind = _config_path_kind(p)
    if kind not in {"missing", "file"}:
        raise ValueError(
            f"Refusing to write Codex config at {p}: path is a {kind}, not a "
            "regular file. A symlink/junction config is externally managed; "
            "os.replace would sever the link instead of updating its target. "
            "(M-12.)"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".allostat-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, p)
    except BaseException:
        # Best-effort: on a failed commit, drop the temp file so no partial write
        # lingers; a cleanup error here must not mask the original write error,
        # which we re-raise. The live config is never touched (the atomic swap).
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install(config_path: str | Path,
            *, server_url: str = _DEFAULT_SERVER_URL,
            token_env_var: str = _DEFAULT_TOKEN_ENV_VAR,
            sandbox_writable_root: str | Path | None = None,
            python_token: str = "python") -> bool:
    """Install the Codex block into config.toml (idempotent, marker-fenced).

    The block carries the canonical launcher commands only — no path parameters
    exist here anymore (the installer bakes machine paths into the launcher
    module it generates into user site-packages, not into config.toml).
    python_token is the installer-selected interpreter name.

    Returns True when the `[sandbox_workspace_write]` table was written inside
    the fence. Returns False when `sandbox_writable_root` was requested but the
    user already declares that table themselves: we then leave THEIR table
    untouched (a second one would make config.toml unparseable) and the caller
    tells them what to add. With `sandbox_writable_root=None` the return is
    False and means only "not requested".
    """
    existing = read_config(config_path)
    wrote_sandbox = (
        sandbox_writable_root is not None
        and not has_foreign_sandbox_table(existing)
    )
    block = render_block(
        server_url=server_url,
        token_env_var=token_env_var,
        sandbox_writable_root=sandbox_writable_root if wrote_sandbox else None,
        python_token=python_token,
    )
    write_config(config_path, install_block(existing, block))
    return wrote_sandbox


def uninstall(config_path: str | Path) -> bool:
    """Remove the Codex block from config.toml. Returns True if a block was
    removed, False if none was present (benign). Never touches other config."""
    text = read_config(config_path)
    new_text, removed = remove_block(text)
    if removed:
        write_config(config_path, new_text)
    return removed


def _cli(argv: list[str]) -> int:
    """Thin CLI so the PowerShell/shell installers can invoke this module:
        codex_wiring.py install   <config> [token_env_var]
        codex_wiring.py uninstall <config>
    The launcher grammar takes no path arguments — the config block never
    carries machine paths (legacy `install <config> <hooks_dir> <python_exe>
    [env_var]` is still ACCEPTED for old callers; the path arguments are
    ignored, since only the installer's generated launcher module needs them).
    Note: no raw-token argument exists — install only ever takes the env-var name."""
    if len(argv) >= 2 and argv[0] == "install":
        if len(argv) >= 4:  # legacy arity: <config> <hooks_dir> <python_exe> [env_var]
            env_var = argv[4] if len(argv) > 4 else _DEFAULT_TOKEN_ENV_VAR
        else:
            env_var = argv[2] if len(argv) > 2 else _DEFAULT_TOKEN_ENV_VAR
        install(argv[1], token_env_var=env_var)
        print(f"Codex block installed in {argv[1]}")
        return 0
    if len(argv) == 2 and argv[0] == "uninstall":
        print(f"Codex block removed: {uninstall(argv[1])}")
        return 0
    print("usage: codex_wiring.py {install <config> [env_var] "
          "| uninstall <config>}", file=__import__("sys").stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
