"""Filesystem-context detection for PreToolUse innate-rule dispatch.

Computes the boolean context flags that rule 04 (canonical-verification)
requires:

  - file_being_edited_has_sibling_folders_with_similar_files
  - no_CANONICAL_txt_in_current_folder_or_any_sibling

The server's innate-enforcer pillar has no filesystem visibility into the
operator's machine — it only sees what the wrapper sends in `question.details`.
This module is the wrapper's filesystem-side computation, kept narrow so
the server stays a pure decision function. Closes Finding #8 (sprint
2026-05-15) by making rule 04's match_all trigger reachable.

Privacy: only the boolean flags + a list of sibling FOLDER NAMES (not
paths) are sent to the server. The full file path stays on the operator's
machine (the server already sees it because it arrives as a tool-call
attribute, but we don't broaden disclosure here).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CanonicalContext:
    """Result of canonical-folder detection for a file path.

    - `flags`: subset of rule 04's context-key vocabulary that holds true.
    - `sibling_folder_names`: folder names (not paths) of the file's
       parent dir + any sibling dirs that contain a same-named file.
       Used to populate rule 04's `{sibling_list}` template placeholder.
    """

    flags: frozenset[str]
    sibling_folder_names: tuple[str, ...]


CANONICAL_MARKER = "CANONICAL.txt"


def detect_canonical_context(file_path: str | None) -> CanonicalContext:
    """Inspect the filesystem around `file_path` and return rule 04 flags.

    Returns an empty CanonicalContext if `file_path` is missing, doesn't
    resolve to a real file/directory, or has no siblings to compare against.
    Treats all filesystem errors (permission, broken junction, missing
    parent) as "no canonical concern" — silent skip is safer than a
    false-positive red box on every Edit.

    Detection rule:
      1. Identify the file's parent directory (the "current" folder).
      2. Identify sibling directories of the current folder.
      3. A sibling is "similar" if it contains a file with the same basename
         as `file_path`. This catches the rollout pattern
         (`site/index.html` + `site_v2/index.html` + `site_old/index.html`)
         and avoids false positives on unrelated sibling folders.
      4. If at least one similar sibling exists, the first context flag is
         set.
      5. If neither the current folder nor any similar sibling holds a
         `CANONICAL.txt` marker, the second flag is set.

    The two flags are independent. Rule 04 only fires when BOTH are set
    (match_all semantics on the server side).
    """
    if not file_path:
        return CanonicalContext(frozenset(), ())

    try:
        target = Path(file_path)
    except (TypeError, ValueError):
        return CanonicalContext(frozenset(), ())

    current_dir = target.parent
    basename = target.name
    if not basename or not current_dir or current_dir == current_dir.parent:
        return CanonicalContext(frozenset(), ())

    try:
        if not current_dir.is_dir():
            return CanonicalContext(frozenset(), ())
    except OSError:
        return CanonicalContext(frozenset(), ())

    parent_of_current = current_dir.parent
    try:
        if not parent_of_current.is_dir():
            return CanonicalContext(frozenset(), ())
        siblings = [
            d
            for d in parent_of_current.iterdir()
            if _safe_is_dir(d) and d.name != current_dir.name
        ]
    except OSError:
        return CanonicalContext(frozenset(), ())

    similar_siblings: list[Path] = []
    for sibling in siblings:
        candidate = sibling / basename
        if _safe_is_file(candidate):
            similar_siblings.append(sibling)

    flags: set[str] = set()
    sibling_folder_names: list[str] = []

    if similar_siblings:
        flags.add("file_being_edited_has_sibling_folders_with_similar_files")
        sibling_folder_names = [current_dir.name] + [s.name for s in similar_siblings]

        folders_to_check = [current_dir, *similar_siblings]
        canonical_present = any(
            _safe_is_file(folder / CANONICAL_MARKER) for folder in folders_to_check
        )
        if not canonical_present:
            flags.add("no_CANONICAL_txt_in_current_folder_or_any_sibling")

    return CanonicalContext(
        flags=frozenset(flags),
        sibling_folder_names=tuple(sibling_folder_names),
    )


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False
