#!/usr/bin/env python3
"""Create and verify a private CPython 3.12 Windows x64 runtime offline.

Only a new explicitly supplied destination is written.  The caller provides a
trusted base interpreter and the wheel folder matching the reviewed lock.  No
user profile, global packages, account state, or plugin registration is changed.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


LOCK_PATH = (
    Path(__file__).resolve().parent / "requirements-win312.lock"
    if (Path(__file__).resolve().parent / "requirements-win312.lock").is_file()
    else Path(__file__).resolve().parents[1] / "install/local/requirements-win312.lock"
)
RECEIPT = "runtime-receipt.json"
LOCK_LINE = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)==([A-Za-z0-9.]+) --hash=sha256:([0-9a-f]{64})")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_lock(path: Path = LOCK_PATH) -> dict[str, tuple[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Runtime dependency lock is missing or linked")
    locked = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        if match is None or match[1] in locked:
            raise ValueError("Runtime dependency lock is malformed or duplicated")
        locked[match[1]] = (match[2], match[3])
    if not locked:
        raise ValueError("Runtime dependency lock is empty")
    return locked


def validate_wheelhouse(
    wheelhouse: Path, locked: dict[str, tuple[str, str]]
) -> dict[str, str]:
    """Prove the exact wheel bytes before pip or venv is invoked."""
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("Wheel folder is missing or linked")
    seen = {}
    inventory = {}
    for path in sorted(wheelhouse.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise ValueError("Wheel folder contains an unsupported entry")
        digest = _sha256(path)
        if digest not in {value[1] for value in locked.values()}:
            raise ValueError("Wheel bytes do not match the dependency lock")
        with zipfile.ZipFile(path) as archive:
            entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError("Wheel has ambiguous package metadata")
            metadata = BytesParser().parsebytes(archive.read(entries[0]))
        name = re.sub(r"[-_.]+", "-", str(metadata["Name"]).lower())
        version = str(metadata["Version"])
        if name in seen or locked.get(name) != (version, digest):
            raise ValueError("Wheel identity does not match the dependency lock")
        seen[name] = version
        inventory[path.name] = digest
    if set(seen) != set(locked):
        raise ValueError("Wheel folder is missing a locked dependency")
    return inventory


def _child_env() -> dict[str, str]:
    # pip runs isolated and offline; preserve only OS process prerequisites.
    names = {"systemroot", "windir", "comspec", "temp", "tmp", "path", "pathext"}
    return {key: value for key, value in os.environ.items() if key.lower() in names}


def _run(argv: list[str], *, timeout: int = 180) -> str:
    result = subprocess.run(argv, env=_child_env(), capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        # No raw command, installation path, pip config, or inherited value is
        # copied into the report. The step is identified by its caller.
        raise RuntimeError("Isolated runtime subprocess failed")
    return result.stdout


def interpreter_identity(python: Path) -> dict:
    if not python.is_absolute() or python.is_symlink() or not python.is_file():
        raise ValueError("Supply an absolute trusted interpreter file")
    digest = _sha256(python)
    probe = json.loads(_run([str(python), "-I", "-B", "-c",
        "import json,sys,struct; print(json.dumps({'implementation':sys.implementation.name,'version':list(sys.version_info[:3]),'platform':sys.platform,'bits':struct.calcsize('P')*8}))"
    ]))
    if (
        probe.get("implementation") != "cpython"
        or probe.get("version", [])[:2] != [3, 12]
        or probe.get("platform") != "win32"
        or probe.get("bits") != 64
    ):
        raise ValueError("This runtime lock supports CPython 3.12 Windows x64 only")
    if _sha256(python) != digest:
        raise ValueError("Interpreter changed during validation")
    return {**probe, "sha256": digest}


def _inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Runtime contains a linked entry")
        if path.is_file() and path != root / RECEIPT:
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def build_runtime(destination: Path, *, python: Path, wheelhouse: Path) -> Path:
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Runtime destination must not exist")
    # Versioned program directory only. Callers keep mutable memory elsewhere.
    locked = read_lock()
    wheels = validate_wheelhouse(Path(wheelhouse), locked)
    identity = interpreter_identity(Path(python))
    _run([str(python), "-I", "-B", "-m", "venv", "--copies", str(destination)])
    executable = destination / "Scripts/python.exe"
    # pip options precede the verb, so ambient index/config cannot change this
    # operation; --no-deps makes the reviewed complete set the sole inventory.
    _run([
        str(executable), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
        "install", "--no-index", "--no-cache-dir", "--no-compile", "--only-binary=:all:",
        "--no-deps", "--require-hashes", "--find-links", str(Path(wheelhouse).resolve()),
        "-r", str(LOCK_PATH),
    ])
    _run([str(executable), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"])
    versions = json.loads(_run([
        str(executable), "-I", "-B", "-c",
        "import json,importlib.metadata as m; import mcp,pydantic,yaml,anyio; print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}))",
    ]))
    if any(versions.get(name) != version for name, (version, _) in locked.items()):
        raise ValueError("Installed dependencies differ from the lock")
    if _sha256(Path(python)) != identity["sha256"]:
        raise ValueError("Base interpreter changed during bootstrap")
    payload = {
        "schema": "allostat-local-runtime-v1", "platform": "win-amd64-cpython312",
        "base_interpreter": identity, "interpreter": "Scripts/python.exe",
        "lock_sha256": _sha256(LOCK_PATH), "wheels": wheels,
        "dependencies": {name: version for name, (version, _) in locked.items()},
        "sha256": _inventory(destination),
    }
    with (destination / RECEIPT).open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def verify_runtime(destination: Path, *, python: Path, expected_receipt_sha256: str) -> bool:
    """Verify private runtime bytes plus the supplied base interpreter/lock."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("Runtime root is linked")
    receipt = destination / RECEIPT
    if receipt.is_symlink():
        raise ValueError("Runtime receipt is linked")
    if (
        not isinstance(expected_receipt_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256) is None
        or _sha256(receipt) != expected_receipt_sha256
    ):
        raise ValueError("Runtime receipt differs from the caller-held identity")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "allostat-local-runtime-v1"
        or payload.get("platform") != "win-amd64-cpython312"
        or payload.get("interpreter") != "Scripts/python.exe"
        or payload.get("lock_sha256") != _sha256(LOCK_PATH)
        or payload.get("base_interpreter") != interpreter_identity(Path(python))
        or payload.get("dependencies") != {name: version for name, (version, _) in read_lock().items()}
        or payload.get("sha256") != _inventory(destination)
    ):
        raise ValueError("Runtime integrity verification failed")
    return True


