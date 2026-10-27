"""Files this session wrote that could later be EXECUTED (ISSUE-005).

innate-02 reads the command it is given, so `bash /tmp/cl.sh` tells it nothing
about the `rm -rf` inside the script. Measured on the bench VM, minutes apart in
one session: the script ran unguarded; the identical `rm -rf` typed inline was
blocked.

This module is the small amount of memory that closes the gap for the one class
that is actually closable — a script the SAME session wrote. PostToolUse records
what was written; PreToolUse asks whether the command is about to run one of
them.

Deliberately not attempted: indirection in general. `curl | sh`, base64, a
Makefile target, `eval`, a script that existed before the session. String
matching at PreToolUse cannot see through those, and the limitation is written
into the guard's own message rather than left for someone to discover.

Per-session file so concurrent sessions in one project cannot see each other's
writes — a script session A wrote is not session B's business, and sharing the
set would make the guard fire on unrelated work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Suffixes worth tracking: things an interpreter runs. A written .md or .json
# is not going to be executed, and tracking everything would make the guard
# fire on ordinary work.
EXECUTABLE_SUFFIXES = frozenset({
    ".sh", ".bash", ".zsh", ".ksh", ".fish",
    ".py", ".pl", ".rb", ".js", ".mjs", ".cjs", ".ts",
    ".ps1", ".psm1", ".bat", ".cmd", ".command",
})

# A written script is only interesting until the session ends, and an unbounded
# list would be a slow read on every single tool call.
MAX_TRACKED = 200

_STATE_SUBDIR = "state"


def _path(state_dir: Path, session_id: str | None) -> Path:
    key = (session_id or "unknown").replace("/", "-").replace("\\", "-")
    return Path(state_dir) / _STATE_SUBDIR / f"session_written_scripts_{key}.json"


def looks_executable(file_path: str | None) -> bool:
    """True when a written file is the kind of thing an interpreter runs.

    A suffix-less file counts: `/tmp/cl` written and then `bash /tmp/cl` is the
    same hazard as `/tmp/cl.sh`, and a shell script without an extension is
    ordinary.
    """
    if not file_path:
        return False
    name = Path(str(file_path)).name
    if not name:
        return False
    suffix = Path(name).suffix.lower()
    return suffix in EXECUTABLE_SUFFIXES or suffix == ""


def record_write(state_dir: Path, session_id: str | None, file_path: str | None) -> bool:
    """Note that this session wrote `file_path`. Returns True if recorded.

    Best-effort: a tracker that cannot write must not break the tool call it
    was observing. It fails OPEN by design — the consequence is that innate-02
    keeps the coverage it had before this module existed, which is the status
    quo, not a new hole.
    """
    if not looks_executable(file_path):
        return False
    try:
        target = _path(state_dir, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        known = read_written(state_dir, session_id)
        normalized = str(file_path).replace("\\", "/")
        if normalized in known:
            return True
        ordered = list(known) + [normalized]
        if len(ordered) > MAX_TRACKED:
            ordered = ordered[-MAX_TRACKED:]
        target.write_text(json.dumps({"paths": ordered}, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def read_written(state_dir: Path, session_id: str | None) -> list[str]:
    """Paths this session wrote. [] on anything unreadable."""
    try:
        data = json.loads(_path(state_dir, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    paths = data.get("paths") if isinstance(data, dict) else None
    return [str(p) for p in paths] if isinstance(paths, list) else []
