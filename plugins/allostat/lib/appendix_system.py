"""Allostat appendix system — PATCH-138 / v0.6.0 Slice 4.

Two parts:
  - appendix_seeder: SessionStart copies missing seed .md files from
    bundle's data/appendix_seed/ into ~/.claude/allostat/projects/<project>_appendix/
  - canon_appendix_loader: UserPromptSubmit reads appendix files' YAML
    frontmatter (topics, confidence_threshold, eager_fallback) and loads
    content of files whose topics match the prompt

Ported from the v2.3 plugin's appendix_seeder + canon_appendix_loader modules
for v2.4 wrapper.

Privacy: appendix CONTENT lives entirely operator-side. canon_appendix_loader
reads + injects into additionalContext client-side; server never sees content.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path


SEED_SUBDIR_PARTS = ("data", "appendix_seed")
USER_APPENDIX_ROOT_PARTS = (".claude", "allostat", "projects")
APPENDIX_SUFFIX = "_appendix"

# Per-bundle bloat cap (advisor Concern 3): no single appendix seed file
# exceeds APPENDIX_PER_FILE_MAX bytes; total seed dir ≤ APPENDIX_TOTAL_MAX.
APPENDIX_PER_FILE_MAX = 20 * 1024  # 20 KB per file
APPENDIX_TOTAL_MAX = 100 * 1024     # 100 KB total seed dir


# ---------- seeder ----------

def seed_default_appendices(
    *,
    home: Path,
    project_name: str,
    plugin_root: Path,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Copy missing seed files into operator's project appendix folder.

    Args:
        home: operator's home (Path.home() in production)
        project_name: e.g. "allostat"
        plugin_root: plugin install root (passed in by SessionStart hook)

    Returns: tuple of (copied, errors) where:
        copied: list of destination paths newly created (empty when no
            seeds exist or all already in place).
        errors: list of (filename, error_class) tuples for any source
            file that failed to copy. Empty on clean runs.

    H3 audit fix (2026-05-25, S1): previously the OSError on shutil.copy2
    was silently swallowed (`except OSError: continue`), which meant
    always-on architecture files could be invisibly absent from operator's
    appendix folder. The caller (session-start hook) now logs an
    `appendix_seed_failed` observation event per error so /allostat-
    handoff-status surfaces them.

    Ship-2 (2026-05-24): skips files with `universal: true` frontmatter —
    those are project-scope-agnostic content (e.g., the handoff protocol)
    that injects directly from the bundle path on every SessionStart
    regardless of cwd. They don't belong in project-scoped appendix
    folders.
    """
    source_dir = plugin_root.joinpath(*SEED_SUBDIR_PARTS)
    if not source_dir.exists() or not source_dir.is_dir():
        return [], []

    dest_dir = home.joinpath(*USER_APPENDIX_ROOT_PARTS, f"{project_name}{APPENDIX_SUFFIX}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    errors: list[tuple[str, str]] = []
    for src in sorted(source_dir.glob("*.md")):
        # Skip universal files — they inject from bundle directly, not from
        # project-scoped appendix folders.
        meta = _parse_appendix_frontmatter(src)
        if meta is not None and meta.universal:
            continue
        dest = dest_dir / src.name
        if dest.exists():
            continue
        try:
            shutil.copy2(src, dest)
            copied.append(dest)
        except OSError as e:
            # H3 audit fix: surface the failure to caller via errors list.
            # Caller emits an `appendix_seed_failed` observation per error.
            errors.append((src.name, type(e).__name__))
            continue
    return copied, errors


def load_universal_appendix_from_bundle(plugin_root: Path) -> list[AppendixFile]:
    """Ship-2 (2026-05-24) — read appendix files with `universal: true`
    frontmatter directly from the bundle.

    These files inject on every SessionStart regardless of cwd (the handoff
    protocol is the canonical example — session continuity is universal,
    not Allostat-specific). They're read straight from the bundle path so
    no per-project seeding is required; operator-edit isn't expected for
    these spec files.
    """
    source_dir = plugin_root.joinpath(*SEED_SUBDIR_PARTS)
    if not source_dir.exists() or not source_dir.is_dir():
        return []
    files: list[AppendixFile] = []
    for p in sorted(source_dir.glob("*.md")):
        meta = _parse_appendix_frontmatter(p)
        if meta is not None and meta.universal:
            files.append(meta)
    return files


def audit_seed_dir(plugin_root: Path) -> dict:
    """Audit data/appendix_seed/ against per-file + total caps. Used by
    make_bundle.py as a build-time gate.
    """
    source_dir = plugin_root.joinpath(*SEED_SUBDIR_PARTS)
    if not source_dir.exists():
        return {"ok": True, "total_bytes": 0, "files": [], "violations": []}

    files = []
    violations = []
    total = 0
    for p in sorted(source_dir.glob("*.md")):
        size = p.stat().st_size
        total += size
        files.append({"name": p.name, "size": size})
        if size > APPENDIX_PER_FILE_MAX:
            violations.append(
                f"{p.name}: {size} bytes exceeds per-file cap {APPENDIX_PER_FILE_MAX}"
            )
    if total > APPENDIX_TOTAL_MAX:
        violations.append(
            f"total seed dir: {total} bytes exceeds total cap {APPENDIX_TOTAL_MAX}"
        )

    return {
        "ok": len(violations) == 0,
        "total_bytes": total,
        "files": files,
        "violations": violations,
    }


# ---------- canon appendix loader (topic-triggered) ----------

@dataclass
class AppendixFile:
    path: Path
    project: str | None
    topics: list[str]
    confidence_threshold: float
    eager_fallback: bool
    always_on: bool = False  # A1 revert 2026-05-24: when true, file injects every SessionStart for matching cwds
    universal: bool = False  # Ship-2 follow-on 2026-05-24: when true, file injects every SessionStart for ALL cwds (project-scope-agnostic content like the handoff protocol). Mutually exclusive with always_on; universal takes precedence.


def _parse_appendix_frontmatter(path: Path) -> AppendixFile | None:
    """Read YAML frontmatter from an appendix file. Returns None when
    no frontmatter or required fields missing.

    Recognized fields:
      project: <slug>
      topics: [a, b, c]            (or single value)
      confidence_threshold: <float>
      eager_fallback: <bool>
      always_on: <bool>            (A1 revert 2026-05-24 — Bucket A flag;
                                    file is injected at SessionStart for
                                    cwds matching this project, regardless
                                    of prompt; match_topics skips it to
                                    prevent double-inject)
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fm = text[4:end]

    project = None
    topics: list[str] = []
    confidence_threshold = 0.7
    eager_fallback = False
    always_on = False
    universal = False

    for line in fm.splitlines():
        s = line.strip()
        if s.startswith("project:"):
            project = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("topics:"):
            # Format: topics: [a, b, c] or topics: a
            val = s.split(":", 1)[1].strip()
            if val.startswith("[") and val.endswith("]"):
                topics = [t.strip().strip("'\"") for t in val[1:-1].split(",") if t.strip()]
            else:
                topics = [val.strip().strip("'\"")] if val else []
        elif s.startswith("confidence_threshold:"):
            try:
                confidence_threshold = float(s.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif s.startswith("eager_fallback:"):
            val = s.split(":", 1)[1].strip().lower()
            eager_fallback = val in ("true", "yes", "1")
        elif s.startswith("always_on:"):
            val = s.split(":", 1)[1].strip().lower()
            always_on = val in ("true", "yes", "1")
        elif s.startswith("universal:"):
            val = s.split(":", 1)[1].strip().lower()
            universal = val in ("true", "yes", "1")

    return AppendixFile(
        path=path,
        project=project,
        topics=topics,
        confidence_threshold=confidence_threshold,
        eager_fallback=eager_fallback,
        always_on=always_on,
        universal=universal,
    )


# ---------- Appendix Lazy-Load (AL) — core extraction ----------

# AL convention: a `universal: true` appendix MAY carry a `## Core (always-on)`
# section. When present, the SessionStart injection for that universal appendix
# is ONLY the Core section + a one-line on-demand pointer; the full verbose body
# stays on disk and is read only when the agent needs the detail. This cuts the
# fixed ~3.6k-token/session cost of injecting the full universal protocol bodies.
CORE_HEADING = "## Core (always-on)"


def _extract_core_section(text: str) -> str | None:
    """Return the `## Core (always-on)` section (heading + body) from an
    appendix file's text, or None when no such section exists.

    The section runs from the `## Core (always-on)` heading up to (but not
    including) the next top-level `## ` heading, or to end-of-file if it is the
    last section. The returned block INCLUDES the heading line so the injected
    core reads as a self-contained section.

    Backward compat: returning None for files without a Core section lets the
    caller fall back to injecting the full body (so nothing breaks when a core
    is absent).
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == CORE_HEADING:
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        # Next top-level section heading ("## ...") terminates the core block.
        # Deeper headings ("### ...") stay inside the core.
        stripped = lines[j].lstrip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            end = j
            break

    section = "\n".join(lines[start:end]).rstrip()
    return section if section.strip() else None


def _render_universal_core_block(meta: AppendixFile) -> str | None:
    """For a universal appendix with a `## Core (always-on)` section, return the
    core block with the `<appendix_path>` placeholder substituted by the real,
    readable on-disk path (the bundle path the file already lives at — universal
    appendices inject from the bundle, so `meta.path` IS the runtime-readable
    location). Returns None when the file has no Core section (caller falls back
    to full body).
    """
    try:
        text = meta.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    core = _extract_core_section(text)
    if core is None:
        return None
    # The pointer must reference a path the agent can actually Read at runtime.
    # Universal appendices are read straight from the bundle (no per-project
    # seeding), so meta.path is exactly that readable location.
    core = core.replace("<appendix_path>", str(meta.path))
    return core


def load_project_appendix_files(home: Path, project_name: str) -> list[AppendixFile]:
    """Read all appendix files for a project. Returns parsed metadata
    (no body content read until canon_appendix_loader.match_topics
    actually triggers a file).
    """
    appendix_dir = home.joinpath(*USER_APPENDIX_ROOT_PARTS, f"{project_name}{APPENDIX_SUFFIX}")
    if not appendix_dir.exists():
        return []
    files = []
    for p in sorted(appendix_dir.glob("*.md")):
        if p.name == "README.md":
            continue
        meta = _parse_appendix_frontmatter(p)
        if meta is not None and meta.topics:
            files.append(meta)
    return files


def load_always_on_appendix_files(home: Path, project_name: str) -> list[AppendixFile]:
    """A1 revert 2026-05-24 — return only the appendix files with
    `always_on: true` frontmatter for a project.

    Called from SessionStart hook to inject Bucket A architecture files
    on every session opened from an Allostat-managed cwd, regardless of
    whether the prompt mentions architecture topics. Restores the always-
    on injection path Phase 3.1 dedup removed.

    Privacy invariant preserved: content read client-side, injected via
    additionalContext, never crosses to server.
    """
    appendix_dir = home.joinpath(*USER_APPENDIX_ROOT_PARTS, f"{project_name}{APPENDIX_SUFFIX}")
    if not appendix_dir.exists():
        return []
    files: list[AppendixFile] = []
    for p in sorted(appendix_dir.glob("*.md")):
        if p.name == "README.md":
            continue
        meta = _parse_appendix_frontmatter(p)
        if meta is not None and meta.always_on:
            files.append(meta)
    return files


def match_topics(prompt: str, appendix_files: list[AppendixFile]) -> list[AppendixFile]:
    """Return appendix files whose topics match the prompt.

    Match logic: simple substring match (case-insensitive) for each topic
    against the prompt. eager_fallback files match on partial/fuzzy hits;
    non-eager files require exact-word substring match.

    A1 revert 2026-05-24: files with `always_on: true` are SKIPPED here —
    those files are injected always-on by the SessionStart hook via
    `load_always_on_appendix_files()` and would otherwise double-inject
    on prompts matching their topic list.
    """
    if not prompt:
        return []
    prompt_lower = prompt.lower()
    matched = []
    for f in appendix_files:
        if f.always_on:
            continue  # A1 revert: skip always-on files (injected separately)
        for topic in f.topics:
            topic_lower = topic.lower()
            if f.eager_fallback:
                if topic_lower in prompt_lower:
                    matched.append(f)
                    break
            else:
                # Require word boundary on either side for non-eager
                pattern = re.compile(r"\b" + re.escape(topic_lower) + r"\b")
                if pattern.search(prompt_lower):
                    matched.append(f)
                    break
    return matched


def render_appendix_content(matched: list[AppendixFile], max_chars: int = 80_000) -> str:
    """Read the matched appendix files' content and return a concatenated
    block suitable for injection into additionalContext.

    Appendix Lazy-Load (AL): for a `universal: true` appendix that carries a
    `## Core (always-on)` section, inject ONLY that core section + the embedded
    on-demand pointer (the `<appendix_path>` placeholder is substituted with the
    file's real readable on-disk path). The full verbose body is NOT injected —
    it stays on disk and is read on demand. This is the lazy-load that drops the
    fixed per-session cost of the universal protocol bodies.

    Backward compat: a universal appendix with NO Core section, and EVERY
    non-universal (topic-conditional) appendix, still injects its full body —
    so nothing breaks when a core is absent and topic-conditional behavior is
    completely unchanged.
    """
    blocks: list[str] = []
    for f in matched:
        # AL core-vs-full decision point: universal + has-Core -> core+pointer.
        if f.universal:
            core_block = _render_universal_core_block(f)
            if core_block is not None:
                header = (
                    f"\n\n=== Allostat appendix (core): {f.path.name} "
                    f"(project={f.project}, topics={f.topics}) ===\n\n"
                )
                blocks.append(header + core_block)
                continue
        # Full-body path: non-universal appendices, and universal appendices
        # without a Core section (backward compat).
        try:
            text = f.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"\n\n=== Allostat appendix: {f.path.name} (project={f.project}, topics={f.topics}) ===\n\n"
        blocks.append(header + text)
    combined = "".join(blocks)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
    return combined
