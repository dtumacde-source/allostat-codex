"""Allostat sandbox-permission enforcement — PATCH-137 / v0.5.0 Slice 10,
refactored in PATCH-142 v1.2.0 to load patterns from per-user config.

Resolves the session's role from cwd-mapping and denies non-advisor
writes to sandbox-scoped trees (advisor project memory tree, advisor
sandbox folders).

Per advisor brief 2026-05-20 Concern 1: hook-automatic detection via
cwd-mapping (no operator env-var chore); ambiguity default = block only
writes to sandbox-scoped paths (option b). Behavior preserved across
PATCH-142 refactor; only the source of the path patterns changed.

PATCH-142 v1.2.0 — operator-leak fix (audit T1 + T2)
-----------------------------------------------------
Before: `SANDBOX_SCOPED_SUBSTRINGS`, `ADVISOR_CWD_PATTERNS`, and
`DEV_CWD_PATTERNS` were tuple constants hardcoded into this module
with operator-specific values (operator username, drive letter, dev
directory, side-project sandbox folder names). Shipped to every
paying customer's installed plugin — privacy leak AND functional
no-op on non-operator machines.

After: patterns load from `~/.allostat/sandbox_config.json` via
`lib/sandbox_config.py`. Code ships with EMPTY pattern lists; each
user authors their own config. Customers who never author one get
explicit no-op (matches v1.1.1 customer behavior, just by design
not by accident).

Audit ref: v1.1.1 audit T1 + T2 findings (operator-local audit doc; not shipped).

H-03 (Wave-2 2026-07-11) — frontmatter trust inversion
------------------------------------------------------
PATCH-146's CLAUDE.md frontmatter layers (2/3) originally outranked the
operator-owned cwd mapping (layer 4), so ANY cloned repo could
self-declare `allostat_session_role: advisor` and gain sandbox write
access. Frontmatter may now only DOWNGRADE privileges; the widening role
(advisor) is honored solely from operator-owned sources (env var, cwd
mapping). See resolve_session_role for the full contract.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sandbox_config import load_config


_VALID_ROLES = ("advisor", "dev", "operator")

# H-03 (Wave-2, audit 20260708 concurrent review) — roles that WIDEN write
# access relative to the deny-by-default sandbox policy. evaluate_write_
# permission grants sandbox writes ONLY to "advisor", so it is the single
# privilege-widening role. Widening roles are honored exclusively from
# operator-owned sources (env var, ~/.allostat/sandbox_config.json cwd
# mapping) — never from repo CLAUDE.md frontmatter, which any cloned repo
# controls.
_WRITE_WIDENING_ROLES = frozenset({"advisor"})

# PATCH-146 — frontmatter key recognized in CLAUDE.md. Per advisor §7 Q1
# resolution: namespace-clarity wins; ship with `allostat_session_role`.
_FRONTMATTER_KEY_RE = re.compile(
    r"^allostat_session_role:\s*(\w+)\s*$", re.IGNORECASE
)

# Read at most this many lines while searching for the frontmatter block.
# CLAUDE.md files in the wild are usually small, and frontmatter sits at
# the top — a 50-line cap bounds the I/O without rejecting legitimate cases.
_CLAUDE_MD_PARSE_LINE_CAP = 50


@dataclass
class SessionRole:
    role: str  # "advisor" | "dev" | "operator" | "unknown"
    # source: "env_var" | "claude_md_frontmatter_cwd" |
    #         "claude_md_frontmatter_ancestor" | "cwd_mapping" | "default"
    source: str
    cwd: str = ""
    # H-03 forensics: set to the frontmatter-claimed role when a
    # privilege-WIDENING claim (advisor) was found but refused because the
    # operator-owned cwd mapping does not grant it. Empty otherwise.
    refused_frontmatter_role: str = ""


def _parse_claude_md_role(claude_md_path: Path) -> str | None:
    """PATCH-146 — parse `allostat_session_role: <role>` from a CLAUDE.md
    frontmatter block.

    Reads up to _CLAUDE_MD_PARSE_LINE_CAP lines, locates a frontmatter
    block bounded by `---` delimiters, applies the regex to each line
    between them, returns the matched role (lowercased) if it's in
    {advisor, dev, operator}. Returns None on every failure path:
      - file missing / not readable
      - no opening `---` line in the first cap lines
      - no closing `---` line in the first cap lines
      - no `allostat_session_role:` line between delimiters
      - matched value is not in the accepted set

    Fail-closed by design: the caller falls through to lower precedence
    layers (cwd-mapping, default) when this returns None.
    """
    try:
        if not claude_md_path.is_file():
            return None
        with claude_md_path.open("r", encoding="utf-8", errors="replace") as f:
            lines: list[str] = []
            for i, line in enumerate(f):
                if i >= _CLAUDE_MD_PARSE_LINE_CAP:
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError:
        return None

    # Locate the opening `---`.
    try:
        open_idx = lines.index("---")
    except ValueError:
        return None

    # Locate the closing `---` AFTER the opening.
    closing_idx = None
    for j in range(open_idx + 1, len(lines)):
        if lines[j] == "---":
            closing_idx = j
            break
    if closing_idx is None:
        return None

    for line in lines[open_idx + 1 : closing_idx]:
        m = _FRONTMATTER_KEY_RE.match(line)
        if m:
            candidate = m.group(1).strip().lower()
            if candidate in _VALID_ROLES:
                return candidate
            return None  # explicit miss: known key, unknown value
    return None


def _walk_for_claude_md_role(cwd: Path) -> tuple[str, str] | None:
    """PATCH-146 — walk from `cwd` upward looking for the nearest CLAUDE.md
    whose frontmatter declares a role.

    Walk semantics (per advisor §7 Q2 resolution: EXCLUDE home from walk):
      - Start at cwd. If cwd/CLAUDE.md declares a role, return
        (role, "claude_md_frontmatter_cwd").
      - Walk up one ancestor at a time. If <ancestor>/CLAUDE.md declares
        a role, return (role, "claude_md_frontmatter_ancestor").
      - STOP before reaching Path.home() — home-level CLAUDE.md would
        role-tag every session on the machine, which is wrong by design.
      - Also stop at filesystem root, or at any cwd that IS or is above
        Path.home() (degenerate edge cases — caller-driven).

    Returns None if no in-scope ancestor has a frontmatter-declared role.
    """
    try:
        cwd_resolved = cwd.resolve()
        home_resolved = Path.home().resolve()
    except OSError:
        return None

    # Reject degenerate cases where cwd is home or above home.
    try:
        if cwd_resolved == home_resolved:
            return None
        if home_resolved in cwd_resolved.parents:
            # cwd is a strict descendant of home — proceed.
            pass
        else:
            # cwd is NOT inside home (e.g., a different drive). Walk anyway
            # but still stop if we hit home along the way.
            pass
    except OSError:
        return None

    current = cwd_resolved
    is_first = True
    visited: set[Path] = set()
    while True:
        if current in visited:
            return None
        visited.add(current)
        if current == home_resolved:
            return None  # exclude home itself
        candidate = current / "CLAUDE.md"
        role = _parse_claude_md_role(candidate)
        if role is not None:
            source = (
                "claude_md_frontmatter_cwd"
                if is_first
                else "claude_md_frontmatter_ancestor"
            )
            return (role, source)
        parent = current.parent
        if parent == current:
            return None  # filesystem root
        # If the NEXT step would be home, stop here.
        if parent == home_resolved:
            return None
        current = parent
        is_first = False


def _resolve_cwd_mapping_role(cwd: str) -> SessionRole:
    """Layers 4 + 5: cwd-mapping against the OPERATOR-OWNED per-user config
    (~/.allostat/sandbox_config.json), falling back to unknown/default."""
    cwd_lower = cwd.lower().replace("\\", "/")
    config = load_config()
    for pattern in config.advisor_cwd_patterns:
        if pattern in cwd_lower:
            return SessionRole(role="advisor", source="cwd_mapping", cwd=cwd)
    for pattern in config.dev_cwd_patterns:
        if pattern in cwd_lower:
            return SessionRole(role="dev", source="cwd_mapping", cwd=cwd)
    return SessionRole(role="unknown", source="default", cwd=cwd)


def resolve_session_role(cwd: str | None = None) -> SessionRole:
    """Return the session role using the full precedence stack.

    Precedence (post-PATCH-146, trust-inverted by H-03):
      1. ALLOSTAT_SESSION_ROLE env var (operator-explicit override)
      2. CLAUDE.md frontmatter at cwd (allostat_session_role: <role>)
         — NON-WIDENING roles only (dev/operator); see H-03 below
      3. CLAUDE.md frontmatter walk: cwd ancestors up to (but not including)
         home — same non-widening restriction
      4. cwd-mapping against patterns from ~/.allostat/sandbox_config.json
      5. default = unknown (most-restrictive on sandbox-scoped writes only)

    PATCH-146 added layers 2 and 3 so a project can declare its role once
    in CLAUDE.md frontmatter instead of needing a cwd-mapping substring.

    H-03 (Wave-2, audit 20260708 concurrent review) — trust inversion:
    repo CLAUDE.md frontmatter is attacker-reachable (any cloned repo
    ships whatever frontmatter its author wrote), so it may only
    DOWNGRADE privileges, never widen them. A frontmatter role in
    _WRITE_WIDENING_ROLES (advisor — the only role granted sandbox
    writes) is NOT honored from layers 2/3; the decision falls through to
    the operator-owned cwd mapping (layer 4/5), and the refused claim is
    recorded on SessionRole.refused_frontmatter_role for forensics.
    Legitimate advisor sessions therefore come from the env var or an
    advisor_cwd_patterns entry — both operator-owned surfaces an
    untrusted repo cannot touch. Non-widening frontmatter (dev/operator)
    keeps full PATCH-146 semantics, including downgrading a cwd that the
    operator mapping would have classified as advisor.
    """
    env_override = os.environ.get("ALLOSTAT_SESSION_ROLE", "").strip().lower()
    if env_override in _VALID_ROLES:
        return SessionRole(role=env_override, source="env_var", cwd=cwd or "")

    if cwd is None:
        cwd = os.getcwd()
    cwd_path = Path(cwd)

    # PATCH-146 — CLAUDE.md frontmatter walk (layers 2 + 3 combined).
    walk_result = _walk_for_claude_md_role(cwd_path)

    # Operator-owned baseline (layers 4 + 5).
    baseline = _resolve_cwd_mapping_role(cwd)

    if walk_result is None:
        return baseline

    fm_role, fm_source = walk_result
    if fm_role not in _WRITE_WIDENING_ROLES:
        # Downgrade-or-lateral claim: honored at frontmatter precedence.
        # dev/operator get sandbox writes DENIED, so a hostile repo gains
        # nothing by declaring them.
        return SessionRole(role=fm_role, source=fm_source, cwd=cwd)

    # Widening claim (advisor). Authority is the operator-owned mapping
    # ONLY: if the mapping independently grants the same role, use it
    # (source stays cwd_mapping — the frontmatter never decides widening);
    # otherwise refuse the claim and record it.
    if baseline.role != fm_role:
        baseline.refused_frontmatter_role = fm_role
    return baseline


def is_sandbox_scoped(file_path: str | Path) -> bool:
    """Return True if file_path lives in a sandbox-scoped tree where
    non-advisor session writes should be denied.

    Sandbox-scoped substring list loaded from ~/.allostat/sandbox_config.json.
    PATCH-142: customers with no config file get False unconditionally
    (no patterns to match).
    """
    if not file_path:
        return False
    config = load_config()
    # M-02 (SAFETY): a config file present but unparseable fails CLOSED. We
    # cannot know the real scoped substrings, so treat every path as scoped
    # (non-advisor writes denied) rather than silently unscoping everything —
    # which is what empty-patterns-from-corruption would otherwise do.
    if getattr(config, "load_error", False):
        return True
    if not config.sandbox_scoped_substrings:
        return False
    # C1 (Wave 3): match against the literal path AND its fully-resolved real
    # path. Matching only the raw string let a path that RESOLVES into a scoped
    # tree evade the deny gate — `..` traversal, a symlink into the tree, or a
    # Windows 8.3 short name. os.path.realpath collapses `..` + symlinks (and,
    # for existing paths on Windows, expands 8.3 short names to long form).
    # Testing BOTH keeps the gate fail-safe: a path that merely looks scoped OR
    # resolves scoped is scoped. realpath never raises for a non-existent target
    # (it resolves as far as it can), but guard defensively anyway.
    def _strip_component_trailing(p: str) -> str:
        # Windows silently strips trailing dots/spaces from each path component
        # at write time, so `advault.\x` lands in `advault\x`. realpath can't
        # collapse a component that doesn't exist yet, so also test a form with
        # per-segment trailing dots/spaces removed.
        return "/".join(
            seg.rstrip(". ") if seg not in ("", ".", "..") else seg
            for seg in p.split("/")
        )

    raw = str(file_path).lower().replace("\\", "/")
    candidates = {raw, _strip_component_trailing(raw)}
    try:
        real = os.path.realpath(str(file_path)).lower().replace("\\", "/")
        candidates.add(real)
        candidates.add(_strip_component_trailing(real))
    except (OSError, ValueError):
        pass
    return any(
        substr in cand
        for cand in candidates
        for substr in config.sandbox_scoped_substrings
    )


@dataclass
class PermissionDecision:
    allowed: bool
    reason: str
    denial_message: str = ""


def evaluate_write_permission(
    file_path: str | Path,
    role: SessionRole,
) -> PermissionDecision:
    """Decide whether a Write/Edit on file_path should proceed given the
    session role.

    Default policy per advisor §1 Concern 1 option (b):
      block ONLY writes to sandbox-scoped paths when role ambiguous;
      everything else proceeds.

    PATCH-142: denial message no longer references operator-specific README
    paths. The advisor_readme_path comes from per-user config (or a generic
    placeholder if no config exists).
    """
    if not is_sandbox_scoped(file_path):
        return PermissionDecision(allowed=True, reason="not_sandbox_scoped")

    # File IS sandbox-scoped. Decide based on role.
    if role.role == "advisor":
        # Advisor-role session: per-action operator approval is the policy.
        # The wrapper-side hook can't actually prompt the operator (Claude
        # Code handles tool-permission prompts at its own layer). We return
        # allowed=True with a flag so the calling hook can log it; the
        # higher-priority permission gate is Claude Code's standard
        # tool-permission flow. The README documents the policy the
        # operator follows on their side.
        return PermissionDecision(
            allowed=True,
            reason="advisor_session_per_action_approval_required_at_claude_code_layer",
        )

    # role is dev, operator, or unknown: DENY
    config = load_config()
    denial = (
        "Sandbox write denied. Session role detected as "
        f"'{role.role}' (source: {role.source}); target path is a "
        f"sandbox-scoped tree where non-advisor writes are denied. "
        f"Permission policy: {config.advisor_readme_path}. "
        "Override paths: (1) set ALLOSTAT_SESSION_ROLE=advisor if this "
        "is actually an advisor session, or (2) operate from an "
        "advisor-configured cwd. "
        "Per Allostat v1.2.0 PATCH-142 sandbox enforcement (per-user config)."
    )
    return PermissionDecision(
        allowed=False,
        reason=f"non_advisor_session_on_sandbox_path:role={role.role}",
        denial_message=denial,
    )
