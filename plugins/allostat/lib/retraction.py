"""Allostat retraction — "forget X" that tells the truth about what it forgot.

The defect this closes (2026-08-07): the operator asked for something to be
forgotten. The memory leaf was deleted. The product reported success. The same
content remained, verbatim, in 66 rows of `observations.jsonl` and in 42 audit
files.

That is not a severity question, it is a truthfulness one. It contradicts
principle 1 of `Custody and Constraint`, published under the operator's name on
2026-07-31, which requires the record be held in storage the operator "can read
in full, can amend, and can destroy" — and principle 2, full legibility. A
deletion that leaves verbatim copies in two other stores is not destruction,
and the operator has argued in public that a system claiming this and not
delivering it is the specific failure that paper exists to name.

So this module is built around three rules:

  1. **Enumerate every store first.** A retraction can only be honest about
     stores it knows exist. `inventory()` is the list, and it is the thing to
     extend when a new store is added — not the purge code.
  2. **Report PER STORE, never in aggregate.** "Forgotten" is not a summary; it
     is a claim about each place the fact could live. Each store reports what
     was removed, what was rewritten, and what could not be reached.
  3. **Verify, then claim.** Every store is re-scanned after the purge. A store
     that still matches is reported as FAILED, however the write went. No store
     is described as purged on the strength of having attempted it.

Two categories are deliberately never purged, and both say so at request time
rather than being quietly counted as done:

  - **Claude Code and Codex transcripts.** These are harness records, not
    Allostat stores, and the operator's 10-year transcript retention rule
    governs them. Allostat does not reach into either harness's transcripts.
  - **The server purge bridge.** The server is RAM-only and returns a client
    purge instruction. If that bridge cannot be reached, the local purge still
    runs but the report remains incomplete rather than claiming success.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_state import atomic_write_text, jsonl_lock, utc_iso  # noqa: E402

# Stores that exist but are NOT Allostat's to purge. Named explicitly so they
# appear in every report rather than being silently absent from it.
OUT_OF_SCOPE_NOTE_TRANSCRIPTS = (
    "Claude Code and Codex session transcripts are harness records, not "
    "Allostat stores. Your 10-year transcript retention rule governs them. "
    "Allostat does not touch either harness's transcripts — purging those is "
    "a deliberate harness-level decision, never a side effect of this command."
)
UNREACHABLE_NOTE_SERVER_SILOS = (
    "The server purge bridge could not be reached or did not return a valid "
    "client purge instruction. Local stores were still processed, but the "
    "command will not claim a complete retraction without that bridge."
)

AUDIT_LOG_NAME = "retraction_log.jsonl"


@dataclass
class StoreResult:
    """What actually happened in one store."""

    store: str
    location: str
    matches_before: int = 0
    lines_removed: int = 0
    files_deleted: int = 0
    files_rewritten: int = 0
    matches_after: int = 0
    status: str = "clean"  # clean|purged|FAILED|unreachable|out_of_scope
    note: str = ""

    @property
    def honest_line(self) -> str:
        if self.status == "out_of_scope":
            return f"  [not Allostat's] {self.store}: {self.note}"
        if self.status == "unreachable":
            return f"  [CANNOT REACH]   {self.store}: {self.note}"
        if self.status == "clean":
            return f"  [nothing there]  {self.store}"
        if self.status == "FAILED":
            return (
                f"  [STILL PRESENT]  {self.store}: {self.matches_after} match(es) "
                f"remain after the purge — NOT forgotten. {self.note}".rstrip()
            )
        bits = []
        if self.lines_removed:
            bits.append(f"{self.lines_removed} line(s) removed")
        if self.files_deleted:
            bits.append(f"{self.files_deleted} file(s) deleted")
        if self.files_rewritten:
            bits.append(f"{self.files_rewritten} file(s) rewritten")
        return f"  [purged]         {self.store}: " + ", ".join(bits or ["done"])


@dataclass
class RetractionReport:
    pattern: str
    dry_run: bool
    regex: bool = False
    retraction_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stores: list[StoreResult] = field(default_factory=list)
    #: True when the retraction ran but could not write its own audit line.
    #: Surfaced, never swallowed: a deletion nobody can account for is the
    #: mirror image of the custody problem this module closes, so the operator
    #: is told the removal happened without a record of it.
    audit_write_failed: bool = False
    authorization_status: str = "unreachable"  # verified|unreachable
    authorization_note: str = UNREACHABLE_NOTE_SERVER_SILOS

    @property
    def fully_purged(self) -> bool:
        """True only when every reachable store verified clean.

        An unreachable store makes this False — that is the point. A retraction
        that could not reach a store has not forgotten the fact, whatever it
        managed elsewhere.
        """
        return self.authorization_status == "verified" and all(
            s.status in ("clean", "purged", "out_of_scope") for s in self.stores
        ) and not self.audit_write_failed

    def format(self) -> str:
        verb = "WOULD REMOVE" if self.dry_run else "REMOVED"
        lines = [
            f"=== Retraction: {self.pattern!r} ({verb}) ===",
            "",
        ]
        lines.extend(s.honest_line for s in self.stores)
        if self.authorization_status == "verified":
            lines.append(f"  [authorized]     server: {self.authorization_note}")
        else:
            lines.append(f"  [CANNOT AUTHORIZE] server: {self.authorization_note}")
        lines.append("")
        touched = sum(s.matches_before for s in self.stores)
        unreachable = [s for s in self.stores if s.status == "unreachable"]
        if self.dry_run:
            lines.append(f"{touched} match(es) across {len(self.stores)} store(s) inspected.")
            lines.append("Nothing was changed — this was a preview.")
            if unreachable or self.authorization_status != "verified":
                # Say it BEFORE the operator commits, not after. Learning that
                # a deletion was incomplete once it is already irreversible is
                # the worst possible order.
                lines.append(
                    "NOT fully forgotten even if you run this: "
                    + (
                        ", ".join(s.store for s in unreachable)
                        + " cannot be reached from here."
                        if unreachable
                        else "server authorization cannot be reached from here."
                    )
                )
        elif self.fully_purged:
            lines.append(
                "Every store Allostat can reach is verified clean. "
                "The stores listed above as out-of-scope are unchanged and are "
                "not Allostat's to change."
            )
        else:
            lines.append(
                "NOT fully forgotten. The stores marked above still hold it, or "
                "could not be reached. Do not treat this as a completed deletion."
            )
        if self.audit_write_failed:
            lines.append(
                "WARNING: the removal happened but its audit line could not be "
                f"written to {AUDIT_LOG_NAME}. This deletion is unrecorded."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# inventory — the list a retraction is honest about
# ---------------------------------------------------------------------------


def inventory(memory_dir: Path | None, state_dir: Path | None) -> list[tuple[str, Path, str]]:
    """Every store a remembered fact can live in.

    Returns (store_name, path, kind) where kind is
    "md" | "jsonl" | "json" | "gz".
    Extend THIS when a new store is added; the purge code is generic over it,
    so a store added here is purged and reported automatically, and a store
    NOT added here is the exact failure mode this module exists to prevent.
    """
    out: list[tuple[str, Path, str]] = []
    seen: set[Path] = set()

    def add(store: str, path: Path, kind: str) -> None:
        if path in seen or not path.is_file():
            return
        seen.add(path)
        out.append((store, path, kind))

    if memory_dir and memory_dir.is_dir():
        for p in sorted(memory_dir.rglob("*.md")):
            rel = p.relative_to(memory_dir)
            if p.name == "MEMORY.md":
                store = "memory index"
            elif "handoffs" in rel.parts:
                store = "session handoffs"
            elif "_processed" in rel.parts:
                store = "processed derivatives"
            elif any(
                part.startswith(("_archived_", "_LEGACY", "_PURGE", "_RETIRED"))
                for part in rel.parts
            ):
                store = "cold storage (archived memory)"
            else:
                store = "memory tree"
            add(store, p, "md")
        for p in sorted(memory_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.name.endswith(".jsonl.gz"):
                add("memory machinery archives", p, "gz")
            elif p.suffix == ".jsonl":
                add("memory machinery", p, "jsonl")
            elif p.suffix == ".json":
                add("memory machinery", p, "json")
            elif p.suffix == ".txt":
                add("memory machinery", p, "md")

    if state_dir and state_dir.is_dir():
        named = {
            "observations.jsonl": "observation log",
            "nudge_history.jsonl": "nudge history",
            "pending_dusk_surface.jsonl": "pending surfaces",
            "server_instructed_writes.jsonl": "server-instructed write log",
            "canonical_resolutions.jsonl": "canonical resolutions",
            AUDIT_LOG_NAME: "retraction audit",
        }
        for p in sorted(state_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(state_dir)
            if p.name.endswith(".jsonl.gz"):
                add("rotated archives", p, "gz")
                continue
            if p.suffix not in (".md", ".txt", ".jsonl", ".json"):
                continue
            if "silos" in rel.parts:
                store = f"local silo ({p.stem})"
            elif "audit" in rel.parts:
                store = "audit files"
            elif p.name.startswith("pending_dusk_surface_") and p.name.endswith(".surfaced.jsonl"):
                store = "surfaced dusk archives"
            else:
                store = named.get(p.name, f"local state ({rel.as_posix()})")
            kind = (
                "json" if p.suffix == ".json"
                else "jsonl" if p.suffix == ".jsonl"
                else "md"
            )
            add(store, p, kind)

    return out


def _harness_pointer_paths(memory_dir: Path | None) -> list[Path]:
    """The harness-side mirror of the index, if this project has one.

    `_root_project_memory` regenerates it as a thin pointer, but a pointer
    written before a retraction can still carry the retracted text.
    """
    if memory_dir is None:
        return []
    try:
        import local_state  # noqa: E402
        import session_handoff  # noqa: E402

        project_root = local_state.resolve_project_root(memory_dir) or memory_dir.parent
        harness = session_handoff._harness_memory_root(project_root)
    except Exception:  # noqa: BLE001
        return []
    if not harness or not Path(harness).is_dir():
        return []
    return [p for p in Path(harness).rglob("*.md") if p.is_file()]


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def _compile(pattern: str, *, regex: bool) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("empty retraction patterns are not allowed")
    rx = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE)
    if regex and _regex_minimum_width(rx) == 0:
        raise ValueError("zero-width retraction regexes are not allowed")
    return rx


def _regex_minimum_width(rx: re.Pattern[str]) -> int:
    """Return the regex parser's structural minimum consumed character count.

    Corpus probes cannot prove that a regex consumes input: a lookaround can be
    dormant for every probe yet become zero-width for a decoded JSON value.
    CPython's own parser computes the width used by the regex compiler, so use
    that structure before inventory or any effect. ``re._parser`` is present
    on supported Python versions; the compatibility import keeps older 3.x
    runtimes on the same parser contract.
    """
    try:
        parser = re._parser  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - older supported Python
        import sre_parse as parser  # type: ignore[no-redef,deprecated]
    return int(parser.parse(rx.pattern, rx.flags).getwidth()[0])


def _json_match_texts(value) -> list[str]:
    """Return exactly the decoded JSON values/keys the mutator can change."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _json_match_texts(item)]
    if isinstance(value, dict):
        texts: list[str] = []
        for key, item in value.items():
            texts.append(str(key))
            texts.extend(_json_match_texts(item))
        return texts
    return [json.dumps(value, ensure_ascii=False)]


