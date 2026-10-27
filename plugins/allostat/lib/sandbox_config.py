"""Allostat sandbox configuration loader — PATCH-142 v1.2.0.

Loads per-user sandbox-permission patterns from `~/.allostat/sandbox_config.json`
so that operator-specific path patterns NEVER ship in the plugin source code.

Before PATCH-142, `lib/sandbox_perms.py` hardcoded operator-specific
strings (a username, a drive letter, a dev directory path, a cwd slug,
and nine side-project sandbox folder names). That shipped to every
paying customer's installed plugin, making the sandbox-perms refusal
pillar a no-op on any non-operator machine (none of the substrings
matched customer paths) AND leaking the operator's side-project list
to anyone who could `grep` their own `~/.claude/plugins/`.

Audit ref: v1.1.1 audit T1 + T2 findings (kept locally on the dev's
machine; not shipped with this module).

Contract
--------
Customer side (the default):
  - No `~/.allostat/sandbox_config.json` → all pattern lists empty.
  - `sandbox_perms.is_sandbox_scoped()` returns False for all paths.
  - The pillar is a no-op (matches v1.1.1 customer behavior, just
    explicitly; the previous behavior was a no-op by accident, not
    by design).

Per-user opt-in (operator + advanced users):
  - Author `~/.allostat/sandbox_config.json` with the three pattern lists.
  - Loader reads on every hook fire (cheap; ~1ms; no caching needed
    since the file is sub-1KB and changes rarely).
  - Pillar refuses non-advisor writes to sandbox-scoped paths per the
    same logic as before — but the patterns now reflect THIS user's
    actual project layout, not operator's hardcoded names.

Schema (`~/.allostat/sandbox_config.json`):
  {
    "sandbox_scoped_substrings": [
      "/.claude/projects/<user-specific-advisor-memory>/",
      "/.claude/allostat/projects/<user-specific-project>_sandbox/",
      ...
    ],
    "advisor_cwd_patterns": ["<user-specific cwd substring>"],
    "dev_cwd_patterns": ["<user-specific cwd substring>"],
    "advisor_readme_path": "~/.claude/projects/<user-specific>/memory/README.md"
  }

All keys optional; missing keys fall back to empty list / safe default.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path.home() / ".allostat" / "sandbox_config.json"
DEFAULT_ADVISOR_README_FALLBACK = (
    "~/.claude/allostat/projects/<project>_sandbox/README.md"
)


@dataclass
class SandboxConfig:
    """Resolved sandbox configuration. All fields safe-default to empty
    or a generic fallback — never operator-specific."""
    sandbox_scoped_substrings: tuple[str, ...] = field(default_factory=tuple)
    advisor_cwd_patterns: tuple[str, ...] = field(default_factory=tuple)
    dev_cwd_patterns: tuple[str, ...] = field(default_factory=tuple)
    advisor_readme_path: str = DEFAULT_ADVISOR_README_FALLBACK
    source: str = "default"  # "default" | "user_config" | "user_config_partial" | "error"
    # M-02 (SAFETY): True when a config file was PRESENT on disk but could not
    # be parsed (malformed JSON, wrong shape, unreadable). Distinct from the
    # legitimate absent-config case (source="default", load_error=False).
    # Downstream code MUST fail closed on this — empty patterns from a corrupt
    # config must never be interpreted as "no scoping / allow everything".
    load_error: bool = False


def _fail_closed(path: Path, detail: str) -> "SandboxConfig":
    """Build the fail-closed config for a present-but-unparseable file and log
    loudly to stderr (never stdout — hooks parse stdout)."""
    try:
        print(
            f"[allostat][sandbox_config] SECURITY: sandbox config present but "
            f"unparseable at {path} ({detail}). Failing CLOSED: sandbox-scoped "
            f"paths remain scoped (non-advisor writes denied) until the config "
            f"is repaired. Fix or remove the file to restore normal behavior.",
            file=sys.stderr,
        )
    except Exception:
        # Best-effort: the fail-closed decision below stands even if the stderr
        # warning can't be written — never let a logging failure crash the gate.
        pass
    return SandboxConfig(source="error", load_error=True)


def load_config(config_path: Path | None = None) -> SandboxConfig:
    """Load sandbox configuration from disk, falling back to empty defaults.

    Reads `~/.allostat/sandbox_config.json` (or `config_path` override for
    tests). Returns a SandboxConfig with normalized pattern tuples.

    Absent config (normal customer case) → empty defaults, source="default",
    load_error=False. The pillar is an intentional no-op.

    Present but unparseable config → FAIL CLOSED via source="error",
    load_error=True (M-02). This is a DISTINCT state from absent: a corrupt
    config must never silently collapse into "no scoping / allow everything",
    which would disable the sandbox. Triggers on:
      - Malformed JSON
      - Valid JSON of the wrong shape (not a dict)
      - Permission error / any unexpected exception during read/parse

    All pattern values are lowercased on load (matching sandbox_perms.py's
    case-insensitive comparison) and normalized to forward slashes.
    """
    path = config_path if config_path is not None else CONFIG_PATH

    # H-07 (2026-07-17, SAFETY): existence/stat/open ALL live INSIDE the
    # fail-closed path. A prior `path.exists()` guard sat OUTSIDE this try and
    # treated an INACCESSIBLE config as ABSENT — `Path.exists()` swallows OSError
    # (permission-denied stat, transient IO, unreadable parent dir) and returns
    # False — so a config that COULD NOT be read collapsed to the permissive
    # default (source="default", load_error=False) and silently unscoped every
    # path. Only a genuine FileNotFoundError (the config, or a parent component,
    # truly does not exist) is the legitimate absent case; every other OSError
    # (PermissionError, IsADirectoryError, transient IO) FAILS CLOSED exactly as
    # a corrupt file does, so an unreadable config denies rather than allows.
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        # Normal customer case: no config on disk → explicit no-op default.
        return SandboxConfig(source="default")
    except (json.JSONDecodeError, OSError) as exc:
        return _fail_closed(path, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        return _fail_closed(path, type(exc).__name__)

    if not isinstance(raw, dict):
        return _fail_closed(path, f"top-level JSON is {type(raw).__name__}, expected object")

    # M-02 (SAFETY, 2026-07-16): each pattern field, IF PRESENT, must be a LIST.
    # A present-but-wrong-typed field (e.g. a bare string or object where a list
    # is required — a "forgot the brackets" typo) previously slid through
    # _normalize_pattern_list, which coerced any non-list into () with no error.
    # That silently produced empty patterns + load_error=False, so the sandbox
    # unscoped EVERY path (allow-everything) with no signal. Validate the TYPE
    # up front and FAIL CLOSED on the wrong one, exactly as a corrupt file does.
    # An ABSENT field stays legitimate (falls back to empty); an explicitly EMPTY
    # list ([]) is the right type and is honored as "no patterns here".
    for key in ("sandbox_scoped_substrings", "advisor_cwd_patterns", "dev_cwd_patterns"):
        if key in raw and not isinstance(raw[key], list):
            return _fail_closed(
                path, f"{key} is {type(raw[key]).__name__}, expected a list"
            )
        if key in raw:
            for index, member in enumerate(raw[key]):
                if not isinstance(member, str) or not member.strip():
                    return _fail_closed(
                        path,
                        f"{key}[{index}] is not a non-empty string",
                    )

    sandbox_substrings = _normalize_pattern_list(raw.get("sandbox_scoped_substrings"))
    advisor_patterns = _normalize_pattern_list(raw.get("advisor_cwd_patterns"))
    dev_patterns = _normalize_pattern_list(raw.get("dev_cwd_patterns"))

    advisor_readme = raw.get("advisor_readme_path", DEFAULT_ADVISOR_README_FALLBACK)
    if not isinstance(advisor_readme, str) or not advisor_readme.strip():
        advisor_readme = DEFAULT_ADVISOR_README_FALLBACK

    all_keys = {"sandbox_scoped_substrings", "advisor_cwd_patterns", "dev_cwd_patterns", "advisor_readme_path"}
    present_keys = all_keys & set(raw.keys())
    source = "user_config" if len(present_keys) == len(all_keys) else "user_config_partial"

    return SandboxConfig(
        sandbox_scoped_substrings=sandbox_substrings,
        advisor_cwd_patterns=advisor_patterns,
        dev_cwd_patterns=dev_patterns,
        advisor_readme_path=advisor_readme,
        source=source,
    )


def _normalize_pattern_list(value: object) -> tuple[str, ...]:
    """Coerce a JSON value into a tuple of normalized patterns.

    Validation in ``load_config`` guarantees that configured members are
    non-empty strings. The defensive checks here keep this helper total for
    direct callers without making malformed on-disk members silently valid.

    Each pattern is lowercased and has backslashes replaced with forward
    slashes (matching the comparison done by sandbox_perms.py).
    """
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.lower().replace("\\", "/").strip()
        if not normalized:
            continue
        out.append(normalized)
    return tuple(out)
