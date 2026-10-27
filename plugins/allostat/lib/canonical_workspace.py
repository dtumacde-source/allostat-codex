"""Canonical-workspace path classifier — rule innate-12.

Rule 12 (canonical-workspace-write-nudge) hard-pauses when a *strategic
deliverable* is about to be written to the operator's Downloads folder
while the active project has a *canonical workspace* declared. Strategic
deliverables (Phase docs, market analyses, strategy memos, product
decisions) belong in the project's source-of-truth workspace, not in
Downloads where they go stale or get edited as a phantom canonical copy.

This module is the wrapper's filesystem-side computation. The server has
no visibility into the operator's machine, so all path logic lives here.
It mirrors the idiom of `filesystem_context.py` (rule 04's analog):
  - a frozen result dataclass,
  - narrow filesystem inspection,
  - all filesystem errors treated as "no concern" → silent safe default
    (never raise; a hook must not crash the operator's session),
  - GENERIC paths only — no operator-specific paths (the server-side
    privacy invariant + the rule's `scope: universal`-by-construction
    intent).

Public API (consumed by the rule-12 producer wired in the main session):

  - is_downloads_path(path)         -> bool   # any-drive Downloads segment
  - is_drift_location(path)         -> bool   # Downloads / Desktop / Documents
  - find_canonical_workspace(path)  -> str | None
  - classify_write_target(path, content=None) -> bool   # strategic?
  - surface_canonical_workspace_write(target, project_root=None,
        content=None) -> WorkspaceDecision

Rule YAML contract this implements:
  rules/innate/12-canonical-workspace-write-nudge.yaml
    - trigger: write_to_downloads + canonical_workspace_declared +
      file_classifies_as_strategic_deliverable
    - heuristic_for_strategic_deliverable.filename_patterns / content_signals
    - allowlist.patterns (zip / pdf / screenshots / handoff / session_NNN)
    - scope: per-project (only fires when a canonical workspace is declared)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePath, Path


# Marker file an operator drops to declare "this folder is canonical".
# Shared vocabulary with rule 04 / filesystem_context.CANONICAL_MARKER.
CANONICAL_MARKER = "CANONICAL.txt"

# Drift-location folder names. Generic OS-default user folders where
# project source-of-truth should NOT live. Matched case-insensitively as a
# path SEGMENT (not a substring) so "Downloads_archive" is not a hit.
_DRIFT_SEGMENTS = ("downloads", "desktop", "documents")
_DOWNLOADS_SEGMENT = "downloads"

# Memory-pointer filename that declares a canonical workspace, per the rule
# YAML ("a memory file matching project_*_workspace.md").
_WORKSPACE_POINTER_RE = re.compile(r"^project_.+_workspace\.md$", re.IGNORECASE)


# Strategic-deliverable filename patterns. Mirrors
# heuristic_for_strategic_deliverable.filename_patterns in the rule YAML.
# Matched against the BASENAME only.
_STRATEGIC_FILENAME_RES: tuple[re.Pattern, ...] = (
    re.compile(r"^phase_-?\d+_.*\.md$", re.IGNORECASE),     # phase_-1_..., phase_1_...
    re.compile(r".*_market_analysis\.md$", re.IGNORECASE),
    re.compile(r".*_pain_points\.md$", re.IGNORECASE),
    re.compile(r".*_product_decisions\.md$", re.IGNORECASE),
    re.compile(r".*_voice_positioning\.md$", re.IGNORECASE),
    re.compile(r".*_strategy(_memo)?\.(md|pdf)$", re.IGNORECASE),
    re.compile(r".*_competitive_analysis\.md$", re.IGNORECASE),
    re.compile(r".*_financial_projection\.md$", re.IGNORECASE),
    re.compile(r".*_board_(update|memo)\.md$", re.IGNORECASE),
)

# Content signals — frontmatter `type: project` / `type: deliverable`.
# Mirrors content_signals in the rule YAML. Anchored to a frontmatter-style
# `type:` line so a passing mention of the word "deliverable" in prose
# doesn't false-fire.
_CONTENT_TYPE_RE = re.compile(
    r"^\s*type:\s*(project|deliverable)\s*$", re.IGNORECASE | re.MULTILINE
)

# Allowlist patterns — correct Downloads writes that must NOT be blocked.
# Mirrors allowlist.patterns in the rule YAML. Checked against the basename
# AND, for folder-scoped entries, against path segments.
_ALLOWLIST_BASENAME_RES: tuple[re.Pattern, ...] = (
    re.compile(r".*\.zip$", re.IGNORECASE),            # build artifacts, archives
    re.compile(r".*\.pdf$", re.IGNORECASE),            # PDF export = sharing copy
    re.compile(r"screenshot.*\.png$", re.IGNORECASE),  # screenshots
    re.compile(r".*_handoff_.*\.md$", re.IGNORECASE),  # session handoff docs
)
# Folder-scoped allowlist entries: any path that traverses one of these
# folder names is allowlisted (the rule YAML lists `allostat_handoffs/` and
# `session_\d{8}/`).
_ALLOWLIST_FOLDER_LITERALS = ("allostat_handoffs",)
_ALLOWLIST_FOLDER_RES: tuple[re.Pattern, ...] = (
    re.compile(r"^session_\d{8}$", re.IGNORECASE),
)


@dataclass(frozen=True)
class WorkspaceDecision:
    """Result of the rule-12 surface predicate.

    - should_pause:   True iff all three rule-12 conditions hold (Downloads
                      target + canonical workspace declared + strategic
                      deliverable) AND the target is not allowlisted.
    - is_downloads:   target is under a Downloads folder (any drive).
    - is_strategic:   target classifies as a strategic deliverable.
    - canonical_path: the declared canonical workspace root (for the
                      red-box [redirect] option), or None when none declared.
    - reason:         short machine tag for forensics / logging.
    """

    should_pause: bool
    is_downloads: bool
    is_strategic: bool
    canonical_path: str | None
    reason: str


# ---------- path-segment helpers ----------


def _segments(path: str) -> list[str]:
    """Lowercased path segments, drive/root stripped. Empty on bad input.

    Parses host-INDEPENDENTLY: both `\\` and `/` are treated as separators
    regardless of the OS the hook runs on. A bare `PurePath` is HOST-flavored
    — on a POSIX host `PurePosixPath(r'C:\\...\\Downloads\\f').parts` returns a
    single unsplit element, so a Windows backslash path would never expose its
    Downloads/Desktop/Documents segment (H-21). Normalizing the separators
    ourselves makes Windows-shaped and POSIX-shaped inputs split correctly on
    any platform. Falls back to an empty list on bad input.

    Residual (accepted, review 2026-07-12): backslash is a legal filename char
    on POSIX, so a genuine POSIX path with a literal `\\` in a name over-splits
    here. This is rare and fails toward OVER-detection (a spurious canonical-
    workspace nudge, never an exposure), so we keep the simple unconditional
    normalize rather than add fragile path-flavor sniffing.
    """
    if not path:
        return []
    try:
        normalized = str(path).replace("\\", "/")
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for part in normalized.split("/"):
        seg = part.strip().lower()
        # Skip empties (from leading/collapsed separators) and drive anchors
        # ("c:", "c:\\" -> "c:").
        if not seg or seg.endswith(":"):
            continue
        out.append(seg)
    return out


def is_downloads_path(path: str | None) -> bool:
    """True iff `path` traverses a Downloads folder on ANY drive.

    Segment match, not substring: `Downloads_archive` is NOT a hit. The
    rule must catch the operator's D:-drive Downloads as well as C:.
    """
    if not path:
        return False
    return _DOWNLOADS_SEGMENT in _segments(path)


def is_drift_location(path: str | None) -> bool:
    """True iff `path` traverses a generic drift folder: Downloads, Desktop,
    or Documents (any drive). These are OS-default user folders where
    project source-of-truth should not live."""
    if not path:
        return False
    segs = set(_segments(path))
    return any(seg in segs for seg in _DRIFT_SEGMENTS)


# ---------- canonical-workspace discovery ----------


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _safe_iterdir_has_workspace_pointer(d: Path) -> bool:
    """True iff directory `d` holds a project_*_workspace.md memory pointer."""
    try:
        for child in d.iterdir():
            try:
                if child.is_file() and _WORKSPACE_POINTER_RE.match(child.name):
                    return True
            except OSError:
                # Best-effort: an unreadable/broken child entry (permission,
                # dangling junction) is treated as "not a workspace pointer".
                # Silence is intentional — a hook must not crash mid-scan; a
                # missed pointer is recoverable, a raised hook is not.
                continue
    except OSError:
        # Best-effort: directory not listable (permission, broken junction)
        # → "no pointer here". Silenced because this is a read-only
        # filesystem probe in a PreToolUse hot path; raising would break
        # the operator's session.
        return False
    return False


def find_canonical_workspace(path: str | None) -> str | None:
    """Walk up from `path` looking for a declared canonical workspace.

    A workspace is "declared" when an ancestor directory (including the
    file's own parent) holds either:
      - a CANONICAL.txt marker, OR
      - a project_*_workspace.md memory pointer.

    Returns the declaring directory's path as a string, or None when no
    declaration is found anywhere up the chain. All filesystem errors are
    treated as "no declaration" (safe default — never raise).
    """
    if not path:
        return None
    try:
        start = Path(path)
    except (TypeError, ValueError):
        return None

    # If `path` itself is an existing directory, start the walk there;
    # otherwise start at its parent (the typical case: path is a file).
    try:
        current = start if start.is_dir() else start.parent
    except OSError:
        current = start.parent

    seen: set[Path] = set()
    while current and current not in seen:
        seen.add(current)
        try:
            if _safe_is_file(current / CANONICAL_MARKER):
                return str(current)
            if _safe_iterdir_has_workspace_pointer(current):
                return str(current)
        except OSError:
            # Best-effort: this ancestor isn't probeable (permission, broken
            # junction). Silenced because the safe default is "no canonical
            # declaration at this level" — keep walking up rather than crash
            # the PreToolUse hook. Helpers above already swallow their own
            # OSErrors; this guards the path-join arithmetic itself.
            pass
        parent = current.parent
        if parent == current:  # reached filesystem root
            break
        current = parent
    return None


# ---------- strategic-deliverable classification ----------


def _is_allowlisted(path: str) -> bool:
    """True iff the target matches a rule-12 allowlist pattern (basename or
    folder-scoped)."""
    if not path:
        return False
    basename = PurePath(path).name
    for pat in _ALLOWLIST_BASENAME_RES:
        if pat.match(basename):
            return True
    segs = _segments(path)
    for seg in segs:
        if seg in _ALLOWLIST_FOLDER_LITERALS:
            return True
        for pat in _ALLOWLIST_FOLDER_RES:
            if pat.match(seg):
                return True
    return False


def _matches_strategic_filename(basename: str) -> bool:
    return any(pat.match(basename) for pat in _STRATEGIC_FILENAME_RES)


def _matches_strategic_content(content: str | None) -> bool:
    if not content:
        return False
    return bool(_CONTENT_TYPE_RE.search(content))


def classify_write_target(path: str | None, content: str | None = None) -> bool:
    """True iff `path` (+ optional `content`) classifies as a strategic
    deliverable per rule 12's heuristic.

    Allowlisted targets (zip / pdf / screenshot / handoff / handoff folder /
    date-stamped session folder) ALWAYS return False — they are explicitly
    correct Downloads writes the rule must not block, even if their filename
    also matches a strategic pattern (e.g. `..._strategy.pdf` is allowlisted
    as a PDF sharing copy).
    """
    if not path:
        return False
    if _is_allowlisted(path):
        return False
    basename = PurePath(path).name
    if _matches_strategic_filename(basename):
        return True
    if _matches_strategic_content(content):
        return True
    return False


# ---------- env gate ----------


def _check_enabled() -> bool:
    """Honor ALLOSTAT_CANONICAL_WORKSPACE_CHECK. Defaults to enabled.

    Operator can set `0|false|no|off` to disable the rule-12 surface for a
    session (mirrors the rollout_detector env-gate idiom).
    """
    raw = os.environ.get("ALLOSTAT_CANONICAL_WORKSPACE_CHECK", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ---------- producer-facing surface predicate ----------


def surface_canonical_workspace_write(
    target_path: str | None,
    project_root: str | None = None,
    content: str | None = None,
) -> WorkspaceDecision:
    """Rule-12 surface predicate the Volume Control producer calls per
    Write/Edit.

    Returns a WorkspaceDecision. `should_pause` is True iff ALL hold:
      1. target is under a Downloads folder (any drive),
      2. a canonical workspace is declared (CANONICAL.txt or
         project_*_workspace.md at/under `project_root`, or — when
         `project_root` is None — discovered by walking up from the target),
      3. the file classifies as a strategic deliverable,
    and the target is not allowlisted.

    `project_root` is the active project's root (passed by the wiring
    layer). When provided, the canonical search starts there so the
    declaration is found even though the target itself lives in Downloads
    (which has no canonical ancestor). When None, the predicate falls back
    to walking up from the target — which, for a Downloads target, will
    almost always find nothing, yielding the safe no-pause default.

    Never raises; on any unexpected condition returns a no-pause decision.
    """
    if not _check_enabled() or not target_path:
        return WorkspaceDecision(
            should_pause=False,
            is_downloads=False,
            is_strategic=False,
            canonical_path=None,
            reason="disabled" if not _check_enabled() else "no_target",
        )

    is_dl = is_downloads_path(target_path)
    is_strat = classify_write_target(target_path, content=content)

    # Canonical workspace: prefer the explicitly-passed project root, else
    # autodiscover from the target path.
    canonical_path = None
    if project_root:
        canonical_path = find_canonical_workspace(project_root)
        # When project_root is an existing directory, find_canonical_workspace
        # already checks it directly (the walk starts AT it), so this fallback
        # is normally redundant. Kept as a narrow defensive net for the one
        # edge where is_dir() errors and the walk starts at the parent instead
        # (fixpass 2026-07-01: prior comment claimed the opposite mechanics).
        if canonical_path is None:
            canonical_path = _direct_declaration(project_root)
    else:
        canonical_path = find_canonical_workspace(target_path)

    should_pause = bool(is_dl and is_strat and canonical_path) and not _is_allowlisted(
        target_path
    )

    reason = "pause" if should_pause else _no_pause_reason(
        is_dl, is_strat, canonical_path, target_path
    )

    return WorkspaceDecision(
        should_pause=should_pause,
        is_downloads=is_dl,
        is_strategic=is_strat,
        canonical_path=canonical_path,
        reason=reason,
    )


def _direct_declaration(root: str | None) -> str | None:
    """Check whether `root` directly holds a canonical declaration."""
    if not root:
        return None
    try:
        d = Path(root)
        if _safe_is_file(d / CANONICAL_MARKER) or _safe_iterdir_has_workspace_pointer(
            d
        ):
            return str(d)
    except (OSError, TypeError, ValueError):
        return None
    return None


def _no_pause_reason(
    is_dl: bool, is_strat: bool, canonical_path: str | None, target_path: str
) -> str:
    if _is_allowlisted(target_path):
        return "allowlisted"
    if not is_dl:
        return "not_downloads"
    if not canonical_path:
        return "no_canonical_workspace"
    if not is_strat:
        return "not_strategic"
    return "no_pause"
