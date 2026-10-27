"""Pre-write existence stash (pre-release item 12).

PostToolUse classifies successful Write tool calls as `file_created`
vs `file_written`, but it fires AFTER the Write completed — the target
always exists by then, so `file_created` was unreachable and the
server-side KISS / volume-control detectors that consume it were
starved. PreToolUse stashes whether the target existed BEFORE the
write; PostToolUse pops the stash to classify.

Storage: `<state_dir>/state/pre_write_existence.json` — a small dict
{file_path: existed_bool}. Entries are consumed on pop; the map is
capped so stale entries (PostToolUse never fired) can't accumulate.

Best-effort like the rest of local state: filesystem errors degrade to
"unknown" (None) and the caller falls back to `file_written`.
"""
from __future__ import annotations

from pathlib import Path

from local_state import read_state_json, write_state_json

_STASH_FILENAME = "pre_write_existence.json"

# Keep at most this many pending entries. Normal flow pops each entry
# on the very next PostToolUse fire, so the map should hold ~1 entry;
# the cap only matters when PostToolUse misses (crash, kill).
_MAX_PENDING_ENTRIES = 32


def _stash_path(state_dir: Path) -> Path:
    return state_dir / "state" / _STASH_FILENAME


def stash_pre_write_existence(state_dir: Path, file_path: str) -> bool:
    """Record whether `file_path` exists right now (pre-write).

    Called from the PreToolUse hook on Write tool calls. Returns True
    on successful persist; False on any error (never raises).
    """
    try:
        existed = Path(file_path).exists()
    except (OSError, ValueError):
        # Can't probe the target — leave no stash so PostToolUse falls
        # back to file_written (pre-fix behavior).
        return False
    path = _stash_path(state_dir)
    data = read_state_json(path, default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data[str(file_path)] = existed
    # Cap: drop oldest entries (dict preserves insertion order).
    while len(data) > _MAX_PENDING_ENTRIES:
        data.pop(next(iter(data)))
    return write_state_json(path, data)


def pop_pre_write_existence(state_dir: Path, file_path: str) -> bool | None:
    """Consume and return the stashed pre-write existence for `file_path`.

    Returns True (existed), False (did not exist), or None when no stash
    entry is found / on any error. Never raises.
    """
    path = _stash_path(state_dir)
    data = read_state_json(path, default=None)
    if not isinstance(data, dict):
        return None
    key = str(file_path)
    if key not in data:
        return None
    existed = data.pop(key)
    write_state_json(path, data)  # best-effort consume
    return bool(existed)
