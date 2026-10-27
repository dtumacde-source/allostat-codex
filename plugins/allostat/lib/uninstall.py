"""Allostat uninstaller — wrapper-only removal across Claude Code + Codex.

Removes the ACTIVE MACHINERY only: the Claude Code plugin + its marketplace entry
+ MCP registration, and the Codex config.toml block. It does NOT touch a single
byte of user DATA — memory trees, handoffs, .allostat state, appendices — nor the
ALLOSTAT_MCP_TOKEN env var (the user's credential; a harmless orphan once the MCP
registration is gone, and left so a reinstall needs no fresh code).

Removing the wrapper fully DISABLES Allostat (no hooks fire, MCP unregistered, no
banner); the user's files sit dormant and intact, and a reinstall resumes cleanly.

Design: all logic lives here (one testable module) so the PowerShell/shell entry
points stay trivial. The delicate text/JSON transforms are pure functions; the
`claude mcp remove` call is injectable so the whole flow is unit-testable without
a live CLI. Codex removal delegates to codex_wiring (shared markers with install).

Surface contract (Sol P2, 2026-07-18): the surface is ALWAYS explicit -- run()
requires it and the CLI requires --surface. Unknown ownership fails closed;
"both" is a deliberate, explicitly-requested full removal, never a fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

# codex_wiring is imported LAZILY inside uninstall_codex() so a Claude-only
# uninstall (surface="claude") never needs the Codex helper present.

_PLUGIN_NAME = "allostat-mcp"
# MCP server registration name variants seen across install eras.
_MCP_NAMES = ("allostat", "allostat-mcp")


# Directory entries the uninstaller OWNS inside plugins/marketplaces/local/: the
# marketplace-metadata dir and Allostat's own plugin subdir(s). ANYTHING else
# under that dir belongs to another plugin or the user, so a wholesale rmtree of
# the local marketplace must never run when a non-owned entry is present (F-03).
_OWNED_MARKETPLACE_ENTRIES = frozenset({".claude-plugin", _PLUGIN_NAME, "allostat"})


def _standalone_wrapper_is_owned(path: Path) -> bool:
    """Recognize the exact standalone machinery before recursive removal."""

    required = (
        path / "plugin.json",
        path / "lib" / "uninstall.py",
        path / "lib" / "codex_wiring.py",
    )
    for candidate in required:
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                return False
        except OSError:
            return False
    try:
        manifest = json.loads(required[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("name") == _PLUGIN_NAME


def remove_marketplace_entry(text: str, plugin_name: str = _PLUGIN_NAME):
    """Remove `plugin_name` from marketplace.json's plugins[].

    Returns (new_json_or_None, removed):
      - (json_str, True)  — entry removed → rewrite the file. plugins[] MAY now be
                            empty; the JSON stays valid either way. Whether the
                            local marketplace DIR is safe to delete wholesale is
                            NOT decided here — it depends on the on-disk directory
                            contents (a shared `local/` marketplace can hold other
                            plugins' files not listed in plugins[]; F-03), so that
                            call lives in `uninstall_claude` where the FS is visible.
      - (text, False)     — entry not present / unparseable → benign no-op
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text, False
    plugins = data.get("plugins", [])
    # Shape-guard (H-30): valid JSON may still carry a wrong-shape plugins[] — a
    # string, a dict, or a list with non-dict entries. A non-list is "nothing to
    # remove"; inside a list, only dict entries can carry a "name", so skip the
    # rest instead of calling .get() on them (which would raise AttributeError and
    # abort the uninstall BEFORE plugin-dir removal / mcp remove — a PARTIAL wipe).
    # Non-dict entries are preserved in `kept`, never silently dropped.
    if not isinstance(plugins, list):
        return text, False
    kept = [p for p in plugins if not (isinstance(p, dict) and p.get("name") == plugin_name)]
    if len(kept) == len(plugins):
        return text, False
    data["plugins"] = kept
    return json.dumps(data, indent=2) + "\n", True


def _local_marketplace_is_allostat_only(local_mkt: Path) -> bool:
    """True iff `local_mkt` contains nothing but Allostat-owned entries
    (marketplace metadata + our plugin subdir). If ANY other entry is present,
    the dir is shared and must not be wholesale-removed (F-03). Errs toward
    preservation: an unreadable dir returns False (keep, don't rmtree)."""
    try:
        entries = list(local_mkt.iterdir())
    except OSError:
        return False
    return all(e.name in _OWNED_MARKETPLACE_ENTRIES for e in entries)


def _still_present(path: Path) -> bool:
    """True iff ``path`` still exists after a removal attempt. Fail-closed (M-02):
    an inaccessible stat (permission/IO error) counts as STILL PRESENT, because
    we cannot confirm the removal. Uses lstat so a surviving symlink is detected,
    not followed. Only a definitive FileNotFoundError proves absence."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


try:
    # Sol M-01: SHARE the canonical three-form MCP-list matcher with install
    # validation (recognizes `allostat`, `allostat-mcp`, the plugin-namespaced
    # `plugin:allostat-mcp:allostat-mcp`, and the mcp.allostat.ai URL fallback),
    # rather than a second, weaker first-token parser that missed the namespaced
    # form. Both lib/ and hooks/ are on the wrapper's import path.
    from install_validation import _MCP_ENTRY_OR_URL_RE as _MCP_REGISTERED_RE
except Exception:  # pragma: no cover - defensive: hooks/ not importable at runtime
    import re as _re
    # Identical pattern to install_validation._MCP_ENTRY_OR_URL_RE; a drift-guard
    # test asserts the two stay in sync whenever install_validation is importable.
    _MCP_REGISTERED_RE = _re.compile(
        r"(?:^|\s|:)(?:allostat|allostat-mcp|plugin:allostat-mcp:allostat-mcp)\b"
        r"|mcp\.allostat\.ai",
        _re.MULTILINE,
    )


def _allostat_mcp_registered(listing: str) -> bool:
    """True iff ANY Allostat MCP entry — in any known `claude mcp list` form, or
    by the mcp.allostat.ai URL — is still present. Uses the canonical matcher so
    the plugin-namespaced `plugin:allostat-mcp:allostat-mcp` form is not missed
    (Sol M-01). If Allostat is still listed, deregistration is not confirmed."""
    return bool(_MCP_REGISTERED_RE.search(listing))


def _default_mcp_remover() -> list[dict]:
    """Deregister each MCP name variant and VERIFY the outcome.

    Returns one structured record per name: ``{"name", "removed", "detail"}``.
    ``removed`` is True only when absence is CONFIRMED via `claude mcp list`
    (whether we just removed it or it was already gone). A still-registered name,
    or an unrunnable/failing CLI (missing, timeout, non-zero list) yields
    ``removed=False`` so the caller reports an actionable INCOMPLETE uninstall
    rather than an unqualified success (M-02). Fail-closed: never claim a removal
    we could not verify."""
    claude = shutil.which("claude")
    remove_detail: dict[str, str] = {}
    for name in _MCP_NAMES:
        if claude is None:
            remove_detail[name] = "remove skipped (claude CLI not found)"
            continue
        try:
            r = subprocess.run(
                [claude, "mcp", "remove", name, "--scope", "user"],
                capture_output=True, text=True, timeout=30,
            )
            remove_detail[name] = f"remove rc={r.returncode}"
        except Exception as e:  # CLI missing / timeout — unverified
            remove_detail[name] = f"remove skipped ({type(e).__name__})"

    # Verify absence. Only a clean `mcp list` lets us CONFIRM removal.
    listing: str | None = None
    verify_note = "claude CLI not found"
    if claude is not None:
        try:
            lst = subprocess.run(
                [claude, "mcp", "list"],
                capture_output=True, text=True, timeout=30,
            )
            if lst.returncode == 0:
                listing = lst.stdout or ""
            else:
                verify_note = f"mcp list rc={lst.returncode}"
        except Exception as e:
            verify_note = f"mcp list skipped ({type(e).__name__})"

    results: list[dict] = []
    still_registered = _allostat_mcp_registered(listing) if listing is not None else None
    for name in _MCP_NAMES:
        if listing is None:
            results.append({
                "name": name,
                "removed": False,
                "detail": f"{remove_detail[name]}; absence UNVERIFIED ({verify_note})",
            })
        elif still_registered:
            results.append({
                "name": name,
                "removed": False,
                "detail": f"{remove_detail[name]}; STILL REGISTERED",
            })
        else:
            results.append({
                "name": name,
                "removed": True,
                "detail": f"{remove_detail[name]}; confirmed absent",
            })
    return results


def uninstall_claude(claude_dir, *, mcp_remover=None) -> dict:
    """Remove the Claude Code wrapper: marketplace entry, plugin dir(s), MCP reg.
    Leaves everything else in ~/.claude (memory, projects, settings) untouched."""
    claude_dir = Path(claude_dir)
    removed: list[str] = []
    # Paths a removal ATTEMPT could not clear (locked/protected state left behind
    # by rmtree(ignore_errors=True)). M-10: never record a path as `removed` — nor
    # return success — without verifying it is actually gone; a surviving token-
    # bearing .env or plugin dir is an actionable partial failure, not a success.
    failed: list[str] = []
    local_mkt = claude_dir / "plugins" / "marketplaces" / "local"
    mkt_json = local_mkt / ".claude-plugin" / "marketplace.json"
    plugin_dir = local_mkt / _PLUGIN_NAME
    legacy_dir = claude_dir / "plugins" / _PLUGIN_NAME  # pre-marketplace layout
    # Cache tree: Claude Code loads hooks from plugins/cache/local/<name>/<ver>/,
    # and the installer writes a token-bearing .env there (install.ps1 line 1029).
    # It lives OUTSIDE the marketplace dir, so remove it explicitly — otherwise
    # uninstall would leave a plaintext credential .env orphaned on disk.
    cache_dir = claude_dir / "plugins" / "cache" / "local" / _PLUGIN_NAME
    cache_legacy = claude_dir / "plugins" / "cache" / "local" / "allostat"

    # 1. marketplace.json entry. If removing our entry empties plugins[], the
    #    local marketplace MAY be ours-only — but only the on-disk contents can
    #    prove that. Wholesale-rmtree ONLY when the dir holds nothing but
    #    Allostat-owned entries; otherwise keep the shared dir, leaving a valid
    #    empty-plugins marketplace.json and removing just our own subdir (step 2).
    #    (F-03: the old code inferred "allostat-only" from empty plugins[] and
    #    rmtree'd unrelated plugins' files.)
    wholesale = False
    if mkt_json.is_file():
        new_json, was_removed = remove_marketplace_entry(mkt_json.read_text(encoding="utf-8"))
        if was_removed:
            plugins_now_empty = not json.loads(new_json).get("plugins")
            if plugins_now_empty and _local_marketplace_is_allostat_only(local_mkt):
                # allostat:destructive-ok F-03-scoped — reached only when the dir holds nothing but Allostat-owned entries (checked in the condition above)
                shutil.rmtree(local_mkt, ignore_errors=True)
                if _still_present(local_mkt):
                    # rmtree swallowed an error (locked/protected/inaccessible) — the dir remains.
                    # Report the survivor; leave `wholesale` False so step 2 retries
                    # our own subdirs individually rather than assuming they're gone.
                    failed.append(str(local_mkt) + " (local marketplace — rmtree left state behind)")
                else:
                    removed.append(str(local_mkt) + " (local marketplace — was allostat-only)")
                    wholesale = True
            else:
                mkt_json.write_text(new_json, encoding="utf-8")
                note = " (allostat entry)"
                if plugins_now_empty:
                    note = " (allostat entry; local marketplace kept — holds non-allostat content)"
                removed.append(str(mkt_json) + note)

    # 2. plugin dir(s). If the local marketplace was removed wholesale, plugin_dir
    #    is already gone (it lived inside it) — the exists() guard makes that a no-op.
    for d in (plugin_dir, legacy_dir, cache_dir, cache_legacy):
        if _still_present(d):
            # allostat:destructive-ok removes only Allostat's OWN plugin/cache dirs (fixed literal paths built above), never a shared or user directory
            shutil.rmtree(d, ignore_errors=True)
            if _still_present(d):
                # Locked/protected/inaccessible — the dir (possibly a token-bearing
                # cache .env) survived or can't be confirmed gone. Report it as
                # failed instead of claiming removal (M-02 fail-closed).
                failed.append(str(d))
            else:
                removed.append(str(d))

    # 3. MCP registration.
    mcp = (mcp_remover or _default_mcp_remover)()

    return {
        "surface": "claude",
        "removed": removed,
        "failed": failed,
        "mcp_remove": mcp,
        "wholesale_marketplace": wholesale,
    }


#: Removal is authorized by CONTENT, never by filename — a user's own module
#: of the same name is not ours to touch. The structured ownership record
_LAUNCHER_CANDIDATES = ("python", "py", "python3")
#: Current filename plus every superseded one we may still need to clean up.
#: The legacy name is judged by codex_wiring.is_legacy_generated_launcher —
#: a structural fingerprint of what the old generator actually emitted —
#: never by any substring: this module deliberately carries NO ownership
#: grammar of its own (a test pins that), because two implementations of
#: "is this ours" is how the reviewer deleted a stranger's file over one
#: quoted sentence (remediation audit H02).
_LAUNCHER_FILENAMES = ("allostat_mcp_codex_launcher.py", "allostat_hook.py")
_LEGACY_LAUNCHER_FILENAME = "allostat_hook.py"


def _candidate_user_sites() -> list[Path]:
    """Every user site-packages directory a canonical interpreter token
    resolves to on this machine — the same candidate set the installer selects
    from, probed READ-ONLY (`<candidate> -m site --user-site`), deduplicated
    (py and python frequently name the same interpreter). A candidate that is
    missing, errors, or reports nothing usable is skipped silently: uninstall
    must keep working on a machine whose Python has changed since install."""
    sites: list[Path] = []
    seen: set[str] = set()
    for candidate in _LAUNCHER_CANDIDATES:
        try:
            proc = subprocess.run(
                [candidate, "-m", "site", "--user-site"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines or not os.path.isabs(lines[-1]):
            continue
        key = os.path.normcase(lines[-1])
        if key not in seen:
            seen.add(key)
            sites.append(Path(lines[-1]))
    return sites


def _launcher_is_ours(filename: str, text: str) -> bool:
    """True only for a launcher file Allostat provably generated — decided by
    the ONE wiring authority, never by any grammar of this module's own.

    Current filename: the strict structured ownership record. Legacy filename
    (pre-record era): positive structural identification of the shape the old
    generator actually emitted. A file matching neither — and EVERY file when
    the wiring module cannot be imported — is foreign and stays: an
    uninstaller that cannot PROVE ownership preserves. (The previous version
    fell back to a raw substring here and deleted a stranger's module over
    one quoted sentence — remediation audit H02.)"""
    try:
        import codex_wiring  # lazy: the same single authority the installer uses
    except Exception:
        return False
    if filename == _LEGACY_LAUNCHER_FILENAME:
        checker = getattr(codex_wiring, "is_legacy_generated_launcher", None)
        return bool(checker is not None and checker(text))
    reader = getattr(codex_wiring, "read_launcher_ownership", None)
    return bool(reader is not None and reader(text) is not None)


def _remove_launcher_modules() -> tuple[list[str], list[str]]:
    """Remove OUR generated launcher module from every candidate user site.

    Both the current filename and every superseded one are swept, because a
    machine upgraded across the rename would otherwise keep the old module
    forever. Returns (removed, preserved): ``preserved`` lists same-named
    objects that were NOT provably ours — foreign files, but also symlinks,
    junctions, directories, special objects, and unreadable files, none of
    which are ever read through or removed."""
    try:
        import codex_wiring  # the ONE ownership authority; without it nothing
        # can be proven ours, so nothing is touched at all.
    except Exception:
        return [], []
    removed: list[str] = []
    preserved: list[str] = []
    for site_dir in _candidate_user_sites():
        for filename in _LAUNCHER_FILENAMES:
            module = site_dir / filename
            kind = codex_wiring.classify_path_kind(module)
            if kind == "missing":
                continue
            if kind != "file":
                # A symlink/junction, directory, or special object under our
                # module's name is ambiguous at best and a trap at worst:
                # never followed, never removed — reported, not skipped.
                preserved.append(str(module))
                continue
            try:
                text = module.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Unreadable: cannot prove ownership, so it stays (inert once
                # the config block is gone) and the report says so.
                preserved.append(str(module))
                continue
            if _launcher_is_ours(filename, text):
                try:
                    # allostat:destructive-ok parsed launcher ownership was proven immediately above; foreign files are preserved.
                    module.unlink()
                except OSError:
                    preserved.append(str(module))
                    continue
                removed.append(str(module))
            else:
                preserved.append(str(module))
    return removed, preserved


def uninstall_codex(codex_config, standalone_wrapper=None) -> dict:
    """Remove the Codex block, the generated user-site launcher module(s), and
    the owned standalone wrapper, if present.

    Non-Allostat Codex configuration, the recovery credential, and every other
    ``~/.allostat`` path are preserved.
    A symlink/special object at the standalone wrapper path is ambiguous and is
    never followed or removed.
    """
    import codex_wiring  # lazy: only the Codex surface needs the helper

    removed = codex_wiring.uninstall(codex_config)
    launcher_removed, launcher_preserved = _remove_launcher_modules()
    wrapper_removed = False
    wrapper_refused = False
    wrapper_path = Path(standalone_wrapper) if standalone_wrapper is not None else None
    if wrapper_path is not None:
        try:
            mode = wrapper_path.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            if wrapper_path.is_symlink() or not wrapper_path.is_dir():
                wrapper_refused = True
            elif not _standalone_wrapper_is_owned(wrapper_path):
                wrapper_refused = True
            else:
                # C-01 — exact standalone machinery path supplied by run();
                # ~/.allostat/codex and credential.env remain untouched.
                # allostat:destructive-ok C-01 — standalone wrapper machinery only
                shutil.rmtree(wrapper_path)
                wrapper_removed = True
    return {
        "surface": "codex",
        "config": str(codex_config),
        "block_removed": removed,
        "launcher_modules_removed": launcher_removed,
        "launcher_modules_preserved": launcher_preserved,
        "wrapper": str(wrapper_path) if wrapper_path is not None else None,
        "wrapper_removed": wrapper_removed,
        "wrapper_refused": wrapper_refused,
    }


def run(home=None, *, codex_home=None, mcp_remover=None, surface) -> dict:
    """Uninstall the requested surface(s) — whichever are present.

    ``surface`` is "claude", "codex", or "both", and it is REQUIRED (Sol P2,
    2026-07-18): unknown ownership must fail closed, never broaden into
    cross-surface removal. Each entry point passes the surface that OWNS it
    (the Claude uninstall passes "claude", the standalone Codex uninstall
    passes "codex"), so neither install ever reaches into the other (operator
    requirement 2026-07-18). ``both`` is the deliberate, explicitly-requested
    full removal — never a default. Returns a structured summary the entry
    points render for the user."""
    if surface not in ("both", "claude", "codex"):
        raise ValueError(f"unknown uninstall surface: {surface!r}")
    home = Path(home) if home is not None else Path.home()
    # surface_requested lets the summary stay honest when NOTHING was found to
    # remove (Sol gate-3 P3): the already-absent headline names what the caller
    # ASKED for, instead of inferring scope from empty records.
    result: dict = {
        "claude": None,
        "codex": None,
        "preserved": [],
        "surface_requested": surface,
    }

    if surface in ("both", "claude"):
        claude_dir = home / ".claude"
        if claude_dir.is_dir():
            result["claude"] = uninstall_claude(claude_dir, mcp_remover=mcp_remover)
            # name the data we deliberately preserved (never removed)
            for rel in ("projects", "allostat"):
                p = claude_dir / rel
                if p.exists():
                    result["preserved"].append(str(p))

    if surface in ("both", "codex"):
        codex_home = Path(codex_home) if codex_home is not None else home / ".codex"
        codex_config = codex_home / "config.toml"
        standalone_wrapper = home / ".allostat" / "codex" / _PLUGIN_NAME
        if codex_config.is_file() or standalone_wrapper.exists() or standalone_wrapper.is_symlink():
            result["codex"] = uninstall_codex(codex_config, standalone_wrapper)

    allostat_data = home / ".allostat"
    if allostat_data.exists():
        result["preserved"].append(str(allostat_data))

    return result


def _uninstall_incomplete(result: dict) -> bool:
    """True iff any removal ATTEMPT could not be confirmed complete, on EITHER
    surface (Sol gate-2): a surviving Claude filesystem path (M-10), an
    unverified/failed MCP deregistration (M-02), OR a Codex wrapper that was
    refused and therefore SURVIVES (a symlink/unowned dir we correctly declined
    to remove). Callers report actionable partial failure + nonzero exit instead
    of an unqualified success."""
    c = result.get("claude")
    if c:
        if c.get("failed"):
            return True
        for m in c.get("mcp_remove", []):
            if isinstance(m, dict) and not m.get("removed", False):
                return True
    x = result.get("codex")
    if x and x.get("wrapper_refused"):
        # The wrapper was present but we declined to remove it -> it survives.
        # Correct behavior, but NOT a complete uninstall: surface it + exit nonzero.
        return True
    return False


def _format_summary(result: dict) -> str:
    c = result.get("claude")
    x = result.get("codex")
    # A surface only counts as ACTED ON when something was actually removed
    # (Sol last-mile P3): run() creates a record on mere HOST presence (any
    # ~/.codex/config.toml, any .claude dir), so a non-None record must never
    # by itself produce a "removed/disabled" claim. The common installed-host /
    # no-Allostat state falls through to the already-absent headline below.
    surfaces = []
    if c is not None and c.get("removed"):
        surfaces.append("Claude Code")
    if x is not None and (x.get("block_removed") or x.get("wrapper_removed")):
        surfaces.append("Codex")
    scope = " + ".join(surfaces) if surfaces else "Allostat"

    if _uninstall_incomplete(result):
        # Name every surface with a problem, not just the acted-on ones: a
        # refused Codex wrapper or a failed Claude removal belongs in the
        # INCOMPLETE headline even though nothing was successfully removed.
        troubled = []
        if c is not None and (
            c.get("removed") or c.get("failed")
            or any(
                isinstance(m, dict) and not m.get("removed", False)
                for m in c.get("mcp_remove", [])
            )
        ):
            troubled.append("Claude Code")
        if x is not None and (
            x.get("block_removed") or x.get("wrapper_removed") or x.get("wrapper_refused")
        ):
            troubled.append("Codex")
        scope_inc = " + ".join(troubled) if troubled else scope
        lines = [
            f"Allostat uninstall INCOMPLETE ({scope_inc}) — some active state could "
            "not be removed (see FAILED/WARNING below).",
            "",
        ]
    elif not surfaces:
        # Nothing was found to remove on the REQUESTED surface(s) (Sol gate-3
        # P3): say so plainly — never claim a removal that did not happen.
        # Idempotent no-op; callers exit 0.
        requested = {
            "claude": "Claude Code",
            "codex": "Codex",
            "both": "Claude Code + Codex",
        }.get(result.get("surface_requested"), "the requested surface(s)")
        lines = [
            f"Allostat was already absent for {requested} — nothing to remove "
            "(no changes made).",
            "",
        ]
    else:
        # Surface-aware (Sol gate-2): never claim a GLOBAL disable after a
        # single-surface uninstall. Name exactly what was disabled; note the
        # other surface is untouched when only one ran.
        if surfaces == ["Claude Code"]:
            head = ("Allostat uninstalled for Claude Code — the Claude wrapper is "
                    "removed and Allostat is disabled for Claude Code. (A separate "
                    "Codex install, if any, is untouched.)")
        elif surfaces == ["Codex"]:
            head = ("Allostat uninstalled for Codex — the Codex wrapper is removed "
                    "and Allostat is disabled for Codex. (A separate Claude Code "
                    "install, if any, is untouched.)")
        else:
            head = ("Allostat uninstalled — the wrapper(s) are removed and Allostat "
                    "is disabled for " + scope + ".")
        lines = [head, ""]
    c = result.get("claude")
    if c:
        lines.append("Claude Code:")
        for r in c["removed"]:
            lines.append(f"  removed: {r}")
        for f in c.get("failed", []):
            lines.append(f"  FAILED (still present, remove manually): {f}")
        for m in c.get("mcp_remove", []):
            if isinstance(m, dict):
                tag = "removed" if m.get("removed") else "NOT removed"
                lines.append(f"  mcp {tag}: {m['name']} ({m.get('detail', '')})")
            else:
                lines.append(f"  mcp:     {m}")
    x = result.get("codex")
    if x:
        lines.append("Codex:")
        lines.append(f"  config.toml block removed: {x['block_removed']} ({x['config']})")
        if x.get("wrapper_removed"):
            lines.append(f"  removed standalone wrapper: {x['wrapper']}")
        elif x.get("wrapper_refused"):
            lines.append(
                "  WARNING: standalone wrapper path was not a recognized "
                f"owned directory; kept: {x['wrapper']}"
            )
    if result.get("preserved"):
        lines.append("")
        lines.append("Your data was PRESERVED (not touched):")
        for p in result["preserved"]:
            lines.append(f"  kept: {p}")
        lines.append("  (also kept: your ALLOSTAT_MCP_TOKEN env var. Reinstall resumes from here.)")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Uninstall Allostat (surface-scoped; entry points pass their own surface).",
    )
    parser.add_argument(
        "--surface",
        choices=("claude", "codex", "both"),
        required=True,
        help="which install to remove; 'both' is the explicit full removal "
        "(never a default -- unknown ownership fails closed, Sol P2)",
    )
    args = parser.parse_args(argv)
    result = run(surface=args.surface)
    print(_format_summary(result))
    # Non-zero exit when active state survived removal, so the entry-point scripts
    # and any caller can detect a partial uninstall instead of trusting rc=0.
    return 1 if _uninstall_incomplete(result) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
