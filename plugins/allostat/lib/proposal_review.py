"""Proposal-review reader — terminal half of the pattern-observer loop.

The server-side `pattern_observer` pillar detects N=4 cross-session and
N=2 in-session behavior patterns and returns `ClientStateWrite`
instructions. `client_state_writer.apply_writes` appends each to the
operator's `<state_dir>/pending_proposals.jsonl` (append-only). This
module is the consumer of that file: it loads the queued proposals,
presents them for review, and records the operator's decision.

Why a sidecar for resolution state
-----------------------------------
`pending_proposals.jsonl` is APPEND-ONLY by contract (the whitelist in
client_state_writer pins `mode="append"`, and the server can re-emit a
proposal on a later session). Rewriting it to flip a "resolved" flag
would (a) violate the append-only invariant other code relies on and
(b) race with a concurrent server-instructed append. So resolution state
lives in a companion `<state_dir>/proposal_resolutions.json` keyed by the
proposal `hash`. A proposal is "pending" iff its hash is absent from the
sidecar. This keeps the event log immutable and the decision log mutable,
each in its natural shape.

Proposal record schema (one JSON object per line in pending_proposals.jsonl),
as written by allostat_server/pillars/pattern_observer.py::
  {
    "timestamp": "<utc iso>",
    "kind": "n5_proposal" | "in_session_override",
    "n_threshold": int,
    "hash": "<16-hex>",                 # == proposal.hash; dedup key
    "current_session_id": "...",        # in_session_override only
    "proposal": {                       # inner dict the writer consumes
      "fingerprint": "class:subject:direction",
      "hash": "<16-hex>",
      "class": "...", "subject": "...", "direction": "...",
      "occurrence_count": int,
      "first_seen": "...", "last_seen": "...",
      "sessions_spanning": int,
      "suggested_rule_wording": "...",
      "flags": [...]                    # in_session_override only
    }
  }

Public API
----------
  load_pending_proposals(state_dir)            -> list[PendingProposal]
      Every proposal in the log (deduped by hash, latest line wins),
      each tagged with its current resolution status.
  list_pending(state_dir)                      -> list[PendingProposal]
      Only the ones still awaiting a decision (status == "pending").
  count_pending(state_dir)                     -> int
      Cheap count for the session-start nudge.
  get_proposal(state_dir, hash_)               -> PendingProposal | None
  mark_resolved(state_dir, hash_, status, ...) -> bool
      Record approved | rejected | deferred in the sidecar.

This module is filesystem-only — no network, no MCP layer.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_state import (  # noqa: E402
    resolve_project_root,
    resolve_state_dir,
)

PENDING_PROPOSALS_FILE = "pending_proposals.jsonl"
RESOLUTIONS_FILE = "proposal_resolutions.json"

# Decision a proposal can land in. "pending" is the implicit state of any
# proposal with no sidecar entry; the other three are operator decisions.
VALID_STATUSES = frozenset({"approved", "rejected", "deferred"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PendingProposal:
    """Normalized view of one pending-proposal record.

    Flattens the server's `{kind, hash, proposal: {...}}` wrapper into a
    single object the command + writer can both consume, while preserving
    the raw inner dict (`raw_proposal`) so `to_proposal_dict()` hands the
    writer exactly what the server produced.
    """

    hash: str
    kind: str
    class_: str
    subject: str
    direction: str
    suggested_rule_wording: str
    occurrence_count: int = 0
    sessions_spanning: int = 0
    first_seen: str = ""
    last_seen: str = ""
    n_threshold: int = 0
    flags: list[str] = field(default_factory=list)
    current_session_id: str | None = None
    timestamp: str = ""
    status: str = "pending"
    raw_proposal: dict[str, Any] = field(default_factory=dict)

    @property
    def is_fast_promotion(self) -> bool:
        """True for the N=2 in-session-override fast lane."""
        return self.kind == "in_session_override" or (
            "fast-promotion-lane" in self.flags
        )

    @property
    def subject_human(self) -> str:
        """Subject with hyphens turned back into spaces, for display."""
        return self.subject.replace("-", " ")

    def to_proposal_dict(self) -> dict[str, Any]:
        """Return the inner proposal dict for learned_rule_writer.

        Prefers the raw inner dict the server emitted (so no field is lost
        in normalization); falls back to a reconstructed dict if the record
        carried no nested `proposal` object.
        """
        if self.raw_proposal:
            return dict(self.raw_proposal)
        return {
            "hash": self.hash,
            "class": self.class_,
            "subject": self.subject,
            "direction": self.direction,
            "occurrence_count": self.occurrence_count,
            "sessions_spanning": self.sessions_spanning,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "suggested_rule_wording": self.suggested_rule_wording,
            "flags": list(self.flags),
        }


# ---------- internal parse helpers ----------


def _record_to_proposal(record: dict[str, Any]) -> PendingProposal | None:
    """Map one raw JSONL record to a PendingProposal, or None if unusable.

    A record is unusable when it lacks a `hash` (the dedup + resolution
    key). The inner `proposal` dict is the source of truth for the pattern
    fields; top-level fields supply the wrapper metadata (kind, threshold,
    session).
    """
    if not isinstance(record, dict):
        return None
    inner = record.get("proposal")
    if not isinstance(inner, dict):
        inner = {}
    hash_ = record.get("hash") or inner.get("hash")
    if not hash_:
        return None
    flags = inner.get("flags") or []
    if not isinstance(flags, list):
        flags = []
    return PendingProposal(
        hash=str(hash_),
        kind=str(record.get("kind") or "n5_proposal"),
        class_=str(inner.get("class") or ""),
        subject=str(inner.get("subject") or ""),
        direction=str(inner.get("direction") or ""),
        suggested_rule_wording=str(inner.get("suggested_rule_wording") or ""),
        occurrence_count=int(inner.get("occurrence_count") or 0),
        sessions_spanning=int(inner.get("sessions_spanning") or 0),
        first_seen=str(inner.get("first_seen") or ""),
        last_seen=str(inner.get("last_seen") or ""),
        n_threshold=int(record.get("n_threshold") or 0),
        flags=[str(f) for f in flags],
        current_session_id=record.get("current_session_id"),
        timestamp=str(record.get("timestamp") or ""),
        raw_proposal=dict(inner),
    )


def _iter_records(state_dir: Path):
    """Yield parsed JSONL records from pending_proposals.jsonl.

    Malformed lines + non-dict records are silently skipped (the log is
    operator-local and append-only; a single bad line must not blind the
    reader to the rest).
    """
    path = state_dir / PENDING_PROPOSALS_FILE
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _read_resolutions(state_dir: Path) -> dict[str, Any]:
    """Parse the resolution sidecar; {} on missing/unparseable."""
    path = state_dir / RESOLUTIONS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_resolutions(state_dir: Path, data: dict[str, Any]) -> bool:
    """Atomically overwrite the resolution sidecar. Best-effort."""
    path = state_dir / RESOLUTIONS_FILE
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


# ---------- public API ----------


def load_pending_proposals(state_dir: Path) -> list[PendingProposal]:
    """Load every proposal from the log, deduped by hash (latest wins),
    each tagged with its current resolution status.

    Order is by first appearance in the log so the operator reviews them
    in the order they were detected.
    """
    by_hash: dict[str, PendingProposal] = {}
    order: list[str] = []
    for record in _iter_records(state_dir):
        p = _record_to_proposal(record)
        if p is None:
            continue
        if p.hash not in by_hash:
            order.append(p.hash)
        # Latest line for a hash wins (server may re-emit with higher count).
        by_hash[p.hash] = p

    resolutions = _read_resolutions(state_dir)
    out: list[PendingProposal] = []
    for h in order:
        p = by_hash[h]
        entry = resolutions.get(h)
        if isinstance(entry, dict) and entry.get("status"):
            p.status = str(entry["status"])
        out.append(p)
    return out


def list_pending(state_dir: Path) -> list[PendingProposal]:
    """Proposals still awaiting an operator decision (status == pending)."""
    return [p for p in load_pending_proposals(state_dir) if p.status == "pending"]


def count_pending(state_dir: Path) -> int:
    """Cheap count of still-pending proposals — powers the session nudge."""
    return len(list_pending(state_dir))


def get_proposal(state_dir: Path, hash_: str) -> PendingProposal | None:
    """Return the proposal with the given hash, or None.

    Accepts a full hash (exact match) OR an unambiguous prefix — the
    `--list` view shows 8-char prefixes, so the operator can resolve by
    what they see. An exact full-hash match always wins; a prefix that
    matches more than one proposal returns None (ambiguous, refuse to
    guess).
    """
    proposals = load_pending_proposals(state_dir)
    for p in proposals:
        if p.hash == hash_:
            return p
    prefix_matches = [p for p in proposals if p.hash.startswith(hash_)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def mark_resolved(
    state_dir: Path,
    hash_: str,
    *,
    status: str,
    note: str | None = None,
    learned_rule_path: str | None = None,
) -> bool:
    """Record an operator decision for one proposal in the sidecar.

    `status` must be one of VALID_STATUSES. Idempotent: re-resolving the
    same hash overwrites with the latest decision. Never touches the
    append-only pending_proposals.jsonl.

    `learned_rule_path` lets the promote flow record where an approved
    proposal was persisted (set by the command after learned_rule_writer
    runs), so the audit trail links decision → artifact.

    Returns True on success, False on filesystem error.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    # Resolve a prefix to the full hash so the sidecar is always keyed by the
    # canonical hash (load_pending_proposals matches resolution by full hash).
    matched = get_proposal(state_dir, hash_)
    if matched is not None:
        hash_ = matched.hash
    resolutions = _read_resolutions(state_dir)
    entry: dict[str, Any] = {
        "status": status,
        "resolved_at": _utc_now_iso(),
    }
    if note:
        entry["note"] = note
    if learned_rule_path:
        entry["learned_rule_path"] = learned_rule_path
    resolutions[hash_] = entry
    return _write_resolutions(state_dir, resolutions)


