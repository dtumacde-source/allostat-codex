#!/usr/bin/env python3
"""Activate an Allostat install code into a harness-scoped MCP credential.

Purpose
-------
Plugin-marketplace installs deliver the plugin CODE via git (`/plugin install`),
but they do NOT run the Windows installer (`install.ps1`) that normally mints the
subscriber bearer and sets the harness's credential environment variable. Without
that token the plugin's MCP registration
has nothing to send, so the server stays dormant (silent-on-no-subscription).

This script is the store-install activation step. It exchanges a one-time install
code for the bearer at `/install/resolve` — the SAME endpoint install.ps1 uses —
then persists the bearer and sets the harness-scoped variable as a persistent User-scope
env var. It deliberately does NOT download a bundle (the store already delivered
the code) — it is the token-only slice of the installer's resolve step.

IMPORTANT
---------
* `/install/resolve` CONSUMES the one-time code and issues a device bearer.
  Do not call it speculatively — `--dry-run` therefore does NOT hit the network.
* After a successful activation the user MUST fully restart Claude Code: MCP server
  env is read at launch, so a running instance won't see the new variable.

Usage
-----
    python activate.py <CODE>
    python activate.py --dry-run <CODE>   # show what would happen; no network, no writes

Activation is pinned to a known Allostat origin.  ALLOSTAT_MCP_ENDPOINT may
select production or staging, but it cannot introduce an arbitrary bearer-
receiving host or relabel a known origin's namespace.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import ntpath
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENDPOINT = "https://mcp.allostat.ai"
TRUSTED_ACTIVATION_ENDPOINTS = {
    "https://mcp.allostat.ai": "production",
    "https://mcp-staging.allostat.ai": "staging",
}
MAX_JSON_BYTES = 1024 * 1024
ENV_VAR = "ALLOSTAT_MCP_TOKEN"
HARNESS_ENV_VARS = {
    "claude": "ALLOSTAT_CLAUDE_MCP_TOKEN",
    "codex": "ALLOSTAT_CODEX_MCP_TOKEN",
}


def _endpoint() -> str:
    value = os.environ.get("ALLOSTAT_MCP_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/mcp"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("activation endpoint must be an exact trusted HTTPS origin")
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if parsed.scheme != "https" or origin not in TRUSTED_ACTIVATION_ENDPOINTS:
        raise ValueError("activation endpoint must be a trusted HTTPS origin")
    return origin


def _resolve_url() -> str:
    return _endpoint() + "/install/resolve"


def _expected_namespace() -> str:
    endpoint = _endpoint()
    expected = TRUSTED_ACTIVATION_ENDPOINTS[endpoint]
    explicit = os.environ.get("ALLOSTAT_EXPECTED_NAMESPACE", "").strip()
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", explicit):
            raise ValueError("ALLOSTAT_EXPECTED_NAMESPACE has an invalid value")
        if explicit != expected:
            raise ValueError("activation origin and expected namespace disagree")
    return expected


def _request_json(
    url: str,
    *,
    payload: dict[str, str] | None = None,
    bearer: str | None = None,
    timeout: int = 30,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": "allostat-activate/1.1 (+https://allostat.ai)",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with _urlopen_no_redirect(request, timeout=timeout) as response:
        raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("server response exceeded the activation size limit")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("server response was not a JSON object")
    return parsed


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        del request, fp, code, message, headers, newurl
        return None


def _urlopen_no_redirect(request: urllib.request.Request, *, timeout: int):
    return urllib.request.build_opener(_NoRedirectHandler()).open(
        request,
        timeout=timeout,
    )


def preflight_activation_endpoint() -> None:
    """Prove the selected endpoint and namespace before a code is consumed."""
    _expected_namespace()
    try:
        response = _request_json(_endpoint() + "/healthz", timeout=10)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise ValueError(f"activation endpoint preflight failed: {type(exc).__name__}") from exc
    if response != {"status": "ok"}:
        raise ValueError("activation endpoint health response was not recognized")


def resolve_code(
    code: str,
    *,
    device_id: str,
    hostname: str | None = None,
) -> dict:
    """POST the code to /install/resolve; return the parsed JSON.

    Consumes the one-time code and issues a per-device bearer server-side.
    """
    payload = {"code": code, "device_id": device_id, "os": sys.platform}
    if hostname:
        payload["hostname"] = hostname
    try:
        return _request_json(_resolve_url(), payload=payload)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:
            # Best-effort: the error body is a diagnostic nicety appended to the
            # SystemExit message below; if it can't be read we still raise the
            # HTTP-code error, so swallowing here loses nothing actionable.
            pass
        raise SystemExit(
            f"ERROR: server rejected the code (HTTP {e.code}). {detail}\n"
            "Common causes: code expired (24h limit), already used, or invalid.\n"
            "Ask for a fresh install code and try again."
        )
    except urllib.error.URLError as e:
        raise SystemExit(
            f"ERROR: could not reach {_endpoint()} ({e.reason}).\n"
            "Check your internet connection and try again."
        )


def probe_credential(bearer: str, *, require_device: bool = False) -> dict:
    """Authenticate a candidate bearer and validate its complete authority."""
    if not bearer or bearer != bearer.strip() or "\x00" in bearer:
        raise ValueError("candidate credential is malformed")
    try:
        state = _request_json(
            _endpoint() + "/activation/state",
            bearer=bearer,
            timeout=15,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ValueError("server rejected the candidate credential (HTTP 401)") from exc
        raise ValueError(f"credential validation failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("credential validation endpoint is unavailable") from exc

    expected = _expected_namespace()
    if state.get("namespace") != expected:
        raise ValueError("credential belongs to an unexpected namespace")
    if state.get("credential_status") != "active":
        raise ValueError("credential is not active")
    if state.get("subscription_state") not in {"active", "trial"}:
        raise ValueError("credential has no active entitlement")
    scope = state.get("credential_scope")
    allowed_scopes = {"device"} if require_device else {"account", "device"}
    if scope not in allowed_scopes:
        raise ValueError("credential has the wrong authority scope")
    return state


def wrapper_dir() -> Path:
    """Return the authoritative plugin snapshot that loaded this helper.

    ``CLAUDE_PLUGIN_ROOT`` is ambient caller input and may name another cached
    plugin version. Activation must bind dispatcher and recovery writes to
    this loaded helper's resolved snapshot instead.
    """
    return Path(__file__).resolve().parent.parent


def persist_env_file(bearer: str) -> Path:
    """Write the bearer to <wrapper>/.env (recovery copy, matches installer)."""
    env_path = wrapper_dir() / ".env"
    _atomic_write_text(env_path, f"{ENV_VAR}={bearer}\r\n", mode=0o600)
    return env_path


CODEX_STORE = (".allostat", "codex", "credential.env")
CODEX_DEVICE = (".allostat", "codex", "device.env")
CLAUDE_DEVICE = (".allostat", "claude", "device.env")
PENDING_ACTIVATION = {
    "codex": (".allostat", "codex", "pending-activation.json"),
    "claude": (".allostat", "claude", "pending-activation.json"),
}
CODEX_BIN = (".allostat", "codex", "bin")
DEVICE_ID_ENV_VAR = "ALLOSTAT_DEVICE_ID"
CLAUDE_DEVICE_ID_ENV_VAR = "ALLOSTAT_CLAUDE_DEVICE_ID"
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
BEARER_RE = re.compile(r"^[A-Za-z0-9_-]{8,512}$")
RC_BEGIN = "# >>> allostat codex environment >>>"
RC_END = "# <<< allostat codex environment <<<"
DISPATCHER_OWNERSHIP = (
    "# allostat-dispatcher-ownership: owner=allostat "
    "kind=codex-marketplace-dispatcher format=1"
)
LAUNCHER_OWNERSHIP = "allostat-codex-marketplace-launcher format=1"
COMMAND_DISPATCHER_OWNERSHIP = (
    "# allostat-dispatcher-ownership: owner=allostat "
    "kind=marketplace-command-dispatcher format=1"
)
COMMAND_LAUNCHER_OWNERSHIP = "allostat-marketplace-command-launcher format=1"


def _lstat_kind(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attrs & reparse:
        return "reparse"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "special"


def _current_windows_sid() -> str:
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not identify the current Windows user") from exc
    matches = re.findall(r"S-\d(?:-\d+)+", result.stdout)
    if result.returncode != 0 or len(matches) != 1:
        raise RuntimeError("could not identify the current Windows user")
    return matches[0]


def _windows_security_runtime() -> tuple[Path, Path, Path]:
    """Resolve built-in Windows ACL tooling without trusting PSModulePath."""
    raw_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not raw_root:
        raise RuntimeError(
            "Windows ACL tooling is unavailable: SystemRoot is unset"
        )
    system_root = Path(raw_root)
    if not system_root.is_absolute():
        raise RuntimeError("Windows ACL tooling is unavailable: SystemRoot is invalid")
    powershell_root = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0"
    )
    powershell = powershell_root / "powershell.exe"
    module_root = powershell_root / "Modules"
    security_manifest = (
        module_root
        / "Microsoft.PowerShell.Security"
        / "Microsoft.PowerShell.Security.psd1"
    )
    if not powershell.is_file() or not security_manifest.is_file():
        raise RuntimeError(
            "Windows ACL tooling is unavailable: built-in PowerShell "
            "Security module missing"
        )
    return powershell, module_root, security_manifest


def _run_windows_security_powershell(
    script: str, *, env: dict[str, str], timeout: int = 20, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a Security-cmdlet script in a deterministic built-in module context."""
    powershell, module_root, security_manifest = _windows_security_runtime()
    child_env = env.copy()
    # Codex Desktop may supply a Python-focused PSModulePath which prevents
    # Get-Acl/Set-Acl autoloading. Pin the Windows inbox module root and import
    # the exact signed inbox module manifest before the caller's script.
    child_env["PSModulePath"] = str(module_root)
    child_env["ALLOSTAT_SECURITY_MODULE"] = str(security_manifest)
    guarded_script = r"""
$ErrorActionPreference = 'Stop'
Import-Module -Name $env:ALLOSTAT_SECURITY_MODULE -Force -ErrorAction Stop
""" + script
    try:
        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                guarded_script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Windows ACL PowerShell process could not run") from exc
    if check and result.returncode != 0:
        stderr = " ".join((result.stderr or "").split()).lower()
        if "import-module" in stderr or "microsoft.powershell.security" in stderr:
            reason = "built-in PowerShell Security module could not load"
        else:
            reason = "PowerShell Security command failed"
        raise RuntimeError(f"Windows ACL operation failed: {reason}")
    return result