def _target_texts(path: Path, kind: str) -> list[str]:
    if kind == "json":
        return _json_match_texts(json.loads(path.read_text(encoding="utf-8")))
    return [_target_text(path, kind)]


def _target_text(path: Path, kind: str) -> str:
    if kind == "gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
            return stream.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _reject_actual_zero_width_matches(
    rx: re.Pattern[str], targets: list[tuple[str, Path, str]]
) -> None:
    """Reject a regex that selects any actual store byte without consuming it."""
    for _store, path, kind in targets:
        if any(_has_zero_width_match(text, rx) for text in _target_texts(path, kind)):
            raise ValueError(
                "zero-width retraction regexes are not allowed "
                f"(actual match in {path})"
            )


def _has_zero_width_match(text: str, rx: re.Pattern[str]) -> bool:
    return any(match.start() == match.end() for match in rx.finditer(text))


def _refuse_zero_width_at_write(text: str, rx: re.Pattern[str]) -> None:
    """Close the preview/write race by checking the bytes being mutated."""
    if _has_zero_width_match(text, rx):
        raise ValueError("zero-width retraction regexes are not allowed")


def _refuse_zero_width_json_at_write(value, rx: re.Pattern[str]) -> None:
    if any(_has_zero_width_match(text, rx) for text in _json_match_texts(value)):
        raise ValueError("zero-width retraction regexes are not allowed")