# ---------- formatting (operator-facing) ----------


def format_pending_line(index: int, p: PendingProposal) -> str:
    """One-line summary of a pending proposal for the --list view."""
    lane = "⚡fast" if p.is_fast_promotion else f"N={p.n_threshold or p.occurrence_count}"
    span = (
        f"{p.sessions_spanning} session(s)"
        if p.sessions_spanning
        else "this session"
    )
    return (
        f"  [{index}] {p.hash[:8]}  ({lane}, {p.occurrence_count}× over {span})\n"
        f"        class={p.class_}  pattern=\"{p.subject_human}\"  dir={p.direction}\n"
        f"        rule: {p.suggested_rule_wording}"
    )


def format_pending_block(proposals: list[PendingProposal]) -> str:
    """Multi-proposal block for the command's review step."""
    if not proposals:
        return "No pending proposals to review."
    lines = [f"{len(proposals)} pending proposal(s):", ""]
    for i, p in enumerate(proposals, start=1):
        lines.append(format_pending_line(i, p))
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------- CLI ----------


def _cli(argv: list[str] | None = None) -> int:
    """python "$ALLOSTAT_PLUGIN_DIR/lib/proposal_review.py" [--count|--list|--resolve HASH --status S]

    --count            print the number of pending proposals (for the nudge)
    --list             print pending proposals for operator review (default)
    --resolve HASH     mark one proposal resolved; requires --status
    --status STATUS    approved | rejected | deferred (with --resolve)
    --note TEXT        optional note stored with the resolution

    Resolves the active project's state dir from cwd.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="proposal_review")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--count", action="store_true", help="print pending count")
    group.add_argument("--list", action="store_true", help="list pending proposals")
    group.add_argument("--resolve", metavar="HASH", help="mark a proposal resolved")
    parser.add_argument(
        "--status",
        choices=sorted(VALID_STATUSES),
        help="resolution status (with --resolve)",
    )
    parser.add_argument("--note", default=None, help="note stored with resolution")
    parser.add_argument(
        "--learned-rule-path",
        default=None,
        help="path to the persisted learned rule (with --resolve --status approved)",
    )
    args = parser.parse_args(argv)

    project_root = resolve_project_root() or Path.cwd()
    state_dir = resolve_state_dir(project_root)

    if args.count:
        print(count_pending(state_dir))
        return 0

    if args.resolve:
        if not args.status:
            print("ERROR: --resolve requires --status", file=sys.stderr)
            return 2
        if get_proposal(state_dir, args.resolve) is None:
            print(
                f"ERROR: no proposal with hash {args.resolve!r} in {state_dir}",
                file=sys.stderr,
            )
            return 1
        ok = mark_resolved(
            state_dir,
            args.resolve,
            status=args.status,
            note=args.note,
            learned_rule_path=args.learned_rule_path,
        )
        if ok:
            print(f"proposal {args.resolve[:8]} -> {args.status}")
            return 0
        print(
            f"ERROR: could not write resolution to {state_dir} (filesystem error)",
            file=sys.stderr,
        )
        return 1

    # Default + explicit --list: show pending proposals.
    pending = list_pending(state_dir)
    if not pending:
        print("No pending proposals.")
        return 0
    print(format_pending_block(pending))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