def _protect_path(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        sid = _current_windows_sid()
        grant = f"*{sid}:(OI)(CI)(F)" if directory else f"*{sid}:(R,W)"
        for argv in (
            ["icacls", str(path), "/reset"],
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
        ):
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("could not apply owner-only Windows ACLs")
        return
    wanted = 0o700 if directory else 0o600
    os.chmod(path, wanted)
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("activation storage is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != wanted:
        raise RuntimeError("activation storage permissions are not owner-only")


def _verify_owner_only_file(path: Path) -> None:
    """Read-only proof that an existing credential is private to this user."""
    _validate_regular_target(path)
    if _lstat_kind(path) != "file":
        raise ValueError(f"credential store is unavailable: {path}")
    if os.name == "nt":
        sid = _current_windows_sid()
        script = r"""
$ErrorActionPreference = 'Stop'
$path = $env:ALLOSTAT_VERIFY_CREDENTIAL_PATH
$expected = [System.Security.Principal.SecurityIdentifier]::new(
    $env:ALLOSTAT_VERIFY_CREDENTIAL_SID
)
$acl = Get-Acl -LiteralPath $path
$owner = [System.Security.Principal.NTAccount]::new($acl.Owner).Translate(
    [System.Security.Principal.SecurityIdentifier]
)
if ($owner.Value -ne $expected.Value) { exit 20 }
if (-not $acl.AreAccessRulesProtected) { exit 21 }
$rules = @($acl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]
))
if ($rules.Count -ne 1) { exit 22 }
$rule = $rules[0]
if ($rule.IsInherited) { exit 23 }
if ($rule.IdentityReference.Value -ne $expected.Value) { exit 24 }
if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
    exit 25
}
exit 0
"""
        child_env = os.environ.copy()
        child_env["ALLOSTAT_VERIFY_CREDENTIAL_PATH"] = str(path)
        child_env["ALLOSTAT_VERIFY_CREDENTIAL_SID"] = sid
        try:
            result = _run_windows_security_powershell(
                script, env=child_env, check=False
            )
        except RuntimeError as exc:
            raise ValueError(
                "credential owner-only Windows ACL could not be verified"
            ) from exc
        if result.returncode != 0:
            raise ValueError("credential permissions are not owner-only")
        return
    info = path.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("credential permissions are not owner-only")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("credential permissions are not owner-only")


def _ensure_private_codex_dir(path: Path) -> None:
    home = Path.home().absolute()
    target = path.absolute()
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise ValueError("Codex activation path escapes the selected home") from exc
    if _lstat_kind(home) != "directory":
        raise ValueError("selected home is not a real directory")
    current = home
    for part in relative.parts:
        current = current / part
        kind = _lstat_kind(current)
        if kind == "missing":
            current.mkdir(mode=0o700)
        elif kind != "directory":
            raise ValueError("activation path contains a link or non-directory")
        _protect_path(current, directory=True)


def _validate_private_activation_dir(path: Path) -> None:
    """Read-only counterpart to `_ensure_private_codex_dir`."""
    home = Path.home().absolute()
    target = path.absolute()
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise ValueError("activation path escapes the selected home") from exc
    if _lstat_kind(home) != "directory":
        raise ValueError("selected home is not a real directory")
    current = home
    for part in relative.parts:
        current = current / part
        if _lstat_kind(current) != "directory":
            raise ValueError("activation path contains a link or non-directory")


def _validate_regular_target(path: Path) -> None:
    kind = _lstat_kind(path)
    if kind not in {"missing", "file"}:
        raise ValueError(f"activation target is not a regular file: {path}")


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    if path.is_relative_to(Path.home()) and path.parts[: len(Path.home().parts)] == Path.home().parts:
        # Only Allostat-owned descendants get their parent chain made private;
        # shell profiles live directly under HOME and retain their own mode.
        relative = path.absolute().relative_to(Path.home().absolute())
        if relative.parts[:2] in {
            (".allostat", "codex"),
            (".allostat", "claude"),
        }:
            _ensure_private_codex_dir(path.parent)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    _validate_regular_target(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            if mode == 0o600:
                _protect_path(tmp, directory=False)
            else:
                os.chmod(tmp, mode)
        os.replace(tmp, path)
        if mode is not None:
            if mode == 0o600:
                _protect_path(path, directory=False)
            else:
                os.chmod(path, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def validate_codex_rc_markers(text: str) -> None:
    """Accept zero markers or one ordered pair; reject every ambiguous shape."""
    starts = [i for i in range(len(text)) if text.startswith(RC_BEGIN, i)]
    ends = [i for i in range(len(text)) if text.startswith(RC_END, i)]
    if not starts and not ends:
        return
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError(
            "Allostat's Codex profile markers are missing, duplicated, or reversed; "
            "refusing to consume an install code or rewrite the profile"
        )


def validate_codex_rc_marker_bytes(data: bytes) -> None:
    """Byte-safe equivalent used for arbitrary existing shell profiles."""
    begin = RC_BEGIN.encode("ascii")
    end = RC_END.encode("ascii")
    begin_count = data.count(begin)
    end_count = data.count(end)
    if begin_count == 0 and end_count == 0:
        return
    if begin_count != 1 or end_count != 1 or data.find(begin) >= data.find(end):
        raise ValueError(
            "Allostat's Codex profile markers are missing, duplicated, or reversed; "
            "refusing to consume an install code or rewrite the profile"
        )


def _owned_text_file(path: Path, marker: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return marker in path.read_text(encoding="utf-8").splitlines()[:3]
    except OSError:
        return False


def _codex_dispatcher_plan(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
) -> dict[str, Path | str | int]:
    """Validate and describe dispatcher writes without changing user state."""
    if python_token not in {"python", "python3", "py"}:
        raise ValueError(f"unsupported Python token: {python_token!r}")
    system = platform.system() if system is None else system
    source = wrapper_dir() / "lib" / "codex_marketplace_bootstrap.py"
    if not source.is_file():
        raise FileNotFoundError(f"Codex marketplace dispatcher is missing: {source}")
    source_text = source.read_text(encoding="utf-8")
    if not source_text.startswith(DISPATCHER_OWNERSHIP + "\n"):
        raise ValueError("Codex marketplace dispatcher has no valid ownership record")

    command_dir = Path.home().joinpath(*CODEX_BIN) if bin_dir is None else Path(bin_dir)
    if system == "Windows":
        program = command_dir / "allostat-hook.py"
        program_text = source_text
        program_mode = 0o600
        launcher = command_dir / "allostat-hook.cmd"
        marker = f"@REM {LAUNCHER_OWNERSHIP}"
        launcher_text = (
            f"{marker}\r\n@echo off\r\n"
            f'{python_token} -P "%~dp0allostat-hook.py" %*\r\n'
        )
        launcher_mode = 0o700
    else:
        program = command_dir / "allostat-hook"
        program_text = f"#!/usr/bin/env {python_token}\n" + source_text
        program_mode = 0o755
        launcher = command_dir / "allostat-hook"
        marker = DISPATCHER_OWNERSHIP
        launcher_text = program_text
        launcher_mode = program_mode
    if program.exists() and not _owned_text_file(program, DISPATCHER_OWNERSHIP):
        raise FileExistsError(f"refusing to replace foreign dispatcher: {program}")
    if launcher.exists() and not _owned_text_file(launcher, marker):
        raise FileExistsError(f"refusing to replace foreign launcher: {launcher}")

    return {
        "program": program,
        "program_text": program_text,
        "program_mode": program_mode,
        "launcher": launcher,
        "launcher_text": launcher_text,
        "launcher_mode": launcher_mode,
        "bin_dir": command_dir,
    }


def materialize_codex_dispatcher(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
) -> dict[str, Path]:
    """Install the stable marketplace bootstrap and one PATH launcher."""
    plan = _codex_dispatcher_plan(
        python_token=python_token,
        system=system,
        bin_dir=bin_dir,
    )
    program = plan["program"]
    launcher = plan["launcher"]
    assert isinstance(program, Path)
    assert isinstance(launcher, Path)

    _atomic_write_text(program, str(plan["program_text"]), mode=int(plan["program_mode"]))
    if launcher != program:
        _atomic_write_text(
            launcher,
            str(plan["launcher_text"]),
            mode=int(plan["launcher_mode"]),
        )
    command_dir = plan["bin_dir"]
    assert isinstance(command_dir, Path)
    return {"program": program, "launcher": launcher, "bin_dir": command_dir}


def _command_dispatcher_plan(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
) -> dict[str, Path | str | int]:
    """Validate stable customer-command writes without changing user state."""
    if python_token not in {"python", "python3", "py"}:
        raise ValueError(f"unsupported Python token: {python_token!r}")
    system = platform.system() if system is None else system
    source = wrapper_dir() / "lib" / "marketplace_command_dispatcher.py"
    if not source.is_file():
        raise FileNotFoundError(f"marketplace command dispatcher is missing: {source}")
    source_text = source.read_text(encoding="utf-8")
    if not source_text.startswith(COMMAND_DISPATCHER_OWNERSHIP + "\n"):
        raise ValueError("marketplace command dispatcher has no valid ownership record")
    command_dir = Path.home().joinpath(*CODEX_BIN) if bin_dir is None else Path(bin_dir)
    if system == "Windows":
        program = command_dir / "allostat-command.py"
        program_text = source_text
        program_mode = 0o600
        launcher = command_dir / "allostat-command.cmd"
        marker = f"@REM {COMMAND_LAUNCHER_OWNERSHIP}"
        launcher_text = (
            f"{marker}\r\n@echo off\r\n"
            f'{python_token} -P "%~dp0allostat-command.py" %*\r\n'
        )
        launcher_mode = 0o700
    else:
        program = command_dir / "allostat-command"
        program_text = f"#!/usr/bin/env {python_token}\n" + source_text
        program_mode = 0o755
        launcher = program
        marker = COMMAND_DISPATCHER_OWNERSHIP
        launcher_text = program_text
        launcher_mode = program_mode
    if program.exists() and not _owned_text_file(program, COMMAND_DISPATCHER_OWNERSHIP):
        raise FileExistsError(f"refusing to replace foreign command dispatcher: {program}")
    if launcher.exists() and not _owned_text_file(launcher, marker):
        raise FileExistsError(f"refusing to replace foreign command launcher: {launcher}")
    return {
        "program": program,
        "program_text": program_text,
        "program_mode": program_mode,
        "launcher": launcher,
        "launcher_text": launcher_text,
        "launcher_mode": launcher_mode,
        "bin_dir": command_dir,
    }


def materialize_command_dispatcher(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
) -> dict[str, Path]:
    """Install the stable, harness-aware customer-command entry point."""
    plan = _command_dispatcher_plan(
        python_token=python_token, system=system, bin_dir=bin_dir
    )
    program = plan["program"]
    launcher = plan["launcher"]
    assert isinstance(program, Path)
    assert isinstance(launcher, Path)
    _atomic_write_text(program, str(plan["program_text"]), mode=int(plan["program_mode"]))
    if launcher != program:
        _atomic_write_text(
            launcher,
            str(plan["launcher_text"]),
            mode=int(plan["launcher_mode"]),
        )
    command_dir = plan["bin_dir"]
    assert isinstance(command_dir, Path)
    return {"program": program, "launcher": launcher, "bin_dir": command_dir}


def refresh_codex_dispatchers(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
    lock_timeout: float = 30.0,
) -> dict[str, dict[str, Path]]:
    """Refresh both durable Codex entry points as one rollback-safe unit.

    Marketplace upgrades replace the plugin snapshot but cannot replace these
    launchers.  This operation touches only Allostat-owned launcher files; it
    never reads or rewrites credentials, device identity, profiles, or plugin
    cache content.
    """
    system = platform.system() if system is None else system
    lock_path = (
        activation_lock_path("codex")
        if bin_dir is None
        else Path(bin_dir).parent / "activation.lock"
    )
    # The exclusion begins before ownership planning and transaction snapshot.
    # A failed writer therefore finishes rollback before any later refresher
    # can snapshot or commit, and process death releases the OS byte lock.
    with _TimedActivationExclusion(
        "codex", path=lock_path, timeout=lock_timeout
    ):
        command_plan = _command_dispatcher_plan(
            python_token=python_token, system=system, bin_dir=bin_dir
        )
        hook_plan = _codex_dispatcher_plan(
            python_token=python_token, system=system, bin_dir=bin_dir
        )
        paths = list(dict.fromkeys(
            Path(plan[key])
            for plan in (command_plan, hook_plan)
            for key in ("program", "launcher")
        ))
        with _ActivationTransaction(paths, windows_env=[]) as transaction:
            command = materialize_command_dispatcher(
                python_token=python_token, system=system, bin_dir=bin_dir
            )
            hook = materialize_codex_dispatcher(
                python_token=python_token, system=system, bin_dir=bin_dir
            )
            transaction.restore_file_permissions()
            transaction.commit()
    return {"command": command, "hook": hook}


def select_activation_python_token() -> str:
    """Find the safe bare token that invokes this exact interpreter."""
    current = os.path.normcase(os.path.realpath(sys.executable))
    for token in ("python", "python3", "py"):
        if shutil.which(token) is None:
            continue
        try:
            result = subprocess.run(
                [token, "-P", "-c", "import os,sys;print(os.path.realpath(sys.executable))"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and os.path.normcase(result.stdout.strip()) == current:
            return token
    raise RuntimeError(
        "activation was not launched through a stable python/python3/py token; "
        "re-run the activation skill with the platform command it displays"
    )


def _codex_rc_targets() -> list[Path]:
    targets = [Path.home() / ".profile"]
    for optional in (".bashrc", ".zshrc"):
        candidate = Path.home() / optional
        if candidate.exists():
            targets.append(candidate)
    return targets


def preflight_codex_activation(
    *,
    python_token: str,
    system: str | None = None,
    bin_dir: Path | None = None,
    reuse_existing: bool = False,
) -> dict[str, Path | str | int]:
    """Reject unsafe durable state before a one-time code can be consumed."""
    system = platform.system() if system is None else system
    plan = _codex_dispatcher_plan(
        python_token=python_token,
        system=system,
        bin_dir=bin_dir,
    )
    _command_dispatcher_plan(
        python_token=python_token,
        system=system,
        bin_dir=bin_dir,
    )
    bin_dir_path = Path(plan["bin_dir"])
    if _lstat_kind(bin_dir_path) not in {"missing", "directory"}:
        raise ValueError("Codex launcher directory is a link or non-directory")
    store = Path.home().joinpath(*CODEX_STORE)
    device = Path.home().joinpath(*CODEX_DEVICE)
    if reuse_existing:
        # A rejected durable bearer must cause zero local mutation.  Validate
        # the existing path and its ACL/mode read-only before it can be read or
        # transmitted; launcher/profile writes happen only after auth succeeds.
        _validate_private_activation_dir(store.parent)
        _validate_regular_target(store)
        _verify_owner_only_file(store)
        if _lstat_kind(device.parent) == "directory":
            _validate_private_activation_dir(device.parent)
            _validate_regular_target(device)
    else:
        for target in (store, device):
            _ensure_private_codex_dir(target.parent)
            _validate_regular_target(target)
            fd, probe_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=".allostat-preflight-",
                suffix=".tmp",
            )
            os.close(fd)
            probe = Path(probe_name)
            try:
                _protect_path(probe, directory=False)
            finally:
                probe.unlink(missing_ok=True)
    if system != "Windows":
        for profile in _codex_rc_targets():
            if profile.exists():
                _validate_regular_target(profile)
                validate_codex_rc_marker_bytes(profile.read_bytes())
    else:
        # Open the exact registry key with write rights without changing it.
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ):
            pass
    return plan


def persist_codex_store(bearer: str) -> Path:
    """Write the bearer to the DURABLE codex store, `~/.allostat/codex/credential.env`.

    Codex marketplace plan Phase 1 (2026-08-08): the plugin directory is a
    marketplace snapshot that `codex plugin marketplace upgrade` replaces
    wholesale, so the recovery `.env` inside it dies on every update — the
    PATCH-122 rotation class. This store matches install.py's layout
    (INSTALL_DIR_PARTS + RECOVERY_FILE_NAME) and is read by mcp_client's
    codex-lane candidate; rotation rewrites only this file.
    """
    if not BEARER_RE.fullmatch(bearer):
        raise ValueError("refusing to persist a malformed credential")
    store = Path.home().joinpath(*CODEX_STORE)
    _atomic_write_text(store, f"{ENV_VAR}={bearer}\n", mode=0o600)
    return store


def pending_activation_path(harness: str) -> Path:
    """Return the harness-scoped pending credential path under user storage."""
    try:
        parts = PENDING_ACTIVATION[harness]
    except KeyError as exc:
        raise ValueError(f"unsupported activation harness: {harness!r}") from exc
    return Path.home().joinpath(*parts)


def activation_lock_path(harness: str) -> Path:
    """Return the private, harness-scoped cross-process exclusion file."""
    return pending_activation_path(harness).with_name("activation.lock")


class _ActivationExclusion:
    """Crash-released cross-process exclusion for one harness activation.

    The persistent file carries no authority; the operating-system byte lock
    is the exclusion. A process exit releases that lock automatically, while
    the owner-only regular file remains available for the next activation.
    """

    def __init__(self, harness: str, *, path: Path | None = None):
        self.harness = harness
        self.path = activation_lock_path(harness) if path is None else Path(path)
        self._fd: int | None = None

    def __enter__(self):
        path = self.path
        _ensure_private_codex_dir(path.parent)
        initial_kind = _lstat_kind(path)
        _validate_regular_target(path)

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ValueError("activation exclusion file could not be opened") from exc

        try:
            if _lstat_kind(path) != "file":
                raise ValueError("activation exclusion path is not a regular file")
            opened = os.fstat(fd)
            named = path.stat()
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError("activation exclusion path changed while opening")
            if getattr(opened, "st_nlink", 1) != 1:
                raise ValueError("activation exclusion file must not be linked")

            if initial_kind == "missing":
                _protect_path(path, directory=False)
            else:
                try:
                    _verify_owner_only_file(path)
                except ValueError:
                    # A crash immediately after O_CREAT can leave only the
                    # inherited parent DACL/mode. This file contains no
                    # authority, so harden it in place and continue safely.
                    _protect_path(path, directory=False)
            _verify_owner_only_file(path)
            reopened = os.fstat(fd)
            renamed = path.stat()
            if (reopened.st_dev, reopened.st_ino) != (
                renamed.st_dev,
                renamed.st_ino,
            ):
                raise ValueError("activation exclusion path changed during validation")

            if reopened.st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            elif reopened.st_size != 1:
                raise ValueError("activation exclusion file has invalid content")
            os.lseek(fd, 0, os.SEEK_SET)

            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise ValueError(
                        f"another {self.harness} activation is already in progress"
                    ) from exc
                raise RuntimeError("activation exclusion could not be acquired") from exc
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, 1) != b"\0":
                raise ValueError("activation exclusion file has invalid content")
        except Exception:
            os.close(fd)
            raise

        self._fd = fd
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        fd, self._fd = self._fd, None
        if fd is None:
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False


def activation_exclusion(harness: str) -> _ActivationExclusion:
    """Build the per-harness exclusion acquired before any code resolution."""
    if harness not in PENDING_ACTIVATION:
        raise ValueError(f"unsupported activation harness: {harness!r}")
    return _ActivationExclusion(harness)


class _TimedActivationExclusion:
    """Wait for the crash-released harness lock with a bounded timeout."""

    def __init__(self, harness: str, *, path: Path, timeout: float):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(
                "dispatcher refresh lock timeout must be a finite number"
            )
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError(
                "dispatcher refresh lock timeout must be a non-negative finite number"
            )
        self.harness = harness
        self.path = Path(path)
        self.timeout = timeout
        self._held: _ActivationExclusion | None = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        contention = f"another {self.harness} activation is already in progress"
        while True:
            candidate = _ActivationExclusion(self.harness, path=self.path)
            try:
                candidate.__enter__()
            except ValueError as exc:
                if str(exc) != contention:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "timed out waiting for the Codex dispatcher refresh lock"
                    ) from exc
                time.sleep(min(0.05, remaining))
                continue
            self._held = candidate
            return self

    def __exit__(self, exc_type, exc, traceback):
        held, self._held = self._held, None
        if held is None:
            return False
        return held.__exit__(exc_type, exc, traceback)


def preflight_pending_activation(*, harness: str, resume: bool) -> Path:
    """Validate pending storage before a one-time code can be consumed."""
    path = pending_activation_path(harness)
    _ensure_private_codex_dir(path.parent)
    _validate_regular_target(path)
    exists = _lstat_kind(path) == "file"
    if exists:
        _verify_owner_only_file(path)
    if resume and not exists:
        raise ValueError(f"no pending {harness} activation is available")
    if not resume and exists:
        raise ValueError(
            f"a pending {harness} activation already exists; run with --resume "
            "instead of consuming another install code"
        )
    if not exists:
        # Prove this exact private directory can create, flush, protect, and
        # remove a file before the server consumes a one-time code.
        fd, probe_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".allostat-pending-preflight-",
            suffix=".tmp",
        )
        probe = Path(probe_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"pending-activation-preflight\n")
                handle.flush()
                os.fsync(handle.fileno())
            _protect_path(probe, directory=False)
        finally:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
    return path


def persist_pending_activation(
    *, bearer: str, harness: str, namespace: str
) -> Path:
    """Persist newly issued authority before proof or local activation work."""
    if not BEARER_RE.fullmatch(bearer):
        raise ValueError("refusing to persist a malformed pending credential")
    if namespace != _expected_namespace():
        raise ValueError("pending credential belongs to an unexpected namespace")
    path = pending_activation_path(harness)
    _ensure_private_codex_dir(path.parent)
    _validate_regular_target(path)
    if _lstat_kind(path) != "missing":
        raise ValueError(
            f"a pending {harness} activation already exists; run with --resume"
        )
    payload = {
        "format": 1,
        "harness": harness,
        "namespace": namespace,
        "bearer": bearer,
    }
    _atomic_write_text(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        mode=0o600,
    )
    return path


def load_pending_activation(*, harness: str) -> str:
    """Read and authenticate one harness-scoped pending credential file."""
    path = pending_activation_path(harness)
    _validate_private_activation_dir(path.parent)
    _verify_owner_only_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"pending activation is unreadable: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "harness",
        "namespace",
        "bearer",
    }:
        raise ValueError(f"pending activation has an ambiguous format: {path}")
    if payload.get("format") != 1 or payload.get("harness") != harness:
        raise ValueError(f"pending activation has the wrong authority: {path}")
    if payload.get("namespace") != _expected_namespace():
        raise ValueError(f"pending activation has the wrong namespace: {path}")
    bearer = payload.get("bearer")
    if not isinstance(bearer, str) or not BEARER_RE.fullmatch(bearer):
        raise ValueError(f"pending activation has no valid credential: {path}")
    return bearer


def clear_pending_activation(*, harness: str, bearer: str) -> None:
    """Remove only the exact pending authority that was successfully committed."""
    path = pending_activation_path(harness)
    if load_pending_activation(harness=harness) != bearer:
        raise ValueError("pending activation changed during local commit")
    path.unlink()


def _ensure_stable_harness_device_id(
    *,
    harness: str,
    path_parts: tuple[str, ...],
    env_name: str,
) -> str:
    """Return one harness-qualified stable identity, persisted securely.

    The device file is a pre-resolution prerequisite rather than part of the
    bearer transaction: if the network exchange succeeds but a later local
    write is interrupted, a retry still converges on the same server-side
    device row instead of accumulating credentials.
    """
    path = Path.home().joinpath(*path_parts)
    _ensure_private_codex_dir(path.parent)
    _validate_regular_target(path)
    env_candidate = os.environ.get(env_name, "").strip()
    if env_candidate and not DEVICE_ID_RE.fullmatch(env_candidate):
        raise ValueError(f"{env_name} has an invalid value")
    if harness == "claude" and env_candidate and not env_candidate.startswith(
        "claude-"
    ):
        raise ValueError(f"{env_name} must carry the claude- harness qualifier")

    existing = ""
    if _lstat_kind(path) == "file":
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Codex device identity is unreadable: {path}") from exc
        prefix = f"{DEVICE_ID_ENV_VAR}="
        if len(lines) != 1 or not lines[0].startswith(prefix):
            raise ValueError(f"Codex device identity has an ambiguous format: {path}")
        existing = lines[0][len(prefix):]
        if not DEVICE_ID_RE.fullmatch(existing):
            raise ValueError(f"{harness.title()} device identity is invalid: {path}")
        if harness == "claude" and not existing.startswith("claude-"):
            raise ValueError("Claude device identity lacks its harness qualifier")

    if env_candidate and existing and env_candidate != existing:
        raise ValueError(
            f"environment and durable {harness.title()} device identities disagree"
        )
    selected = env_candidate or existing or f"{harness}-{secrets.token_hex(16)}"
    if not existing:
        _atomic_write_text(
            path,
            f"{DEVICE_ID_ENV_VAR}={selected}\n",
            mode=0o600,
        )
    return selected


def ensure_stable_codex_device_id() -> str:
    """Preserve the standalone Codex device file and legacy env override."""
    return _ensure_stable_harness_device_id(
        harness="codex",
        path_parts=CODEX_DEVICE,
        env_name=DEVICE_ID_ENV_VAR,
    )


def ensure_stable_claude_device_id() -> str:
    """Use a Claude-only authority identity, never Codex's device file."""
    return _ensure_stable_harness_device_id(
        harness="claude",
        path_parts=CLAUDE_DEVICE,
        env_name=CLAUDE_DEVICE_ID_ENV_VAR,
    )


def load_existing_codex_bearer() -> str:
    """Read one exact bearer assignment from the durable Codex credential.

    Legacy marketplace migration must not consume another one-time install
    code. The durable store is the authority, but only in its canonical
    single-assignment shape; extra or duplicate lines fail closed.
    """
    store = Path.home().joinpath(*CODEX_STORE)
    _validate_private_activation_dir(store.parent)
    _verify_owner_only_file(store)
    try:
        lines = store.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(
            f"Codex credential store is unavailable: {store}"
        ) from exc
    prefix = f"{ENV_VAR}="
    if len(lines) != 1 or not lines[0].startswith(prefix):
        raise ValueError(
            f"Codex credential store has an ambiguous format: {store}"
        )
    bearer = lines[0][len(prefix):]
    if not bearer or bearer != bearer.strip() or "\x00" in bearer:
        raise ValueError(
            f"Codex credential store has no valid credential: {store}"
        )
    return bearer


def write_codex_rc_block(
    *,
    include_codex_credential: bool = True,
    strict: bool = False,
) -> list[Path]:
    """Install the managed RC block that exports the bearer for Codex (POSIX).

    Codex's own MCP connection reads `bearer_token_env_var` from CODEX's
    process environment, so the variable must exist in the login shell that
    launches Codex. The block SOURCES the durable store rather than embedding
    the token — profiles never carry the secret, and rotation touches only
    credential.env. Markers make the write idempotent (replace-in-place).

    Targets `~/.profile` always (created if absent — the login-shell anchor)
    plus `~/.bashrc` / `~/.zshrc` when they already exist. Returns the paths
    written.
    """
    store = Path.home().joinpath(*CODEX_STORE)
    block_lines = [
        RC_BEGIN,
        f'export PATH="{Path.home().joinpath(*CODEX_BIN)}:$PATH"',
    ]
    if include_codex_credential:
        block_lines.extend(
            (
                f'if [ -f "{store}" ]; then',
                f'  . "{store}"',
                f'  {HARNESS_ENV_VARS["codex"]}="${{{ENV_VAR}}}"',
                f'  unset {ENV_VAR}',
                f'  export {HARNESS_ENV_VARS["codex"]}',
                "fi",
            )
        )
    block_lines.append(RC_END)
    targets = _codex_rc_targets()

    written: list[Path] = []
    for profile in targets:
        try:
            exists = profile.exists()
            existing = profile.read_bytes() if exists else b""
            validate_codex_rc_marker_bytes(existing)
            first_lf = existing.find(b"\n")
            line_end = (
                b"\r\n"
                if first_lf > 0 and existing[first_lf - 1 : first_lf + 1] == b"\r\n"
                else b"\n"
            )
            block = line_end.join(line.encode("utf-8") for line in block_lines)
            begin = RC_BEGIN.encode("ascii")
            end = RC_END.encode("ascii")
            if begin in existing and end in existing:
                begin_at = existing.find(begin)
                end_after = existing.find(end) + len(end)
                updated = existing[:begin_at] + block + existing[end_after:]
            else:
                sep = b"" if (not existing or existing.endswith((b"\n", b"\r"))) else line_end
                updated = existing + sep + block + line_end
            mode = stat.S_IMODE(profile.stat().st_mode) if exists else 0o600
            _atomic_write_bytes(profile, updated, mode=mode)
            written.append(profile)
        except OSError:
            if strict:
                raise
            # Best-effort per profile: a read-only .zshrc must not abort the
            # whole activation — the durable store is already written and the
            # caller prints the manual export as the fallback.
            continue
    return written


def _set_windows_user_env(
    name: str, value: str, value_type: int | None = None
) -> None:
    """Persist a User-scope env var on Windows WITHOUT exposing it on any process
    argv. Writes HKCU\\Environment via the registry API (never a `setx name value`
    command line, whose value is readable by any local process, Task Manager, or
    EDR process-creation log) and broadcasts WM_SETTINGCHANGE so already-running
    shells pick it up without a logoff. Raises on failure so the caller falls
    back to the manual hint. Windows-only (imports are lazy for POSIX safety)."""
    import ctypes
    import winreg

    value_type = winreg.REG_SZ if value_type is None else value_type

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, name, 0, value_type, value)
    # Best-effort broadcast so new/refreshed processes see it; failure here does
    # not undo the persisted write, so it is not fatal.
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()),
        )
    except Exception:
        # Best-effort: a failed WM_SETTINGCHANGE broadcast does not undo the
        # persisted registry write — new shells inherit it on next launch — so
        # swallowing it here is not fatal (H3 audit-gate intent marker).
        pass