def _count_matches_text(text: str, rx: re.Pattern[str]) -> int:
    return sum(1 for line in text.splitlines() if rx.search(line))


#: Files that are STRUCTURE, not content. Emptying one of these must never
#: delete it — `MEMORY.md` is the index every session reads and `_PURPOSE.md`
#: is scaffolding, so removing them because the last entry was retracted would
#: break the tree to honour a request about one fact. Caught by an end-to-end
#: smoke test: retracting the only project deleted the operator's index.
_NEVER_DELETED = frozenset({"MEMORY.md", "_PURPOSE.md", "README.md"})


def _purge_text_file(path: Path, rx: re.Pattern[str], *, dry_run: bool) -> tuple[int, bool, bool]:
    """Drop matching lines. Returns (lines_removed, file_deleted, rewritten).

    A leaf left with nothing but frontmatter and whitespace is deleted: the
    remains of a retracted memory are not a memory, and leaving a hollow file
    behind is how a "deleted" fact stays discoverable by name. Structural files
    (`_NEVER_DELETED`) are rewritten instead — emptied, but still there.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    _refuse_zero_width_at_write(text, rx)
    lines = text.splitlines()
    kept = [ln for ln in lines if not rx.search(ln)]
    removed = len(lines) - len(kept)
    if removed == 0:
        return 0, False, False
    if dry_run:
        return removed, False, False

    remainder = "\n".join(kept)
    if _is_hollow(remainder) and path.name not in _NEVER_DELETED:
        path.unlink()
        return removed, True, False
    atomic_write_text(
        path,
        remainder + ("\n" if text.endswith("\n") else ""),
        encoding="utf-8",
    )
    return removed, False, True


def _is_hollow(text: str) -> bool:
    """True when what's left is frontmatter, headings and whitespace only."""
    body = re.sub(r"(?s)\A---\n.*?\n---\n", "", text)
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return False
    return True


