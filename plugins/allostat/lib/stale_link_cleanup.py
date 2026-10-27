"""Allostat stale link cleanup — PATCH-139 / v1.0.0 Slice 9.

Walks operator-tracked filesystem roots for broken symlinks + junctions.
Surfaces broken links for cleanup via /allostat-tend --check-symlinks.

Specifically addresses operator's documented Downloads-junction failure
pattern (junction breaking silently has happened multiple times per
operator's CLAUDE.md notes).

Ported from the v2.3 plugin's stale_link_cleanup module for v2.4 wrapper.
"""
from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StaleLink:
    link_path: str
    target_path: str
    reason: str  # "target_missing" | "permission" | "loop"


def _is_reparse_link(p: Path) -> bool:
    """True if p is a symlink OR a Windows directory junction.

    Path.is_symlink() returns False for a junction (reparse tag
    IO_REPARSE_TAG_MOUNT_POINT, `mklink /J`), so gating detection on
    is_symlink() alone made the operator's documented Downloads-junction
    failure structurally undetectable (ultraswarm HIGH). Junctions carry the
    FILE_ATTRIBUTE_REPARSE_POINT flag, which lstat() exposes on Windows.
    """
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    try:
        st = p.lstat()
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attrs and reparse_flag and (attrs & reparse_flag))


def _check_link(p: Path) -> StaleLink | None:
    """Check one path. Return StaleLink if broken, None if healthy."""
    if not _is_reparse_link(p):
        return None
    try:
        target = str(p.readlink())
    except OSError:
        # Junctions aren't always readlink-able; keep checking broken-ness via
        # resolve() rather than bailing (a dangling junction must still surface).
        target = "?"
    try:
        target_resolved = p.resolve(strict=False)
        if not target_resolved.exists():
            return StaleLink(
                link_path=str(p),
                target_path=target,
                reason="target_missing",
            )
    except OSError as e:
        return StaleLink(link_path=str(p), target_path=target, reason=f"resolve_failed: {e}")
    return None


def scan_for_stale_links(roots: list[Path], max_depth: int = 6) -> list[StaleLink]:
    """Walk roots, return list of stale links found.

    max_depth caps the walk to prevent runaway scans on deep trees.
    Operator's actual filesystem rarely needs deeper than 6 levels for
    the kinds of links that break (junctions in profile dirs, plugin
    symlinks, etc.).
    """
    found: list[StaleLink] = []
    for root in roots:
        # Check the ROOT ITSELF before descending (ultraswarm follow-up
        # 2026-07-20). `_walk` only ever inspects a root's CHILDREN, and the
        # old `if not root.exists(): continue` early-out discarded exactly the
        # failure this module exists for: Path.exists() FOLLOWS a reparse
        # point, so a dangling ~/Downloads junction reads as "absent" and was
        # skipped. Absence-of-target must not read as absence-of-problem.
        root_link = _check_link(root)
        if root_link is not None:
            found.append(root_link)
            # Do NOT descend through a broken reparse point.
            continue
        if not root.exists():
            continue
        try:
            _walk(root, max_depth, found)
        except OSError:
            continue
    return found


def _walk(root: Path, depth: int, accumulator: list[StaleLink]) -> None:
    if depth < 0:
        return
    try:
        for entry in root.iterdir():
            try:
                if _is_reparse_link(entry):
                    # Symlink OR junction: check it, and do NOT descend through
                    # it (avoids following/loop-walking a reparse point).
                    result = _check_link(entry)
                    if result is not None:
                        accumulator.append(result)
                elif entry.is_dir():
                    _walk(entry, depth - 1, accumulator)
            except OSError:
                continue
    except OSError:
        return


def default_scan_roots(home: Path | None = None) -> list[Path]:
    """Operator's commonly-broken-link roots: ~/.claude/, ~/Downloads (junction),
    plugin install dirs.

    Existence-agnostic (ultraswarm follow-up 2026-07-20): a candidate is kept
    when it exists OR when it is itself a reparse point. `Path.exists()` follows
    a junction, so a BROKEN ~/Downloads junction returns False and used to be
    filtered out of the scan list entirely — `--check-symlinks` then reported
    "No stale symlinks detected" at the exact moment the documented failure
    occurred. A candidate that is genuinely absent (not a link) is still
    dropped; only reparse points survive the existence filter.
    """
    if home is None:
        home = Path.home()
    candidates = [
        home / ".claude",
        home / "Downloads",  # often a junction on operator's Windows setup
    ]
    return [c for c in candidates if c.exists() or _is_reparse_link(c)]


def format_stale_links_report(stale: list[StaleLink]) -> str:
    """Operator-facing report for /allostat-tend --check-symlinks."""
    if not stale:
        return "No stale symlinks detected in default scan roots."
    lines = [f"=== Stale link cleanup — {len(stale)} broken link(s) ==="]
    for s in stale[:20]:
        lines.append("")
        lines.append(f"  {s.link_path}")
        lines.append(f"    target: {s.target_path}")
        lines.append(f"    reason: {s.reason}")
    if len(stale) > 20:
        lines.append("")
        lines.append(f"... + {len(stale) - 20} more")
    lines.append("")
    lines.append("Operator decides per-link: re-target, remove, or ignore.")
    return "\n".join(lines)