def _get_windows_user_env(name: str) -> tuple[str, int | None]:
    """Read one user environment value while preserving its registry type."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return "", None
    return str(value), int(value_type)


def _delete_windows_user_env(name: str) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def merge_windows_path(current: str, wanted: Path) -> str:
    """Append one Windows PATH entry without reordering or duplicating it."""
    wanted_text = str(wanted)

    def normalized(value: str) -> str:
        expanded = ntpath.expandvars(value.strip().strip('"'))
        return ntpath.normcase(ntpath.normpath(expanded))

    wanted_key = normalized(wanted_text)
    if any(normalized(entry) == wanted_key for entry in current.split(";") if entry):
        return current
    if not current:
        return wanted_text
    separator = "" if current.endswith(";") else ";"
    return current + separator + wanted_text


def ensure_windows_codex_launcher_path(bin_dir: Path) -> str:
    """Persist the stable dispatcher directory on Windows User PATH."""
    current, value_type = _get_windows_user_env("Path")
    merged = merge_windows_path(current, bin_dir)
    if merged != current:
        _set_windows_user_env("Path", merged, value_type)
    os.environ["PATH"] = merge_windows_path(os.environ.get("PATH", ""), bin_dir)
    return merged


def set_user_env(bearer: str, *, harness: str = "claude") -> tuple[bool, str]:
    """Set one harness-scoped token as a persistent User-scope env var.

    Returns (persisted, human_message). On Windows writes HKCU\\Environment via
    the registry API (NOT `setx`, whose argv would expose the bearer to any
    local process / EDR process-creation log). On POSIX we cannot reliably set
    an env var that the Claude Code app will inherit, so we report the manual
    step instead of pretending it worked.
    """
    # Update this process's env either way, so a same-process check can see it.
    env_var = HARNESS_ENV_VARS[harness]
    os.environ[env_var] = bearer

    if platform.system() == "Windows":
        try:
            _set_windows_user_env(env_var, bearer)
            return True, (
                f"Set {env_var} as a persistent User environment variable."
            )
        except Exception:
            # Never interpolate the exception: it could render the value.
            # Report only the failure, with a placeholder for the manual hint.
            return False, (
                "Could not set the environment variable automatically.\n"
                "Set it manually, then restart Claude Code:\n"
                f'    setx {env_var} "<your token>"'
            )

    # POSIX: no universally-inherited persistent env. Guide the user.
    return False, (
        f"On macOS/Linux, add this to your shell profile (e.g. ~/.zshrc, ~/.bashrc),\n"
        f"then fully restart Claude Code:\n"
        f'    export {env_var}="<your token>"\n'
        f"(The token has also been saved to {wrapper_dir() / '.env'} for reference.)"
    )


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    existed: bool
    data: bytes = b""
    mode: int = 0o600
    windows_dacl_sddl: str | None = None


def _windows_dacl_sddl(path: Path) -> str:
    """Read one Windows DACL as SDDL without exposing file contents."""
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:ALLOSTAT_ACL_PATH
$sections = [System.Security.AccessControl.AccessControlSections]::Access
[Console]::Out.Write($acl.GetSecurityDescriptorSddlForm($sections))
"""
    child_env = os.environ.copy()
    child_env["ALLOSTAT_ACL_PATH"] = str(path)
    result = _run_windows_security_powershell(script, env=child_env)
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("Windows DACL snapshot returned no access descriptor")
    return value