def _purge_jsonl(path: Path, rx: re.Pattern[str], *, dry_run: bool) -> tuple[int, bool]:
    """Drop matching JSONL rows. Returns (rows_removed, rewritten).

    Matching is on the raw line, so a fact reaches this whether it sits in a
    payload field, a nested detail, or a free-text excerpt. Taken under the
    same lock the appenders hold.
    """
    with jsonl_lock(path):
        raw = path.read_text(encoding="utf-8", errors="replace")
        _refuse_zero_width_at_write(raw, rx)
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        kept = [ln for ln in lines if not rx.search(ln)]
        removed = len(lines) - len(kept)
        if removed == 0 or dry_run:
            return removed, False
        atomic_write_text(
            path,
            "\n".join(kept) + ("\n" if kept else ""),
            encoding="utf-8",
        )
        return removed, True


def _purge_json_value(value, rx: re.Pattern[str]):
    """Remove matching content while retaining a valid JSON value shape."""
    if isinstance(value, str):
        return rx.subn("", value)
    if isinstance(value, list):
        removed = 0
        rewritten = []
        for item in value:
            new_item, count = _purge_json_value(item, rx)
            removed += count
            rewritten.append(new_item)
        return rewritten, removed
    if isinstance(value, dict):
        removed = 0
        rewritten = {}
        for key, item in value.items():
            if rx.search(str(key)):
                removed += 1
                continue
            new_item, count = _purge_json_value(item, rx)
            removed += count
            rewritten[key] = new_item
        return rewritten, removed
    encoded = json.dumps(value, ensure_ascii=False)
    if rx.search(encoded):
        return None, 1
    return value, 0


