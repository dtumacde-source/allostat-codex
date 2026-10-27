"""Bundled local-plugin setup and launch. No accounts, network or PATH edits.

The native host's installed package is the code trust boundary. The embedded
manifest digest detects changed package data; this is not a sandbox against a
local user who can replace the entry program itself or its runtime authority.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

EXPECTED_MANIFEST_SHA256 = "8a513fa879fb14e26daffca7535cfc796d0cd9d07f23055c54dbd75157dfcc0d"
DIGEST = re.compile(r'[0-9a-f]{64}')
ATTEMPT = re.compile(r'[0-9a-f]{32}')


class SetupRequired(ValueError):
    pass


class UnsupportedRuntimePath(ValueError):
    pass


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def no_links(path):
    for part in (Path(path), *Path(path).parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked installation or runtime path refused')


def read_json(path):
    no_links(path)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(path.read_text('utf-8'), object_pairs_hook=unique)


def load(path):
    # Stdlib-only loader; no ambient module search or bytecode cache writes.
    import types
    module = types.ModuleType('allostat_bundled_' + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def package():
    root = Path(__file__).absolute().parent.parent
    no_links(root)
    manifest_path = root / 'bootstrap/manifest.json'
    no_links(manifest_path)
    if sha(manifest_path) != EXPECTED_MANIFEST_SHA256:
        raise ValueError('Bundled manifest integrity failed')
    manifest = read_json(manifest_path)
    if (set(manifest) != {'schema', 'harness', 'sha256', 'optional_native', 'entry_template_sha256'}
            or manifest['schema'] != 'allostat-local-catalog-v1'
            or manifest['harness'] not in {'codex', 'claude'}):
        raise ValueError('Bundled manifest contract failed')
    entry = root / 'bootstrap/local_entry.py'
    no_links(entry)
    template = entry.read_text('utf-8').replace(EXPECTED_MANIFEST_SHA256, 'BUILD_TIME_' + 'DIGEST')
    if hashlib.sha256(template.encode('utf-8')).hexdigest() != manifest['entry_template_sha256']:
        raise ValueError('Bundled entry integrity failed')
    expected = manifest['sha256']
    optional = manifest['optional_native']
    if not isinstance(expected, dict) or not isinstance(optional, dict):
        raise ValueError('Bundled inventory malformed')
    actual = {}
    for path in root.rglob('*'):
        no_links(path)
        if path.is_file():
            name = path.relative_to(root).as_posix()
            if name in {'bootstrap/manifest.json', 'bootstrap/local_entry.py'}:
                continue
            actual[name] = sha(path)
    if (any(actual.get(name) != digest for name, digest in expected.items())
            or any(name not in expected and optional.get(name) != digest
                   for name, digest in actual.items())):
        raise ValueError('Bundled plugin integrity failed')
    if read_json(root / 'runtime.json') != {'runtime': 'local'}:
        raise ValueError('Only a local runtime is supported')
    return root, manifest


def state_root(value=None):
    path = Path(value) if value is not None else Path.home() / '.allostat/plugin-runtimes/v1'
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Runtime state path must be absolute')
    no_links(path)
    return path


@contextmanager
def locked(state):
    no_links(state)
    state.mkdir(parents=True, exist_ok=True)
    lock = state / 'setup.lock'
    no_links(lock)
    with lock.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path, payload):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.next')
    no_links(path)
    no_links(temporary)
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verified_runtime(root, state):
    authority = state / (EXPECTED_MANIFEST_SHA256 + '.json')
    if not authority.exists():
        raise SetupRequired('Local setup required')
    data = read_json(authority)
    if (set(data) != {'schema', 'package', 'attempt', 'base_python', 'receipt'}
            or data['schema'] != 'allostat-catalog-runtime-v1'
            or data['package'] != EXPECTED_MANIFEST_SHA256
            or not isinstance(data['attempt'], str)
            or not ATTEMPT.fullmatch(data['attempt'])
            or not isinstance(data['receipt'], str) or not DIGEST.fullmatch(data['receipt'])
            or not isinstance(data['base_python'], str)
            or Path(data['base_python']) != Path(sys.executable).absolute()):
        raise ValueError('Runtime authority malformed')
    runtime = state / 'attempts' / data['attempt']
    no_links(runtime)
    for child in runtime.rglob('*'):
        no_links(child)
    builder = load(root / 'bootstrap/build_local_runtime.py')
    builder.verify_runtime(runtime, python=Path(data['base_python']),
                           expected_receipt_sha256=data['receipt'])
    return runtime / 'Scripts/python.exe'


def setup(root, state, *, python, repair=False):
    # Build in final unique location: venvs must never be moved after creation.
    # A killed setup leaves an unreferenced attempt, not runnable authority.
    # CreateProcess cannot reliably launch an overlong venv executable even
    # where Python's own file APIs accept the path. Keep attempt names short;
    # the full content binding lives in the authority filename and record.
    longest_executable = state / 'attempts' / ('0' * 32) / 'Scripts/python.exe'
    if os.name == 'nt' and len(str(longest_executable).encode('utf-16-le')) // 2 >= 250:
        raise UnsupportedRuntimePath()
    no_links(python)
    builder = load(root / 'bootstrap/build_local_runtime.py')
    builder.interpreter_identity(python)  # Prerequisite failure creates no state.
    with locked(state):
        authority = state / (EXPECTED_MANIFEST_SHA256 + '.json')
        no_links(authority)
        if authority.exists() and not repair:
            return verified_runtime(root, state)
        attempt = uuid.uuid4().hex
        destination = state / 'attempts' / attempt
        no_links(destination)
        destination.parent.mkdir(exist_ok=True)
        builder.build_runtime(destination, python=python, wheelhouse=root / 'bootstrap/wheels')
        receipt = sha(destination / builder.RECEIPT)
        builder.verify_runtime(destination, python=python, expected_receipt_sha256=receipt)
        # Reconcile cached plugin bytes again before publishing any authority.
        package()
        atomic_json(authority, {'schema': 'allostat-catalog-runtime-v1',
                               'package': EXPECTED_MANIFEST_SHA256, 'attempt': attempt,
                               'base_python': str(python), 'receipt': receipt})
        return verified_runtime(root, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--state-dir', type=Path, help='explicit isolated test/runtime storage')
    parser.add_argument('action', choices=('setup', 'repair', 'status', 'run'))
    parser.add_argument('operation', nargs='?')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        root, manifest = package()
        state = state_root(args.state_dir)
        if state == root or state.is_relative_to(root) or root.is_relative_to(state):
            raise ValueError('Runtime state must be separate from plugin files')
        if args.action in {'setup', 'repair'}:
            if args.operation is not None:
                raise ValueError('Setup accepts no operation')
            setup(root, state, python=Path(sys.executable).absolute(), repair=args.action == 'repair')
            print('Local runtime ready. Restart the host to reconnect MCP and hooks.')
            return 0
        executable = verified_runtime(root, state)
        if args.action == 'status':
            print('Local runtime integrity verified; hook delivery and memory need separate checks.')
            return 0
        dispatcher = load(root / 'lib/marketplace_command_dispatcher.py')
        script, prefix = dispatcher.resolve_operation(root, args.operation)
        harness_args = ['--harness', manifest['harness']] if args.operation in dispatcher.HARNESS_AWARE_OPERATIONS else []
        return subprocess.call([str(executable), '-I', '-B', str(script), *prefix,
                                *harness_args, *args.arguments])
    except UnsupportedRuntimePath:
        print('Allostat runtime location is too long for Windows process startup. Use a shorter supported profile/runtime location; setup created no state and changed no prior selection.', file=sys.stderr)
        return 3
    except SetupRequired:
        print('Allostat local setup required. Run the bundled local-setup skill once; no runtime operation was started.', file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        # No raw exception, command, private path or inherited env in diagnostics.
        print('Allostat local setup/integrity check failed. Check Windows x64 CPython 3.12 and the installed package; retry only after resolving corruption or concurrent setup. No cloud fallback.', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