def _restore_windows_dacl_sddl(path: Path, sddl: str) -> None:
    """Restore exactly the access-control section captured by `_snapshot_file`."""
    script = r"""
$ErrorActionPreference = 'Stop'
$sections = [System.Security.AccessControl.AccessControlSections]::Access
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetSecurityDescriptorSddlForm($env:ALLOSTAT_ACL_SDDL, $sections)
[System.IO.File]::SetAccessControl($env:ALLOSTAT_ACL_PATH, $acl)
"""
    child_env = os.environ.copy()
    child_env["ALLOSTAT_ACL_PATH"] = str(path)
    child_env["ALLOSTAT_ACL_SDDL"] = sddl
    _run_windows_security_powershell(script, env=child_env)


def _snapshot_file(path: Path) -> _FileSnapshot:
    kind = _lstat_kind(path)
    if kind == "missing":
        return _FileSnapshot(path=path, existed=False)
    if kind != "file":
        raise ValueError(f"activation target is not a regular file: {path}")
    info = path.stat()
    return _FileSnapshot(
        path=path,
        existed=True,
        data=path.read_bytes(),
        mode=stat.S_IMODE(info.st_mode),
        windows_dacl_sddl=(
            _windows_dacl_sddl(path) if os.name == "nt" else None
        ),
    )