def _purge_json(path: Path, rx: re.Pattern[str], *, dry_run: bool) -> tuple[int, bool]:
    """Semantically rewrite structured JSON; never treat it as JSONL."""
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    _refuse_zero_width_json_at_write(value, rx)
    rewritten, removed = _purge_json_value(value, rx)
    if removed == 0 or dry_run:
        return removed, False
    atomic_write_text(
        path,
        json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return removed, True


def _purge_gz(path: Path, rx: re.Pattern[str], *, dry_run: bool) -> tuple[int, bool]:
    """Same, for a rotated gzip archive."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    _refuse_zero_width_at_write(raw, rx)
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    kept = [ln for ln in lines if not rx.search(ln)]
    removed = len(lines) - len(kept)
    if removed == 0 or dry_run:
        return removed, False
    tmp = path.with_name(f"{path.name}.{os.getpid()}.allostat-tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            # Best-effort: preserve the original gzip write/replace exception;
            # a failed temporary-file cleanup must not hide its root cause.
            pass
        raise
    return removed, True


def _scan_after(path: Path, kind: str, rx: re.Pattern[str]) -> int:
    """Re-read a store and count what survived. This is what turns an attempt
    into a claim."""
    if not path.exists():
        return 0
    if kind == "gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return _count_matches_text(f.read(), rx)
    if kind == "json":
        # Parsing is part of verification. A corrupt structured store is a
        # failed read, never evidence that the fact disappeared.
        value = json.loads(path.read_text(encoding="utf-8"))
        return sum(_count_matches_text(text, rx) for text in _json_match_texts(value))
    text = path.read_text(encoding="utf-8", errors="replace")
    return _count_matches_text(text, rx)


def retract(
    pattern: str,
    *,
    memory_dir: Path | None,
    state_dir: Path | None,
    dry_run: bool = True,
    regex: bool = False,
    server_purge_available: bool = False,
    server_purge_note: str = "",
) -> RetractionReport:
    """Purge `pattern` from every reachable store and report per store.

    `dry_run=True` (the default) inspects and reports without changing
    anything — a retraction should be previewable, because the operator is
    entitled to see the blast radius of a deletion before it happens.
    """
    rx = _compile(pattern, regex=regex)
    targets = inventory(memory_dir, state_dir)
    targets += [("harness index mirror", p, "md") for p in _harness_pointer_paths(memory_dir)]
    if regex:
        _reject_actual_zero_width_matches(rx, targets)
    report = RetractionReport(
        pattern=pattern,
        dry_run=dry_run,
        regex=regex,
        authorization_status="verified" if server_purge_available else "unreachable",
        authorization_note=(
            server_purge_note
            or (
                "server authorized the local purge"
                if server_purge_available
                else UNREACHABLE_NOTE_SERVER_SILOS
            )
        ),
    )

    grouped: dict[str, StoreResult] = {}

    def _result(store: str, location: str) -> StoreResult:
        if store not in grouped:
            grouped[store] = StoreResult(store=store, location=location)
            report.stores.append(grouped[store])
        return grouped[store]

    for store, path, kind in targets:
        res = _result(store, str(path.parent))
        try:
            before = _scan_after(path, kind, rx)
        except (OSError, UnicodeError, json.JSONDecodeError, EOFError) as exc:
            res.status = "FAILED"
            res.note = f"read_failed:{type(exc).__name__}"
            continue
        if before == 0:
            continue
        res.matches_before += before
        try:
            if kind == "md":
                removed, deleted, rewritten = _purge_text_file(path, rx, dry_run=dry_run)
                res.lines_removed += removed
                res.files_deleted += int(deleted)
                res.files_rewritten += int(rewritten)
            elif kind == "gz":
                removed, rewritten = _purge_gz(path, rx, dry_run=dry_run)
                res.lines_removed += removed
                res.files_rewritten += int(rewritten)
            elif kind == "json":
                removed, rewritten = _purge_json(path, rx, dry_run=dry_run)
                res.lines_removed += removed
                res.files_rewritten += int(rewritten)
            else:
                removed, rewritten = _purge_jsonl(path, rx, dry_run=dry_run)
                res.lines_removed += removed
                res.files_rewritten += int(rewritten)
        except (OSError, UnicodeError, json.JSONDecodeError, EOFError) as exc:
            res.status = "FAILED"
            res.note = f"write_failed:{type(exc).__name__}"
            res.matches_after += before
            continue
        try:
            res.matches_after += _scan_after(path, kind, rx)
        except (OSError, UnicodeError, json.JSONDecodeError, EOFError) as exc:
            res.status = "FAILED"
            res.note = f"verification_read_failed:{type(exc).__name__}"
            res.matches_after += before

    for res in report.stores:
        if res.status == "FAILED":
            continue
        if res.matches_before == 0:
            res.status = "clean"
        elif dry_run:
            res.status = "purged"  # preview: what WOULD be removed
        elif res.matches_after == 0:
            res.status = "purged"
        else:
            res.status = "FAILED"

    # Harness transcripts are named because they exist but are not Allostat's.
    report.stores.append(StoreResult(
        store="harness transcripts",
        location="Claude Code and Codex transcript stores",
        status="out_of_scope",
        note=OUT_OF_SCOPE_NOTE_TRANSCRIPTS,
    ))
    if not dry_run:
        report.audit_write_failed = not _append_audit(state_dir, report)
    return report


def _append_audit(state_dir: Path | None, report: RetractionReport) -> bool:
    """Record the retraction itself.

    A deletion with no record of who deleted what, when, is unaccountable — and
    an unaccountable deletion is its own custody problem. The record uses a
    random operation identifier plus safe counts; it never re-persists the
    literal or a reversible derivative of it.
    """
    if state_dir is None:
        return False
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / AUDIT_LOG_NAME
        entry = {
            "timestamp": utc_iso(),
            "action": "retraction",
            "retraction_id": report.retraction_id,
            "pattern_type": "regex" if report.regex else "literal",
            "fully_purged": report.fully_purged,
            "authorization": {
                "status": report.authorization_status,
                "note": report.authorization_note,
            },
            "stores": [
                {
                    "store": s.store,
                    "status": s.status,
                    "matches_before": s.matches_before,
                    "lines_removed": s.lines_removed,
                    "files_deleted": s.files_deleted,
                    "matches_after": s.matches_after,
                }
                for s in report.stores
            ],
        }
        with jsonl_lock(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return True
    except OSError:
        # NOT silenced: the caller sets `audit_write_failed` and the report
        # tells the operator the removal happened with no record of it. An
        # unaccountable deletion is the mirror image of the custody problem
        # this module exists to close, so it is surfaced rather than swallowed.
        return False


def _bridge_server_purges(
    pattern: str,
    *,
    state_dir: Path,
    dry_run: bool,
    regex: bool,
    harness: str = "claude",
) -> tuple[bool, str]:
    """Request and validate server authorization without touching local bytes.

    The server remains RAM-only: it authorizes and composes a client-state
    instruction, while the central retraction transaction owns, inventories,
    applies, accounts for, and verifies every local byte. A malformed response is a hard
    incompleteness signal, not a reason to skip the local purge.
    """
    silos = state_dir / "silos"
    classes = sorted({
        path.stem
        for pattern_glob in ("*.jsonl", "*.json")
        for path in silos.glob(pattern_glob)
        if path.is_file()
    }) if silos.is_dir() else []
    if not classes:
        return True, "no local silo classes required a server purge instruction"

    try:
        from mcp_client import MCPClient

        client = MCPClient(harness=harness)
        for silo_class in classes:
            response = client.call_tool(
                "recall_silos_query",
                {
                    "excerpt": {},
                    "question": {
                        "pillar": "recall-silos",
                        "question_type": "purge_silo",
                        "details": {
                            "silo_class": silo_class,
                            "pattern": pattern,
                            "regex": regex,
                        },
                    },
                },
            )
            decision = response.get("decision") if isinstance(response, dict) else None
            writes = decision.get("client_state_writes") if isinstance(decision, dict) else None
            if not isinstance(decision, dict) or decision.get("action") != "purge_silo":
                return False, f"server returned no purge decision for {silo_class}"
            if not isinstance(writes, list) or not writes:
                return False, f"server returned no purge instruction for {silo_class}"
            expected = {
                "silo_class": silo_class,
                "pattern": pattern,
                "regex": regex,
            }
            if len(writes) != 1:
                return False, f"server returned ambiguous purge instructions for {silo_class}"
            write = writes[0]
            if not isinstance(write, dict) or any(
                (
                    write.get("target") != "silo_entry.jsonl",
                    write.get("mode") != "purge",
                    write.get("content") != expected,
                )
            ):
                return False, f"server returned mismatched purge instruction for {silo_class}"
    except Exception as exc:  # noqa: BLE001 — translated into an honest report
        return False, f"server purge bridge failed: {type(exc).__name__}"
    return True, "server authorized the central local silo purge"


# ---------------------------------------------------------------------------
# CLI (/allostat-forget)
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="allostat-forget")
    parser.add_argument("pattern", help="text to retract (literal unless --regex)")
    parser.add_argument("--execute", action="store_true",
                        help="actually purge; default is a preview")
    parser.add_argument("--regex", action="store_true")
    parser.add_argument("--memory-dir")
    parser.add_argument("--state-dir")
    parser.add_argument("--harness", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)

    import local_state  # noqa: E402
    import memory_lifecycle  # noqa: E402

    memory_dir = Path(args.memory_dir) if args.memory_dir else memory_lifecycle.resolve_memory_dir()
    state_dir = Path(args.state_dir) if args.state_dir else local_state.resolve_state_dir()

    # Inventory and preview every target before the network request. This is
    # also where corpus-specific zero-width regexes are refused, before any
    # bridge or write can occur.
    try:
        preview = retract(
            args.pattern,
            memory_dir=memory_dir,
            state_dir=state_dir,
            dry_run=True,
            regex=args.regex,
        )
    except (ValueError, re.error, OSError, UnicodeError, EOFError) as exc:
        parser.error(str(exc))

    server_ok, server_note = _bridge_server_purges(
        args.pattern,
        state_dir=state_dir,
        dry_run=not args.execute,
        regex=args.regex,
        harness=args.harness,
    )

    if args.execute:
        report = retract(
            args.pattern,
            memory_dir=memory_dir,
            state_dir=state_dir,
            dry_run=False,
            regex=args.regex,
            server_purge_available=server_ok,
            server_purge_note=server_note,
        )
    else:
        report = preview
        report.authorization_status = "verified" if server_ok else "unreachable"
        report.authorization_note = server_note
    print(report.format())
    return 0 if (report.dry_run or report.fully_purged) else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
