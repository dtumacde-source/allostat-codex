"""Explicit owner preferences, not authentication or permission to run a tool.

No project, chat, or custom environment overrides. Same-user programs can edit
preferences: this is cooperative product configuration, not a security boundary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

SCHEMA = 'allostat-owner-guards/v1'
RULE_IDS = tuple(f'innate-{i:02}' for i in range(1, 13) if i != 9)
LIMIT = 16384
_snapshot = ContextVar('allostat_owner_guard_snapshot', default=None)
EXCLUDED = ('Codex/Claude permissions', 'package integrity and memory readiness',
            'typed memory consistency', 'native patch envelope validity',
            'sandbox roles', 'memory size and shape limits', 'credential storage')


def settings_path():
    return Path.home() / '.allostat' / 'guard-settings-v1.json'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    health: str
    generation: str
    rules: tuple[tuple[str, bool], ...]
    error: str | None = None

    def enabled(self, rule_id):
        return dict(self.rules).get(rule_id, rule_id != 'innate-09')

    @property
    def identity(self):
        return _digest([SCHEMA, self.health, self.generation, self.rules])


def _strict(health, error=None):
    return Policy(health, 'builtin', tuple((r, True) for r in RULE_IDS), error)


def _no_links(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('linked_settings')
        if part == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError('invalid_settings_file')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def _reset_defaults():
    """Explicit reset only: absence of owner settings is not proof of a new user.

    The installed inventory pins this own-package marker. Old/private bundles
    without it retain all-enabled defaults. Never consult cwd or ambient roots.
    """
    path = Path(__file__).resolve().parent.parent / 'data' / 'guard-defaults.json'
    _no_links(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
    except FileNotFoundError:
        return _strict('defaults')
    if len(raw) > LIMIT:
        raise ValueError('invalid_release_defaults')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_release_defaults') from None
    expected = {r: r == 'innate-02' for r in RULE_IDS}
    if (type(data) is not dict or set(data) != {'schema', 'rules'}
            or data['schema'] != 'allostat-release-guard-defaults/v1'
            or type(data['rules']) is not dict or data['rules'] != expected
            or any(type(v) is not bool for v in data['rules'].values())):
        raise ValueError('invalid_release_defaults')
    return Policy('defaults', _digest(data), tuple((r, expected[r]) for r in RULE_IDS))


def load():
    try:
        path = settings_path()
        _no_links(path)
        try:
            with path.open('rb') as stream:
                raw = stream.read(LIMIT + 1)
        except FileNotFoundError:
            return _strict('defaults')
        if len(raw) > LIMIT:
            raise ValueError('settings_too_large')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
        if (type(data) is not dict or set(data) != {'schema', 'generation', 'rules'}
                or data['schema'] != SCHEMA or type(data['generation']) is not str
                or not re.fullmatch('[0-9a-f]{32}', data['generation'])
                or type(data['rules']) is not dict or set(data['rules']) != set(RULE_IDS)
                or any(type(v) is not bool for v in data['rules'].values())):
            raise ValueError('invalid_schema')
        return Policy('valid', data['generation'], tuple((r, data['rules'][r]) for r in RULE_IDS))
    except (OSError, ValueError, UnicodeError, RecursionError):
        # Never include path, raw JSON, or exception text in diagnostics.
        return _strict('invalid', 'settings_unusable_using_shipped_defaults')


def current():
    return _snapshot.get() or load()


def enabled(rule_id):
    return current().enabled(rule_id)


def unchanged():
    return current().identity == load().identity


@contextmanager
def policy_scope():
    existing = _snapshot.get()
    token = None if existing is not None else _snapshot.set(load())
    try:
        yield current()
    finally:
        if token is not None:
            _snapshot.reset(token)


@contextmanager
def detection_scope():
    """Risk/privacy classification must not depend on enforcement preferences."""
    token = _snapshot.set(_strict('defaults'))
    try:
        yield
    finally:
        _snapshot.reset(token)


@contextmanager
def _writer_lock(path):
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    _no_links(lock)
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + 3
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('settings_busy') from None
                time.sleep(.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def update(rule_id=None, value=None, *, reset=False):
    if type(reset) is not bool or (not reset and (rule_id not in RULE_IDS or type(value) is not bool)):
        raise ValueError('unknown_rule_or_mode')
    if reset and (rule_id is not None or value is not None):
        raise ValueError('ambiguous_reset')
    path = settings_path()
    with _writer_lock(path):
        old = load()
        if old.health == 'invalid' and not reset:
            raise ValueError('invalid_settings_use_explicit_reset')
        rules = dict(_reset_defaults().rules if reset else old.rules)
        if not reset:
            rules[rule_id] = value
        data = dict(schema=SCHEMA, generation=uuid.uuid4().hex, rules=rules)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            _no_links(path)
            os.replace(temporary, path)
            if os.name != 'nt':
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
        finally:
            try: temporary.unlink()
            except FileNotFoundError: pass
        result = load()
        if result.health != 'valid' or result.generation != data['generation']:
            raise ValueError('settings_readback_failed')
        return result


def status():
    policy = load()
    health, severities, names = 'unavailable', {}, {}
    try:
        # The stable launcher uses -P: never remove it or search cwd to import
        # the sibling. Resolve only this snapshot's exact packaged module.
        sibling = Path(__file__).resolve().with_name('innate_rules.py')
        _no_links(sibling)
        name = '_allostat_status_rules_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, sibling)
        innate_rules = importlib.util.module_from_spec(spec)
        sys.modules[name] = innate_rules  # dataclass module identity during load
        try:
            spec.loader.exec_module(innate_rules)
        finally:
            sys.modules.pop(name, None)
        dropped = innate_rules.dropped_rules()
        health = dict(whole=not dropped, dropped_ids=[r.get('id', 'unknown') for r in dropped])
        severities = {r['id']: r.get('severity', 'unknown') for r in innate_rules._get_rules()}
        names = {r['id']: r.get('name', r['id']) for r in innate_rules._get_rules()}
    except Exception:
        pass
    return dict(schema=SCHEMA, settings_health=policy.health, settings_error=policy.error,
                source='owner_home' if policy.health != 'defaults' else 'shipped_defaults',
                policy_identity=policy.identity, native_pickup='not_verified', rule_health=health,
                rules={r: dict(requested=None if policy.health == 'invalid' else policy.enabled(r),
                               effective=policy.enabled(r), name=names.get(r, r),
                               severity=severities.get(r, 'unknown')) for r in RULE_IDS},
                retired=['innate-09'], excluded=list(EXCLUDED))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Allostat owner guard preferences. Does not authorize tools or alter host permissions.')
    sub = parser.add_subparsers(dest='operation', required=True)
    sub.add_parser('status')
    command = sub.add_parser('set')
    command.add_argument('rule', choices=RULE_IDS)
    command.add_argument('mode', choices=('enabled', 'disabled'))
    sub.add_parser('reset')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'set': update(args.rule, args.mode == 'enabled')
        if args.operation == 'reset': update(reset=True)
        print(json.dumps(status(), sort_keys=True))
        return 0
    except (OSError, ValueError):
        print('Allostat guard settings were not confirmed; run guards status. No action was authorized.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