class _ActivationTransaction:
    """Rollback envelope for every local activation mutation."""

    def __init__(self, paths: list[Path], *, windows_env: list[str]):
        unique = list(dict.fromkeys(Path(path) for path in paths))
        self._files = [_snapshot_file(path) for path in unique]
        missing_dirs: set[Path] = set()
        home = Path.home().absolute()
        for path in unique:
            current = path.parent.absolute()
            while current != current.parent and current.is_relative_to(home):
                kind = _lstat_kind(current)
                if kind == "missing":
                    missing_dirs.add(current)
                else:
                    break
                current = current.parent
        self._new_directories = sorted(
            missing_dirs,
            key=lambda item: len(item.parts),
            reverse=True,
        )
        self._process_env = {
            name: os.environ.get(name) for name in set(windows_env + ["PATH"])
        }
        self._registry_env: dict[str, tuple[str, int | None]] = {}
        if platform.system() == "Windows":
            self._registry_env = {
                name: _get_windows_user_env(name) for name in windows_env
            }
        self._committed = False

    def commit(self) -> None:
        self._committed = True

    def restore_file_permissions(self) -> None:
        """Preserve exact permissions on pre-existing transactional files."""
        for snapshot in self._files:
            if not snapshot.existed:
                continue
            if snapshot.windows_dacl_sddl is not None:
                _restore_windows_dacl_sddl(
                    snapshot.path, snapshot.windows_dacl_sddl
                )
            elif os.name != "nt":
                os.chmod(snapshot.path, snapshot.mode)

    def rollback(self) -> None:
        for snapshot in reversed(self._files):
            if snapshot.existed:
                if _lstat_kind(snapshot.path) == "file":
                    current_data = snapshot.path.read_bytes()
                    if os.name == "nt":
                        unchanged_permissions = (
                            snapshot.windows_dacl_sddl is not None
                            and _windows_dacl_sddl(snapshot.path)
                            == snapshot.windows_dacl_sddl
                        )
                    else:
                        unchanged_permissions = (
                            stat.S_IMODE(snapshot.path.stat().st_mode)
                            == snapshot.mode
                        )
                    if current_data == snapshot.data and unchanged_permissions:
                        # The transaction never touched this target. Replacing
                        # it would needlessly change its inode/Windows ACL.
                        continue
                _atomic_write_bytes(
                    snapshot.path,
                    snapshot.data,
                    mode=snapshot.mode,
                )
                if snapshot.windows_dacl_sddl is not None:
                    _restore_windows_dacl_sddl(
                        snapshot.path, snapshot.windows_dacl_sddl
                    )
            else:
                kind = _lstat_kind(snapshot.path)
                if kind == "file":
                    snapshot.path.unlink()
                elif kind != "missing":
                    raise RuntimeError(
                        f"rollback target changed type unexpectedly: {snapshot.path}"
                    )
        if platform.system() == "Windows":
            for name, (value, value_type) in self._registry_env.items():
                if value_type is None:
                    _delete_windows_user_env(name)
                else:
                    _set_windows_user_env(name, value, value_type)
        for name, value in self._process_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for directory in self._new_directories:
            if _lstat_kind(directory) == "directory":
                try:
                    directory.rmdir()
                except OSError:
                    # A non-empty directory contains state outside this
                    # transaction; never delete it recursively.
                    pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del traceback
        if exc_type is not None and not self._committed:
            self.rollback()
        return False


