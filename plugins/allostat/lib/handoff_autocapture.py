"""A3 (2026-06-26) — transcript→handoff floor-capture (detect-before-write).

The A3 watchdog (`handoff_watchdog.py`) only NAGS. If the agent never writes a
rolling handoff, nothing captures the session — continuity is lost. This module
is the floor: when a session reaches the failure boundary (escalation at the
ceiling OR ending overdue) with NO substantive handoff on disk, it writes a
minimal handoff distilled from the transcript so the next session has SOMETHING
to resume from.

Strict detect-before-write (this is the only module in the A3 stack that WRITES
operator-tree content):
  - Writes ONLY when ALL of: at-ceiling-or-ending-overdue; AND no substantive
    handoff already exists (reuse `handoff_watchdog._passes_anti_pattern_check`);
    AND a readable transcript exists.
  - NEVER overwrites a substantive handoff — the existence check is the guard,
    and a substantive file short-circuits before any write.
  - NEVER overwrites ANY non-empty agent-authored file, substantive or not
    (audit 2026-07-20). `_passes_anti_pattern_check` is a heuristic: a real,
    detailed handoff that puts everything under `## Focus` and honestly marks
    the other three sections `(none)` scores 1-of-4 and FAILS it. That is
    precisely the file sitting at the escalation ceiling where the floor fires,
    so the failing check used to guarantee the floor clobbered it. The floor now
    diverts to a sibling `<session_id>.autocaptured.md` whenever the canonical
    path already holds bytes this module did not write. Archive-never-destroy is
    absolute here, matching every sibling mutation in the memory subsystem
    (_backup_then_write, _archive_reconcile_loser, MEMORY.md.PRE_*).

Caveat #3 (advisor signoff 2026-06-26): the floor is continuity INSURANCE, not
"protocol satisfied". The file carries `autocaptured: true` + `source:
transcript-floor` frontmatter, and `handoff_watchdog._passes_anti_pattern_check`
treats any `autocaptured: true` file as NON-substantive. So the next session's
watchdog still expects — and nags for — a substantive human-authored
replacement. The floor never resets the watchdog.

Privacy invariant: wrapper-side only. The transcript is read locally; nothing
new crosses to the server.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Frontmatter markers. `_passes_anti_pattern_check` keys off AUTOCAPTURED_MARKER
# so an autocaptured floor never counts as a substantive handoff.
AUTOCAPTURED_MARKER = "autocaptured: true"
SOURCE_MARKER = "source: transcript-floor"

# Process-lifetime sequence counter so two temps created in the same
# microsecond by this process still get distinct, uniquely-owned names.
_FLOOR_TMP_SEQ = itertools.count()

# How many recent transcript text turns to distill into the floor handoff.
_MAX_RECENT_TURNS = 12
# Per-turn excerpt cap so the floor stays lean.
_TURN_EXCERPT_CHARS = 280


def _read_transcript_turns(
    transcript_path: Path,
    harness: str = "claude",
) -> list[tuple[str, str]]:
    """Return [(role, text)] for user/assistant text turns in the transcript
    JSONL, oldest→newest. Best-effort: returns [] on any read/parse failure.

    Slice 2 (write-loop): dispatched by explicit `harness` parameter — the
    Stop hook knows its harness from `--harness` argv and threads it through
    `maybe_write_floor_handoff`. Default "claude" keeps every existing caller
    byte-for-byte unchanged. The two harnesses write different JSONL shapes:

      claude: {"type": "user"|"assistant", "message": {"content": [...]}}
      codex:  {"type": "response_item", "payload": {"type": "message",
               "role": ..., "content": [...]}}  (rollout file)
    """
    if harness == "codex":
        return _read_codex_transcript_turns(transcript_path)
    return _read_claude_transcript_turns(transcript_path)


def _read_claude_transcript_turns(transcript_path: Path) -> list[tuple[str, str]]:
    """Claude Code transcript reader (the original A3 implementation).
    Mirrors the JSONL-walking convention in interrupt_detection.py."""
    turns: list[tuple[str, str]] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    continue
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue
                content = message.get("content", [])
                # content may be a plain string or a list of blocks. Strict
                # type=="text" filter — unchanged from the original A3 reader.
                texts: list[str] = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text", "")
                            if isinstance(t, str):
                                texts.append(t)
                joined = " ".join(t.strip() for t in texts if t.strip())
                if not joined:
                    continue
                # Skip Claude Code synthetic interrupt markers — not real content.
                if joined.startswith("[Request interrupted by user"):
                    continue
                turns.append((etype, joined))
    except OSError:
        return []
    return turns


# Harness-injected user-role preambles in Codex rollouts (verified against
# real rollout files 2026-07-05). Not operator content — excluded from the
# floor distill so the handoff reflects the actual conversation.
_CODEX_PREAMBLE_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<user_instructions>",
)


def _read_codex_transcript_turns(transcript_path: Path) -> list[tuple[str, str]]:
    """Codex rollout-jsonl reader (slice 2).

    Shape (verified via probe + real rollouts 2026-07-05):
      {"type": "response_item", "payload": {"type": "message",
       "role": "user"|"assistant"|"developer",
       "content": [{"type": "input_text"|"output_text", "text": ...}]}}

    NOTE the content block `type` is input_text/output_text, NOT "text" —
    extraction keys on the presence of a string `text` field, not the block
    type. role=developer items (permissions, truncation warnings) and
    harness-injected user preambles are skipped.
    """
    turns: list[tuple[str, str]] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "response_item":
                    continue
                payload = entry.get("payload", {})
                if not isinstance(payload, dict) or payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                joined = _join_content_texts(payload.get("content", []))
                if not joined:
                    continue
                if role == "user" and joined.startswith(_CODEX_PREAMBLE_PREFIXES):
                    continue
                turns.append((role, joined))
    except OSError:
        return []
    return turns


def _join_content_texts(content) -> str:
    """Join the text of a Codex message's content — a plain string or a list
    of blocks. Any dict block carrying a string `text` field counts (Codex
    block types are "input_text"/"output_text"; keying on the text field
    survives future block-type renames). The Claude reader keeps its own
    strict type=="text" inline filter — unchanged."""
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                t = c.get("text", "")
                if isinstance(t, str) and t:
                    texts.append(t)
    return " ".join(t.strip() for t in texts if t.strip())


def _is_autocaptured_file(path: Path) -> bool:
    """True iff `path` carries this module's `autocaptured: true` marker in its
    LEADING frontmatter — i.e. a floor handoff THIS module wrote, which the
    floor may freely refresh.

    Mirrors `handoff_watchdog._passes_anti_pattern_check`'s marker scan
    (utf-8-sig + BOM/whitespace lstrip, frontmatter-scoped so a body mention
    can never be mistaken for the marker). Any read failure returns False —
    unreadable means "not ours", which routes the write to the sibling instead
    of over the operator's bytes.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    marker_text = text.lstrip("﻿").lstrip()
    if not marker_text.startswith("---"):
        return False
    fm_end = marker_text.find("\n---", 3)
    frontmatter = marker_text[:fm_end] if fm_end > 0 else marker_text[:400]
    return AUTOCAPTURED_MARKER in frontmatter