def materialize_command_launcher(
    runtime: Path, *, python: Path, dispatcher: Path, destination: Path,
    expected_receipt_sha256: str, expected_dispatcher_sha256: str,
) -> Path:
    """Create new disposable launcher files using the verified runtime.

    cmd/PowerShell select the .cmd file; Claude's Git Bash selects the adjacent
    extensionless sh launcher. Neither command searches PATH for Python.
    """
    verify_runtime(runtime, python=python, expected_receipt_sha256=expected_receipt_sha256)
    runtime = Path(runtime).resolve()
    dispatcher = Path(dispatcher)
    if dispatcher.is_symlink() or not dispatcher.is_file():
        raise ValueError("Command dispatcher is missing or linked")
    if (
        not isinstance(expected_dispatcher_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_dispatcher_sha256) is None
        or _sha256(dispatcher) != expected_dispatcher_sha256
    ):
        raise ValueError("Command dispatcher differs from the reviewed identity")
    source = dispatcher.read_text(encoding="utf-8")
    if not source.startswith("# allostat-dispatcher-ownership: owner=allostat kind=marketplace-command-dispatcher format=1\n"):
        raise ValueError("Unexpected command dispatcher identity")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Launcher destination must not exist")
    executable = (runtime / "Scripts/python.exe").as_posix()
    if any(char in executable for char in ('%', '"', "\n", "\r", "'")):
        raise ValueError("Interpreter path is not representable by both launchers")
    destination.mkdir(parents=True)
    (destination / "allostat-command.py").write_text(source, encoding="utf-8")
    (destination / "allostat-command.cmd").write_text(
        '@REM allostat-local-launcher-v1\r\n@echo off\r\n'
        f'"{executable}" -B -P "%~dp0allostat-command.py" %*\r\n',
        encoding="utf-8", newline="",
    )
    shell = destination / "allostat-command"
    shell.write_text(
        "#!/bin/sh\n# allostat-local-launcher-v1\n"
        'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 3\n'
        f"exec '{executable}' -B -P \"$SCRIPT_DIR/allostat-command.py\" \"$@\"\n",
        encoding="utf-8", newline="\n",
    )
    shell.chmod(0o755)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--expected-receipt-sha256", help="receipt digest retained outside the runtime after bootstrap")
    args = parser.parse_args(argv)
    if args.verify:
        if args.expected_receipt_sha256 is None:
            parser.error("--expected-receipt-sha256 is required when verifying")
        verify_runtime(args.out, python=args.python, expected_receipt_sha256=args.expected_receipt_sha256)
        print("Runtime integrity verified")
    else:
        if args.wheelhouse is None:
            parser.error("--wheelhouse is required when building")
        build_runtime(args.out, python=args.python, wheelhouse=args.wheelhouse)
        print("Private runtime ready")
        print("receipt-sha256: " + _sha256(args.out / RECEIPT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