def _activation_owned_paths(
    *,
    harness: str,
    python_token: str,
) -> list[Path]:
    system = platform.system()
    command = _command_dispatcher_plan(
        python_token=python_token,
        system=system,
    )
    paths = [Path(command["program"]), Path(command["launcher"])]
    if harness == "codex":
        dispatcher = _codex_dispatcher_plan(
            python_token=python_token,
            system=system,
        )
        paths.extend(
            (
                Path.home().joinpath(*CODEX_STORE),
                Path(dispatcher["program"]),
                Path(dispatcher["launcher"]),
            )
        )
    else:
        paths.append(wrapper_dir() / ".env")
    if system != "Windows":
        paths.extend(_codex_rc_targets())
    return list(dict.fromkeys(paths))


def commit_local_activation(
    *,
    bearer: str,
    harness: str,
    python_token: str,
    persist_credential: bool,
) -> tuple[bool, str]:
    """Apply local activation as one rollback-capable transaction."""
    paths = _activation_owned_paths(harness=harness, python_token=python_token)
    env_names = [HARNESS_ENV_VARS[harness]]
    if platform.system() == "Windows":
        env_names.append("Path")

    with _ActivationTransaction(paths, windows_env=env_names) as transaction:
        if harness == "codex":
            if persist_credential:
                persist_codex_store(bearer)
        else:
            persist_env_file(bearer)

        command_dispatcher = materialize_command_dispatcher(
            python_token=python_token
        )
        launcher_bin = command_dispatcher["bin_dir"]
        if harness == "codex":
            dispatcher = materialize_codex_dispatcher(python_token=python_token)
            launcher_bin = dispatcher["bin_dir"]

        if platform.system() == "Windows":
            ensure_windows_codex_launcher_path(launcher_bin)

        persisted, message = set_user_env(bearer, harness=harness)
        if platform.system() == "Windows" and not persisted:
            raise RuntimeError("could not persist the harness credential")
        if harness == "codex" and platform.system() != "Windows" and not persisted:
            written = write_codex_rc_block(strict=True)
            if not written:
                raise RuntimeError("could not persist the Codex shell profile block")
            persisted = True
            message = (
                "Wrote the token store and a shell-profile block ("
                + ", ".join(path.name for path in written)
                + ") so Codex sessions inherit it."
            )
        elif harness == "claude" and platform.system() != "Windows":
            write_codex_rc_block(
                include_codex_credential=False,
                strict=True,
            )

        transaction.commit()
        return persisted, message