def _holds_foreign_bytes(path: Path) -> bool:
    """True iff `path` exists, is non-empty, and was NOT written by this module.

    "Foreign" = operator/agent content. The floor must never write over it.
    An unstattable path is treated as foreign (fail closed toward preservation).
    """
    try:
        if not path.exists():
            return False
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return True
    return not _is_autocaptured_file(path)


def _render_floor_handoff(session_id: str, turns: list[tuple[str, str]]) -> str:
    """Render the floor handoff markdown. Carries the autocaptured frontmatter
    (so the watchdog won't treat it as substantive) and the six fixed sections,
    with a transcript-distilled Focus + an explicit verify-and-replace banner."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    recent = turns[-_MAX_RECENT_TURNS:]
    focus_lines: list[str] = []
    for role, text in recent:
        excerpt = text[:_TURN_EXCERPT_CHARS].replace("\n", " ").strip()
        if len(text) > _TURN_EXCERPT_CHARS:
            excerpt += "…"
        label = "operator" if role == "user" else "assistant"
        focus_lines.append(f"- ({label}) {excerpt}")
    focus_body = "\n".join(focus_lines) if focus_lines else "- (transcript had no text turns)"

    return (
        "---\n"
        f"{AUTOCAPTURED_MARKER}\n"
        f"{SOURCE_MARKER}\n"
        f"session_id: {session_id}\n"
        f"captured_at: {stamp}\n"
        "---\n\n"
        f"# {session_id} — AUTOCAPTURED FLOOR HANDOFF\n\n"
        "> ⚠ This is a machine-distilled FLOOR handoff written because no "
        "substantive\n"
        "> agent-authored handoff existed when the session reached the watchdog "
        "ceiling.\n"
        "> It is continuity INSURANCE, not a completed handoff. The next session "
        "must\n"
        "> VERIFY this against the transcript and REPLACE it with a substantive, "
        "human-\n"
        "> authored handoff — the watchdog still expects one (this file does not "
        "satisfy it).\n\n"
        "## Focus\n"
        "Reconstructed from the recent transcript turns (verify before trusting):\n"
        f"{focus_body}\n\n"
        "## Decisions\n"
        "(autocaptured — not extracted; verify against transcript and fill in)\n\n"
        "## Memory pointers\n"
        "(autocaptured — verify against transcript and fill in)\n\n"
        "## Open threads\n"
        "(autocaptured — the session ended without a substantive handoff; "
        "reconstruct open work from the transcript)\n\n"
        "## Blocked\n(none)\n\n"
        "## Queued\n"
        "- Replace this floor handoff with a substantive human-authored one.\n"
    )


def _land_floor_write(
    write_target: Path, canonical_target: Path, content: str
) -> "Path | None":
    """Land `content` at (or beside) `write_target` WITHOUT ever overwriting
    bytes this module did not author.

    Round-7 inversion (advisor 2026-07-23): round 6 exclusive-created the
    FINAL shared pathname directly and, on a failed write, closed the
    descriptor and unlinked that shared name — a non-cooperating writer that
    replaced the pathname in the close->unlink window had its bytes deleted
    by cleanup that never proved it still owned the name. Cleanup must only
    ever touch a name THIS call provably owns. So the mechanics move to
    temp+link:

      - ALL bytes are written and fsync'd to a UNIQUELY-OWNED temp name
        (O_EXCL; pid + timestamp + a process-lifetime sequence — no other
        writer can produce this exact name). Partial bytes never reach any
        shared pathname, on any path, including failure paths.
      - Publish is `os.link(temp, dest)` — an atomic no-clobber primitive the
        OS enforces against ALL writers, cooperating or not, with no
        check-then-create window. `FileExistsError` is the expected
        someone-else-won-the-name signal: discard the temp, version around
        (round-6's occupied-destination contract, unchanged).
      - The containing directory is fsync'd after a successful link on
        POSIX, so the publish survives a crash. Windows cannot fsync a
        directory handle; a no-op there.
      - Cleanup unlinks ONLY the temp — ours by construction at every point
        in time — never the destination or the versioned sibling.
      - Advisor correction 5: any OTHER OSError from os.link (EPERM/ENOSYS on
        network shares, FAT32, container mounts) means this filesystem
        cannot do atomic no-clobber at all. There is NO fallback write to a
        shared name — that would re-expose partial bytes at a shared
        pathname, the three-rounds bug by another route. Instead: fail
        CLOSED — keep the fsynced temp (content preserved, not lost), write
        nothing to the canonical or versioned name, surface the degradation
        loudly, return None.

    Destination selection (round-6 contract, unchanged): destination absent
    -> publish at the canonical name; destination occupied (ours, empty, or
    foreign) -> publish at a VERSIONED sibling
    `<stem>.floor-<utcstamp>-<pid><suffix>` instead. These versioned siblings
    are plain `<sid>*.md` files like any other handoff: `handoff_discoverer.py`
    picks them up by its ordinary newest-first glob, with no marker involved
    — the marker only distinguishes floor identity elsewhere
    (`handoff_watchdog.py`'s "is this an autocaptured floor?" check).

    Returns the Path actually landed, or None when nothing could be landed.
    Never raises — continuity insurance.
    """
    try:
        write_target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    data = content.encode("utf-8")

    def _write_unique_temp() -> "Path | None":
        """All bytes + fsync into a name ONLY this call can own: pid,
        timestamp, and a process-lifetime sequence make it unique, O_EXCL
        proves it. Round-7 inversion (advisor 2026-07-23): cleanup ownership
        must be PROVEN, and this name is the only one it is proven for."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        tmp = write_target.with_name(
            f"{write_target.stem}.floortmp-{stamp}-{os.getpid()}"
            f"-{next(_FLOOR_TMP_SEQ)}{write_target.suffix}"
        )
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return None
        ok = False
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    return None   # short write: never publish truncated bytes
                view = view[written:]
            os.fsync(fd)
            ok = True
            return tmp
        except OSError:
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                # Best-effort: the write/fsync result above already stands.
                pass
            if not ok:
                try:
                    os.unlink(tmp)   # OUR unique temp — provably never foreign
                except OSError:
                    # Best-effort: if removal fails, the caller already sees
                    # `tmp is None` and treats the landing as failed.
                    pass

    def _fsync_dir(d: Path) -> None:
        """POSIX: fsync the directory so the published link survives a crash
        (a linked file without its directory entry is not published). Windows
        cannot open directories for fsync — no-op there."""
        if os.name == "nt":
            return
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            # Best-effort: the link already succeeded; a directory-fsync
            # failure only widens the crash-durability window, it does not
            # change what got published.
            pass

    def _publish(dest: Path, tmp: Path) -> str:
        """Atomic no-clobber publish: 'linked' | 'exists' | 'unsupported'.

        os.link fails with FileExistsError when dest exists — enforced by the
        OS against ALL writers, cooperating or not, with no window between
        check and create. Partial bytes never appear at dest: the linked temp
        is complete and fsynced. Advisor correction 5: any OTHER OSError
        (EPERM/ENOSYS on network shares, FAT32, container mounts) means the
        filesystem cannot do atomic no-clobber — there is NO fallback write
        to a shared name (that re-exposes partial bytes at a shared pathname,
        the exact three-rounds bug); the caller keeps the temp and degrades
        loudly instead."""
        try:
            os.link(tmp, dest)
        except FileExistsError:
            return "exists"
        except OSError:
            return "unsupported"
        _fsync_dir(dest.parent)
        return "linked"

    tmp = _write_unique_temp()
    if tmp is None:
        return None
    keep_temp = False
    try:
        if not write_target.exists():
            result = _publish(write_target, tmp)
            if result == "linked":
                return write_target
            if result == "unsupported":
                keep_temp = True
                logger.warning(
                    "handoff floor NOT published: filesystem does not support "
                    "hard link at %s; content preserved in %s", write_target, tmp,
                )
                return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        versioned = write_target.with_name(
            f"{write_target.stem}.floor-{stamp}-{os.getpid()}{write_target.suffix}"
        )
        result = _publish(versioned, tmp)
        if result == "linked":
            return versioned
        if result == "unsupported":
            keep_temp = True
            logger.warning(
                "handoff floor NOT published: filesystem does not support "
                "hard link at %s; content preserved in %s", versioned, tmp,
            )
        return None
    finally:
        if not keep_temp:
            try:
                os.unlink(tmp)   # the ONLY cleanup — a name proven ours by O_EXCL
            except OSError:
                # Best-effort: the landing result above already stands; a
                # stray temp left behind here is inert (unique name, never
                # read again) rather than a correctness issue.
                pass


def maybe_write_floor_handoff(
    transcript_path: Optional[Path],
    project_root: Path,
    session_id: Optional[str],
    *,
    ending_overdue: bool = False,
    escalation_level: int = 0,
    at_ceiling: bool = False,
    harness: str = "claude",
) -> Optional[Path]:
    """Write a floor handoff iff the strict detect-before-write conditions hold.

    Returns the path written, or None when nothing was written (the common case).

    Conditions (ALL must hold):
      1. session_id is present and the watchdog isn't disabled.
      2. The session is at the failure boundary: `ending_overdue` OR
         `at_ceiling` OR escalation_level >= the watchdog ceiling.
      3. A readable transcript with at least one text turn exists.
      4. No substantive handoff already exists at the canonical path
         (`_passes_anti_pattern_check` is False there) — so a real handoff is
         NEVER overwritten.

    Destination (round-6 redesign, reviewer 2026-07-23): the canonical
    `<session_id>.md` is the preferred slot when it is absent, empty, or an
    autocaptured floor this module wrote. Any other bytes there are
    operator/agent content — the floor then prefers the sibling
    `<session_id>.autocaptured.md` slot instead. Either way, landing itself is
    exclusive-create-only (`_land_floor_write`): if the preferred slot is
    already occupied by the time of the actual write — including our OWN prior
    floor, refreshed — the write lands at a VERSIONED sibling
    (`<stem>.floor-<utcstamp>-<pid><suffix>`) instead. Nothing is EVER
    overwritten, truncated, or deleted; a refresh of our own earlier floor now
    always produces a newer versioned file rather than an in-place update.
    Versioned siblings are plain `<sid>*.md` files picked up by
    `handoff_discoverer.py`'s ordinary newest-first glob by recency — no
    marker involved in that selection. `AUTOCAPTURED_MARKER` is what floor-
    file identity is decided by elsewhere (`handoff_watchdog.py`'s frontmatter
    check), not what discovery ranks on.

    Never raises — best-effort continuity insurance.
    """
    try:
        import handoff_watchdog  # noqa: E402

        if handoff_watchdog.disabled() or not session_id:
            return None
        if transcript_path is None:
            return None

        # (2) failure boundary
        ceiling = handoff_watchdog.MAX_ESCALATION_LEVEL
        at_boundary = ending_overdue or at_ceiling or escalation_level >= ceiling
        if not at_boundary:
            return None

        # (4) detect-before-write: a substantive handoff short-circuits.
        from session_handoff import resolve_canonical_handoff_dir  # noqa: E402
        handoff_dir = resolve_canonical_handoff_dir(project_root)
        target = handoff_dir / f"{session_id}.md"
        if target.exists() and handoff_watchdog._passes_anti_pattern_check(target):
            return None

        # (3) transcript must exist and have content. `harness` picks the
        # transcript format reader (claude default / codex rollout).
        turns = _read_transcript_turns(transcript_path, harness=harness)
        if not turns:
            return None

        # Destination selection — never write over bytes we did not author.
        write_target = target
        if _holds_foreign_bytes(target):
            write_target = handoff_dir / f"{session_id}.autocaptured.md"
            if _holds_foreign_bytes(write_target):
                # Both slots hold operator/agent content. Continuity insurance
                # is never worth destroying real writing — decline.
                return None

        # Render, THEN land atomically. Rendering is the last thing before the
        # write and thus the only place a concurrent writer can still land bytes
        # in the check->write window (F1 TOCTOU). `_land_floor_write` is now
        # exclusive-create-only (no replace path at all), so a writer that
        # lands bytes at write_target in that window simply loses the create
        # race — the floor falls through to a versioned sibling instead of
        # ever overwriting what appeared. Returns the path actually landed
        # (may be a versioned sibling), or None only on a filesystem failure.
        content = _render_floor_handoff(session_id, turns)
        landed = _land_floor_write(write_target, target, content)
        return landed
    except Exception:
        # Continuity insurance must never break a hook.
        return None
