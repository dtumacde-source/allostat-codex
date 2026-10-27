"""Allostat apoptotic rule retirement — v2.4 wrapper port (PATCH-136 / v0.4.0).

Ported from the v2.3 plugin's apoptotic_retirement module and adapted
for v2.4 wrapper architecture:

- Files are .md (not .yaml) since v2.4 memory tree is markdown
- Source path is operator's project memory tree at
  ~/.claude/projects/<sanitized-cwd>/memory/ (not v2.3's <proj>/.allostat/rules/)
- Frontmatter-based state mechanism is preserved (works for .md too)

Apoptosis (programmed cell death) preserves surrounding tissue; necrosis
(uncontrolled death) damages neighbors. Apoptotic rule retirement = graceful
deprecation with notice. The retiring rule stays in MEMORY.md during the
window with a "this rule retires in N sessions" notice on each tick.

API surface:
- initiate_retirement(rule_path, reason)  → /allostat-retire mechanics
- cancel_retirement(rule_path)            → /allostat-retain mechanics
- tick_session(rule_path)                 → decrement counter (called per session start)
- tick_retiring_rules(memory_dir)         → batch tick across a memory tree
- list_retiring_rules(memory_dir)         → /allostat-tend audit input
- finalize_retirement(rule_path)          → move to _RETIRED/ at counter=0
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from local_state import utc_iso


DEPRECATION_WINDOW_SESSIONS = 5
RETRIEVAL_TAPER = [1.0, 0.8, 0.6, 0.4, 0.2]

# UTF-8 BOM (H-13): some editors (notably on Windows) write a leading byte-
# order mark before the frontmatter `---` marker. `Path.read_text(encoding=
# "utf-8")` does NOT strip it (only "utf-8-sig" does), so it survives as a
# literal `﻿` character at text[0]. Every frontmatter-boundary check in
# this module must account for it or the `---\n` offset is wrong and the
# whole block is treated as "no frontmatter".
_BOM = "﻿"


@dataclass
class RetirementStatus:
    rule_id: str
    state: str  # "active" | "retiring" | "retired"
    sessions_remaining: int
    retrieval_priority_multiplier: float
    rule_path: Path | None = None


def initiate_retirement(rule_path: Path, reason: str = "operator_directed") -> bool:
    """Mark a .md rule file as retiring. Adds frontmatter fields:
        retirement_state: retiring
        retirement_initiated_at: <ISO>
        retirement_reason: <reason>
        retirement_sessions_remaining: 5

    Returns True on success.
    """
    if not rule_path.exists():
        return False

    try:
        text = rule_path.read_text(encoding="utf-8")
        retirement_block = (
            f"retirement_state: retiring\n"
            f"retirement_initiated_at: {utc_iso()}\n"
            f"retirement_reason: {reason}\n"
            f"retirement_sessions_remaining: {DEPRECATION_WINDOW_SESSIONS}\n"
        )

        span = _frontmatter_slice(text)
        if span is not None:
            start, end = span
            # `end` is the index of the `\n` that opens the closing `\n---\n`
            # marker; text[start:end] therefore excludes that newline, which
            # would otherwise cause the LAST frontmatter line (always
            # `retirement_sessions_remaining` on a re-retire — C-03) to be
            # missed by the strip regex below because it requires a trailing
            # `\n` to match. Re-append it before stripping.
            fm_text = text[start:end] + "\n"
            fm_text = re.sub(
                r"^retirement_(state|initiated_at|reason|sessions_remaining):.*\n",
                "",
                fm_text,
                flags=re.MULTILINE,
            )
            new_text = text[:start] + fm_text + retirement_block + "---\n" + text[end + 5:]
            # Crash-safety (H-12): back up the operator's rule content, then
            # write atomically — same hardened path as tick_session's terminal
            # rewrite. A bare write_text() here could truncate the whole rule
            # on an interrupted write with nothing to recover from.
            _backup_then_write(rule_path, text, new_text)
            return True

        # No frontmatter block found. Preserve a leading UTF-8 BOM (H-13) —
        # the new frontmatter must be inserted AFTER the BOM, not before it,
        # or the BOM stops marking the start of the file.
        bom = _BOM if text.startswith(_BOM) else ""
        body = text[len(bom):]
        new_text = f"{bom}---\n{retirement_block}---\n{body}"
        _backup_then_write(rule_path, text, new_text)
        return True
    except OSError:
        return False


def cancel_retirement(rule_path: Path) -> bool:
    """Operator changed their mind. Strip retirement FRONTMATTER.

    Frontmatter-scoped (audit 2026-07-20): the strip runs over the leading
    `---` block ONLY, exactly as initiate_retirement's re-retire strip and
    tick_session's rewrites do. A whole-file MULTILINE sub deleted any
    column-0 BODY line starting with one of the four keys — e.g. a memory note
    documenting how the retirement machine works, or a rules example — silently
    removing the operator's prose. Body text is never rewritten here; see
    `_sub_in_frontmatter` for the same guard on the other mutating paths.

    A file with no frontmatter has nothing to strip and is left byte-identical.
    """
    if not rule_path.exists():
        return False
    try:
        text = rule_path.read_text(encoding="utf-8")
        span = _frontmatter_slice(text)
        if span is None:
            # No frontmatter → no retirement declaration → nothing to strip.
            # (A body-only match must NEVER be rewritten.)
            return True
        start, end = span
        fm_body = text[start:end]
        if fm_body:
            # `end` indexes the `\n` opening the closing `\n---\n`, so the last
            # frontmatter line has no trailing newline in the slice; re-append
            # one or the strip regex (which requires `\n`) misses it — the same
            # off-by-one initiate_retirement documents at its own strip.
            fm_body = re.sub(
                r"^retirement_(state|initiated_at|reason|sessions_remaining):.*\n",
                "",
                fm_body + "\n",
                flags=re.MULTILINE,
            )
        new_text = text[:start] + fm_body + "---\n" + text[end + 5:]
        # Crash-safety (H-12): back up + atomic write, mirroring tick_session.
        _backup_then_write(rule_path, text, new_text)
        return True
    except OSError:
        return False


def tick_session(rule_path: Path) -> RetirementStatus:
    """Decrement the retirement counter when a new session starts.
    If counter hits 0, the caller (or finalize_retirement) handles archival.
    """
    if not rule_path.exists():
        return RetirementStatus(rule_path.stem, "active", 0, 1.0, rule_path)

    try:
        text = rule_path.read_text(encoding="utf-8")
    except OSError:
        return RetirementStatus(rule_path.stem, "active", 0, 1.0, rule_path)

    # Frontmatter-scoped read: a body-text `retirement_state:` line must never
    # drive the destructive state machine (data-safety guard).
    state = _extract_frontmatter_field(text, "retirement_state", "active")
    sessions_remaining = _safe_int(
        _extract_frontmatter_field(text, "retirement_sessions_remaining", "0")
    )

    if state != "retiring":
        return RetirementStatus(
            rule_id=rule_path.stem,
            state=state,
            sessions_remaining=sessions_remaining,
            retrieval_priority_multiplier=1.0,
            rule_path=rule_path,
        )

    sessions_remaining -= 1
    if sessions_remaining <= 0:
        new_text = _sub_in_frontmatter(
            text,
            r"^retirement_state:.*$",
            "retirement_state: retired",
        )
        new_text = _sub_in_frontmatter(
            new_text,
            r"^retirement_sessions_remaining:.*$",
            "retirement_sessions_remaining: 0",
        )
        # Crash-safety (deep-audit 2026-07-02): back up the operator's rule
        # content before the destructive terminal rewrite, then write
        # atomically — mirrors memory_lifecycle's .PRE_* convention so an
        # interrupted write can't corrupt a learned rule irrecoverably.
        _backup_then_write(rule_path, text, new_text)
        return RetirementStatus(
            rule_id=rule_path.stem,
            state="retired",
            sessions_remaining=0,
            retrieval_priority_multiplier=0.0,
            rule_path=rule_path,
        )

    new_text = _sub_in_frontmatter(
        text,
        r"^retirement_sessions_remaining:.*$",
        f"retirement_sessions_remaining: {sessions_remaining}",
    )
    # Non-terminal counter decrement: atomic write, no backup needed (the
    # mutation is a single reversible counter line, not content-destructive).
    try:
        from local_state import atomic_write_text  # noqa: E402
        atomic_write_text(rule_path, new_text)
    except Exception:
        # Best-effort: a failed counter-decrement write leaves the prior
        # (atomic, intact) file — the tick simply retries next session.
        pass

    taper_idx = DEPRECATION_WINDOW_SESSIONS - sessions_remaining
    # Guard BOTH ends (deep-audit 2026-07-02): a hand-edited counter larger
    # than the deprecation window makes taper_idx negative, and RETRIEVAL_TAPER[
    # neg] silently indexes from the end (wrong multiplier) or crashes. Clamp
    # out-of-range to the fully-tapered 0.0.
    multiplier = (
        RETRIEVAL_TAPER[taper_idx] if 0 <= taper_idx < len(RETRIEVAL_TAPER) else 0.0
    )

    return RetirementStatus(
        rule_id=rule_path.stem,
        state="retiring",
        sessions_remaining=sessions_remaining,
        retrieval_priority_multiplier=multiplier,
        rule_path=rule_path,
    )


def list_retiring_rules(memory_dir: Path) -> list[RetirementStatus]:
    """Scan memory_dir for .md files in deprecation window."""
    if not memory_dir.exists():
        return []

    statuses = []
    for rule_path in memory_dir.rglob("*.md"):
        # Skip the index file itself
        if rule_path.name == "MEMORY.md":
            continue
        try:
            text = rule_path.read_text(encoding="utf-8")
        except OSError:
            continue
        state = _extract_frontmatter_field(text, "retirement_state", "active")
        if state == "retiring":
            sessions_remaining = _safe_int(
                _extract_frontmatter_field(text, "retirement_sessions_remaining", "0")
            )
            taper_idx = DEPRECATION_WINDOW_SESSIONS - sessions_remaining
            # Guard BOTH ends, exactly as tick_session does: a hand-edited
            # counter larger than the window makes taper_idx negative, and
            # RETRIEVAL_TAPER[neg] silently returns a wrong multiplier via
            # Python negative indexing. Clamp out-of-range to fully-tapered 0.0.
            multiplier = (
                RETRIEVAL_TAPER[taper_idx]
                if 0 <= taper_idx < len(RETRIEVAL_TAPER)
                else 0.0
            )
            statuses.append(RetirementStatus(
                rule_id=rule_path.stem,
                state="retiring",
                sessions_remaining=sessions_remaining,
                retrieval_priority_multiplier=multiplier,
                rule_path=rule_path,
            ))
    return statuses


def tick_retiring_rules(memory_dir: Path) -> list[RetirementStatus]:
    """Batch tick. Called once per session-start by SessionStart hook.

    Returns list of transition statuses (suitable for hook to surface
    counts + just-retired notifications).
    """
    transitions: list[RetirementStatus] = []
    if not memory_dir.exists():
        return transitions

    for rule_path in memory_dir.rglob("*.md"):
        if rule_path.name == "MEMORY.md":
            continue
        try:
            text = rule_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _extract_frontmatter_field(text, "retirement_state", "active") != "retiring":
            continue
        try:
            new_status = tick_session(rule_path)
            transitions.append(new_status)
        except Exception:
            continue

    return transitions


def finalize_retirement(rule_path: Path, archive_dir: Path | None = None) -> Path | None:
    """Move a retired rule (counter=0, state=retired) to an archive folder
    and return the new path. Default archive: <memory>/_RETIRED/<YYYYMMDD>/.
    Returns None if not eligible or move failed.
    """
    if not rule_path.exists():
        return None
    try:
        text = rule_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if _extract_frontmatter_field(text, "retirement_state", "active") != "retired":
        return None

    if archive_dir is None:
        date_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        archive_dir = rule_path.parent / "_RETIRED" / date_stamp

    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / rule_path.name
        if target.exists():
            # Detect-before-write (invariant #4, archive-not-destroy): Path.rename
            # clobbers an existing destination silently on POSIX, so retiring a
            # recreated file of the same name on the same UTC day would destroy
            # the earlier _RETIRED archive and report success. Refuse instead —
            # matching the target_exists_skipped guard every sibling stage
            # transition uses (lifecycle_ladder.legacy_supersede /
            # sweep_legacy_to_purge, pruning.archive_candidate /
            # restore_archive_pass). Returning None leaves the active file in
            # place; the operator keeps both copies.
            return None
        rule_path.rename(target)
        return target
    except OSError:
        return None


# PATCH-183.3 (2026-05-23): format_retirement_notice deleted; no callers
# (debloat audit Category A1).


def _backup_then_write(rule_path: Path, original_text: str, new_text: str) -> None:
    """Write a `.PRE_<stamp>` backup of the operator's current rule content,
    then atomically replace the file. Best-effort: on any failure the original
    is left intact (atomic_write_text guarantees no partial target)."""
    try:
        from local_state import atomic_write_text, utc_iso  # noqa: E402
        stamp = utc_iso().replace(":", "").replace("-", "")
        backup = rule_path.with_name(f"{rule_path.name}.PRE_RETIRE_{stamp}")
        try:
            backup.write_text(original_text, encoding="utf-8")
        except OSError:
            pass  # backup best-effort; still write atomically below
        atomic_write_text(rule_path, new_text)
    except Exception:
        pass


def _safe_int(raw: str, default: int = 0) -> int:
    """int() with a safe default — a corrupted/hand-edited counter field must
    degrade gracefully, never crash the retirement state machine."""
    try:
        return int(raw or default)
    except (ValueError, TypeError):
        return default


def _extract_field(text: str, field_name: str, default: str = "") -> str:
    m = re.search(rf"^{re.escape(field_name)}:\s*(.+?)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")
    return default


def _frontmatter_slice(text: str) -> tuple[int, int] | None:
    """Return (start, end) indices of the leading `---` frontmatter body (the
    text BETWEEN the opening `---\\n` and the closing `\\n---\\n`), or None when
    there is no leading frontmatter block. Indices are into `text` so callers
    can scope a substitution to exactly the frontmatter.

    Tolerates a leading UTF-8 BOM (H-13) before the `---` marker — the
    returned indices still point into the ORIGINAL `text` (BOM included),
    so callers slicing `text[:start]` naturally keep the BOM as part of the
    unchanged "head" of the file.
    """
    offset = len(_BOM) if text.startswith(_BOM) else 0
    if not text.startswith("---\n", offset):
        return None
    start = offset + 4
    second = text.find("\n---\n", start)
    if second < 0:
        return None
    return (start, second)


def _sub_in_frontmatter(text: str, pattern: str, replacement: str) -> str:
    """Apply a MULTILINE re.sub to the leading frontmatter slice ONLY, leaving
    the body untouched. If there is no frontmatter, the text is returned
    unchanged — a body-only match must never be rewritten (silent mutation).
    """
    span = _frontmatter_slice(text)
    if span is None:
        return text
    head, fm, tail = text[:span[0]], text[span[0]:span[1]], text[span[1]:]
    fm = re.sub(pattern, replacement, fm, flags=re.MULTILINE)
    return head + fm + tail


def _extract_frontmatter_field(text: str, field_name: str, default: str = "") -> str:
    """Read a YAML frontmatter field, scoped to the leading `---` block ONLY.

    Unlike _extract_field (which scans the whole text with re.MULTILINE and so
    matches ANY column-0 line), this restricts the match to the frontmatter so
    a prose/body mention like a documented `retirement_state: retired` example
    cannot be mistaken for a real declaration. This is the data-safety guard for
    the DESTRUCTIVE paths (move-to-_RETIRED, in-place rewrite): only a genuine
    frontmatter declaration may trigger them. Returns the default when there is
    no frontmatter or the field is absent.
    """
    span = _frontmatter_slice(text)
    if span is None:
        return default
    fm = text[span[0]:span[1]]
    m = re.search(rf"^{re.escape(field_name)}:\s*(.+?)\s*$", fm, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")
    return default