def _main_unlocked(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Activate an Allostat install code.")
    parser.add_argument(
        "code",
        nargs="?",
        help="Your install code (from your Allostat email).",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help=(
            "Codex legacy migration only: reuse the durable credential without "
            "resolving or consuming a new install code."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume a securely preserved post-resolve credential without "
            "consuming another install code. Available for Claude and Codex."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without hitting the network or writing anything.",
    )
    parser.add_argument(
        "--harness",
        choices=("claude", "codex"),
        default="claude",
        help=(
            "Which harness this activation serves. 'codex' additionally writes "
            "the durable store (~/.allostat/codex/credential.env) and, on "
            "macOS/Linux, a managed shell-profile block so Codex's process "
            "environment carries the token."
        ),
    )
    args = parser.parse_args(argv)

    code = (args.code or "").strip()
    if args.reuse_existing and args.harness != "codex":
        parser.error("--reuse-existing is available only with --harness codex")
    selected_modes = int(bool(code)) + int(args.reuse_existing) + int(args.resume)
    if selected_modes > 1:
        parser.error(
            "provide exactly one of an install code, --reuse-existing, or --resume"
        )
    if selected_modes == 0:
        print("ERROR: no install code provided.", file=sys.stderr)
        return 2

    if args.dry_run:
        print("DRY RUN -- no network call, no changes made.")
        if args.reuse_existing:
            print(f"  Would reuse store at:   {Path.home().joinpath(*CODEX_STORE)}")
        elif args.resume:
            print(
                "  Would resume pending:    "
                f"{pending_activation_path(args.harness)}"
            )
        else:
            print(f"  Would POST the code to: {_resolve_url()}")
        print(f"  Would set env var:      {HARNESS_ENV_VARS[args.harness]} (User scope)")
        if args.harness == "claude":
            print(f"  Would write .env at:    {wrapper_dir() / '.env'}")
            if not args.reuse_existing:
                print(
                    "  Would establish device: "
                    f"{Path.home().joinpath(*CLAUDE_DEVICE)}"
                )
        if args.harness == "codex":
            print(f"  Would write store at:   {Path.home().joinpath(*CODEX_STORE)}")
            if not args.reuse_existing:
                print(f"  Would establish device: {Path.home().joinpath(*CODEX_DEVICE)}")
            print(f"  Would install command:  {Path.home().joinpath(*CODEX_BIN)}")
            if platform.system() != "Windows":
                print("  Would add RC block to:  ~/.profile (+ existing .bashrc/.zshrc)")
        print(f"  Platform:               {platform.system()}")
        return 0

    python_token = select_activation_python_token()
    if args.harness == "codex":
        preflight_codex_activation(
            python_token=python_token,
            reuse_existing=args.reuse_existing,
        )
    else:
        _command_dispatcher_plan(python_token=python_token)
        _validate_regular_target(wrapper_dir() / ".env")
    preflight_pending_activation(
        harness=args.harness,
        resume=args.resume,
    )

    # Network identity and reachability are proven before a one-time code can
    # be consumed. The durable random device identity is also established first
    # so an interrupted retry converges on the same per-device authority.
    preflight_activation_endpoint()

    if args.reuse_existing:
        bearer = load_existing_codex_bearer()
        probe_credential(bearer, require_device=False)
        user_email = "existing Codex credential"
        persist_credential = False
    elif args.resume:
        bearer = load_pending_activation(harness=args.harness)
        probe_credential(bearer, require_device=True)
        user_email = "pending activation credential"
        persist_credential = True
    else:
        if args.harness == "codex":
            device_id = ensure_stable_codex_device_id()
        else:
            device_id = ensure_stable_claude_device_id()
        print(f"Resolving install code at {_endpoint()} ...")
        resolved = resolve_code(
            code,
            device_id=device_id,
            hostname=platform.node() or None,
        )
        bearer = resolved.get("bearer")
        if not bearer:
            raise SystemExit("ERROR: server response did not include a bearer token.")
        if resolved.get("namespace") != _expected_namespace():
            raise ValueError("resolved credential belongs to an unexpected namespace")
        if resolved.get("credential_scope") != "device":
            raise ValueError("server did not issue a device-scoped credential")
        persist_pending_activation(
            bearer=bearer,
            harness=args.harness,
            namespace=str(resolved["namespace"]),
        )
        try:
            probe_credential(bearer, require_device=True)
        except ValueError as exc:
            raise ValueError(
                f"{exc}; the issued credential was saved securely. "
                f"Retry with --harness {args.harness} --resume; do not use "
                "another install code"
            ) from exc
        user_email = resolved.get("user_email", "(unknown)")
        persist_credential = True

    try:
        persisted, message = commit_local_activation(
            bearer=bearer,
            harness=args.harness,
            python_token=python_token,
            persist_credential=persist_credential,
        )
    except Exception:
        if not args.reuse_existing:
            print(
                "The issued credential remains in owner-only pending storage. "
                f"Retry with --harness {args.harness} --resume; do not use "
                "another install code.",
                file=sys.stderr,
            )
        raise
    if not args.reuse_existing:
        clear_pending_activation(harness=args.harness, bearer=bearer)

    if args.reuse_existing:
        action = "Existing credential loaded"
    elif args.resume:
        action = "Pending activation resumed"
    else:
        action = "Code resolved"
    print(f"OK: {action} (user: {user_email}).")
    print(f"  {message}")
    print("")
    harness_app = "Codex" if args.harness == "codex" else "Claude Code"
    if persisted:
        print(f"ACTIVATED. Now fully quit and reopen {harness_app} so the plugin picks up")
        print("your token — the MCP connection reads the environment at launch.")
    else:
        print(f"Once the variable is set, fully quit and reopen {harness_app}.")
    if args.harness == "codex":
        print("Then run /hooks inside Codex and trust SessionStart, UserPromptSubmit,")
        print("PreToolUse, and Stop — untrusted events remain offline.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse no-write modes, then serialize every real harness activation."""
    raw = list(sys.argv[1:] if argv is None else argv)
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("code", nargs="?")
    pre_parser.add_argument("--reuse-existing", action="store_true")
    pre_parser.add_argument("--resume", action="store_true")
    pre_parser.add_argument("--dry-run", action="store_true")
    pre_parser.add_argument(
        "--harness",
        choices=("claude", "codex"),
        default="claude",
    )
    preview, unknown = pre_parser.parse_known_args(raw)
    selected_modes = (
        int(bool((preview.code or "").strip()))
        + int(preview.reuse_existing)
        + int(preview.resume)
    )
    if preview.dry_run or unknown or selected_modes != 1:
        return _main_unlocked(raw)
    with activation_exclusion(preview.harness):
        return _main_unlocked(raw)


if __name__ == "__main__":
    raise SystemExit(main())
