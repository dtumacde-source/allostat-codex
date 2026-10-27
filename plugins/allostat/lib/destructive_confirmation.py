"""Single-use cooperative accident prevention, NOT same-user authentication.

Only the UserPromptSubmit adapter should arm a pending action. A program running
as the user can alter this state (or the plugin); no tamper-proof claim is made.
Legacy consent stores are never read. Commands and paths are hashed, not saved.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
from functools import wraps

PHRASE = 'allostat:override'
SCHEMA = 'allostat-cooperative-destructive-v2'
TTL_SECONDS = 300
FIELDS = {'schema', 'scope', 'action', 'state', 'created', 'expires', 'approval_turn'}
HASH = re.compile(r'[0-9a-f]{64}')


def presentation(action):
    """Complete closed-form previews, never truncated/redacted into ambiguity.

    Only simple literal relative targets are supported. Private, composed or
    indirect requests require a future human-visible host presentation bridge.
    Correlation IDs alone are not that bridge. Nothing here is persisted.
    """
    if action.get('tool') not in {'Bash', 'PowerShell'}:
        return None
    context = action.get('cwd')
    if not isinstance(context, str) or not Path(context).is_absolute():
        return None
    inputs = action.get('input', {})
    if not isinstance(inputs, dict) or set(inputs) - {
        'command', 'timeout_ms', 'timeout', 'yield_time_ms', 'max_output_tokens',
        'description', 'workdir', 'cwd', 'login', 'shell', 'tty', 'run_in_background',
    }:
        return None
    # Host-normalized metadata stays in the action digest. Never pretend a
    # caller-selected different execution directory is the current project.
    for key in ('workdir', 'cwd'):
        value = inputs.get(key)
        if value is not None:
            if not isinstance(value, str) or not isinstance(action.get('cwd'), str):
                return None
            root = Path(context).resolve()
            selected = Path(value)
            if not selected.is_absolute():
                selected = root / selected
            if selected.resolve() != root:
                return None
    if inputs.get('shell') is not None and inputs['shell'] not in {
        'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe', 'bash', '/bin/bash', '/bin/sh',
    }:
        return None
    command = inputs.get('command')
    if not isinstance(command, str) or command != action.get('command', command):
        return None
    # Single component, without expansion, quoting, private paths or secret
    # indicators. Do not claim support for arbitrary commands/targets.
    safe = r'([A-Za-z][A-Za-z0-9_.-]{0,79})'
    match = re.fullmatch(r'rm (-rf|-fr|-r|-f) ' + safe, command)
    if match:
        target = match[2]
        if re.search(r'secret|token|private|password|credential|account|AKIA|ghp_', target, re.I):
            return None
        effects = {'-rf': 'recursive forced removal', '-fr': 'recursive forced removal',
                   '-r': 'recursive removal', '-f': 'forced removal'}
        return f'Operation: {effects[match[1]]}. Target: {target} in the current project working directory. Options: {match[1]}.'
    match = re.fullmatch(r'Remove-Item -LiteralPath ' + safe + r' -Recurse( -Force)?', command, re.I)
    if match and not re.search(r'secret|token|private|password|credential|account|AKIA|ghp_', match[1], re.I):
        return f'Operation: recursive removal. Target: {match[1]} in the current project working directory. Force: {bool(match[2])}.'
    if command == 'git reset --hard':
        return 'Operation: discard all tracked working-tree and index changes in the current project repository, resetting to HEAD. Options: --hard.'
    return None


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _identity(state_dir, session_id, harness):
    if state_dir is None or not isinstance(session_id, str) or not session_id:
        raise ValueError('Session scope is required')
    if harness not in {'codex', 'claude'}:
        raise ValueError('Harness scope is required')
    root = Path(state_dir).absolute()
    from guard_settings import current
    scope = _digest([str(root.resolve()), session_id, harness, current().identity])
    return root / 'destructive_confirmation_v2' / (scope + '.json'), scope


def _no_links(path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked confirmation state refused')


@contextmanager
def _locked(path):
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    _no_links(lock)
    with lock.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
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


def _read(path, scope, now):
    _no_links(path)
    if not path.exists():
        return None
    if path.stat().st_size > 2048:
        raise ValueError('Invalid confirmation state')
    record = json.loads(path.read_text('utf-8'))
    if (not isinstance(record, dict) or set(record) != FIELDS or record['schema'] != SCHEMA
            or record['scope'] != scope or not isinstance(record['action'], str)
            or not HASH.fullmatch(record['action'])
            or record['state'] not in {'pending', 'armed', 'consumed', 'cancelled', 'conflicted'}
            or any(type(record[key]) not in {float, int} for key in ('created', 'expires'))
            or not 0 < record['expires'] - record['created'] <= TTL_SECONDS
            or (record['approval_turn'] is not None and (
                not isinstance(record['approval_turn'], str) or not HASH.fullmatch(record['approval_turn'])))):
        raise ValueError('Invalid confirmation state')
    if not record['created'] <= now < record['expires']:
        return None
    return record


def _write(path, record):
    _no_links(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        # Best-effort temporary cleanup; a failure never grants confirmation.
        try: temporary.unlink()
        except FileNotFoundError: pass


def _policy_request(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from guard_settings import policy_scope
        with policy_scope() as policy:
            if policy.health == 'invalid':
                return False
            return function(*args, **kwargs)
    return wrapped


@_policy_request
def pending_or_approved(state_dir, action, *, session_id, harness, turn_id=None, now=None):
    """Publish one pending digest, or inspect a matching unspent confirmation.

    A different pending action invalidates both candidates until cancellation.
    Inspection does not spend it; the hook consumes only after all other gates.
    """
    from guard_settings import enabled, unchanged
    if not enabled('innate-02') or not unchanged():
        return False
    now = time.time() if now is None else now
    path, scope = _identity(state_dir, session_id, harness)
    action_id = _digest(action)
    with _locked(path):
        record = _read(path, scope, now)
        if record and record['state'] == 'conflicted':
            return False
        if record and record['state'] in {'pending', 'armed'}:
            if record['action'] != action_id:
                record['state'] = 'conflicted'
                record['approval_turn'] = None
                _write(path, record)
                return False
            if presentation(action) is None:
                record['state'] = 'cancelled'
                _write(path, record)
                return False
            return bool(record['state'] == 'armed' and record['action'] == action_id
                        and (record['approval_turn'] is None or record['approval_turn'] == _digest(turn_id)))
        if presentation(action) is None:
            return False
        _write(path, {'schema': SCHEMA, 'scope': scope, 'action': action_id, 'state': 'pending',
                      'created': now, 'expires': now + TTL_SECONDS, 'approval_turn': None})
        return False


@_policy_request
def observe_prompt(state_dir, prompt, *, session_id, harness, turn_id=None, now=None):
    from guard_settings import enabled, unchanged
    if not enabled('innate-02') or not unchanged():
        return False
    now = time.time() if now is None else now
    path, scope = _identity(state_dir, session_id, harness)
    if not path.exists():
        return False  # No pre-arming, nor state writes on ordinary prompts.
    with _locked(path):
        record = _read(path, scope, now)
        if record is None or record['state'] not in {'pending', 'armed', 'conflicted'}:
            return False
        if prompt != PHRASE:
            record['state'] = 'cancelled'
            _write(path, record)
            return False
        if record['state'] != 'pending':
            return False
        record['state'] = 'armed'
        record['approval_turn'] = _digest(turn_id) if turn_id else None
        _write(path, record)
        return True


@_policy_request
def consume(state_dir, action, *, session_id, harness, turn_id=None, now=None):
    from guard_settings import enabled, unchanged
    if not enabled('innate-02') or not unchanged():
        return False
    now = time.time() if now is None else now
    path, scope = _identity(state_dir, session_id, harness)
    with _locked(path):
        record = _read(path, scope, now)
        if (not unchanged() or record is None or presentation(action) is None or record['state'] != 'armed' or record['action'] != _digest(action)
                or (record['approval_turn'] is not None and record['approval_turn'] != _digest(turn_id))):
            return False
        record['state'] = 'consumed'
        _write(path, record)
        return True


def invalidate(state_dir, *, session_id, harness, now=None):
    """Cancel an old request on an unpresentable/refused operation; never arm."""
    now = time.time() if now is None else now
    path, scope = _identity(state_dir, session_id, harness)
    if not path.exists():
        return
    with _locked(path):
        record = _read(path, scope, now)
        if record and record['state'] in {'pending', 'armed', 'conflicted'}:
            record['state'] = 'cancelled'
            record['approval_turn'] = None
            _write(path, record)


def refusal_message(state_dir=None, action=None, *, session_id=None, harness=None,
                    turn_id=None, now=None):
    """Show only a complete safe preview; state itself contains digests only."""
    if state_dir is None or action is None:
        return 'DESTRUCTIVE ACTION PAUSED: no matching pending confirmation is available.'
    now = time.time() if now is None else now
    path, scope = _identity(state_dir, session_id, harness)
    with _locked(path):
        record = _read(path, scope, now)
    if record and record['state'] == 'conflicted':
        return ('DESTRUCTIVE ACTION PAUSED: conflicting requests were presented. '
                'You cannot confirm either request. Cancel the pending requests with '
                'an ordinary message, then present only the intended request again.')
    preview = presentation(action)
    if preview is None:
        return ('DESTRUCTIVE ACTION PAUSED: cannot present this action safely; confirmation unavailable. '
                'Complex, indirect, private or unsupported actions are not confirmable in this build.')
    if (record is None or record['state'] not in {'pending', 'armed'}
            or record['action'] != _digest(action)):
        return 'DESTRUCTIVE ACTION PAUSED: no matching pending confirmation is available.'
    return ('DESTRUCTIVE ACTION PAUSED\n\n' + preview + '\nRequest fingerprint: ' + record['action'] +
            '\nConfirm only the matching tool request by sending exactly '
            '`allostat:override` as your entire next message. Confirmation expires after five minutes, '
            'applies only to the unchanged action in this session, and is used once. '
            'Another message cancels the pending confirmation. This is an accident-prevention check, '
            'not tamper-proof protection against software running as you.')
