#!/usr/bin/env python3
"""allostat_state.py - derive Allostat's real state from ground truth.

Built 2026-06-19 after a session burned an hour reasoning from stale STATE.md,
handoffs, and dev_patch_log. The rule this enforces: never report a derivable
fact from prose - derive it from the repos (and, with --server, the live
servers) every time.

Components are DISCOVERED by scanning, never configured from a list (a hardcoded
list is exactly what once reported a dead repo as a live component). A directory
is a LIVE component if it is a git work tree with an 'origin' remote AND a commit
within --recent-days; otherwise it is LEGACY and is called out as such.

Usage:
  python allostat_state.py [--root <dev-root>] [--recent-days 30] [--server]

--server additionally probes the MCP deploy state over SSH (staging vs prod
/healthz + a source diff of the deployed dirs). Network stays behind the flag so
the base command runs offline.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path, PureWindowsPath


def _git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except Exception:
        return ""


def classify(comp: dict, recent_days: int) -> str:
    """Bucket a discovered directory into ROOT / LIVE / LEGACY.

    A directory carrying ALLOSTAT_PROJECT_ROOT.md is the Allostat project root /
    memory & continuity hub — NOT a deployable code component, even if it has a
    git remote. It must never be reported as LEGACY/dead (the bug this fixes: a
    marker-bearing hub with old git history and no remote was stamped LEGACY by
    the old remote+recency heuristic every run). Otherwise: LIVE if it has an
    origin remote AND a commit within recent_days, else LEGACY. Derived from a
    marker IN the directory, not a hardcoded list — the same ground-truth
    philosophy as discovery itself.
    """
    if comp.get("is_root"):
        return "ROOT"
    age = comp.get("age_days")
    if comp.get("remote") and age is not None and age <= recent_days:
        return "LIVE"
    return "LEGACY"


def _component(repo: Path, recent_days: int) -> dict:
    remote = _git(repo, "remote", "get-url", "origin")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(repo, "log", "-1", "--format=%h")
    cdate = _git(repo, "log", "-1", "--format=%cs")        # YYYY-MM-DD
    cts = _git(repo, "log", "-1", "--format=%ct")          # unix ts
    dirty_n = len([ln for ln in _git(repo, "status", "--porcelain").splitlines() if ln.strip()])
    ahead = behind = "-"
    if remote and branch and branch != "HEAD":
        ab = _git(repo, "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
        parts = ab.split()
        if len(parts) == 2:
            behind, ahead = parts[0], parts[1]
    age_days = None
    if cts:
        try:
            age_days = (time.time() - int(cts)) / 86400
        except ValueError:
            age_days = None
    is_root = (repo / "ALLOSTAT_PROJECT_ROOT.md").is_file()
    comp = {
        "name": repo.name, "remote": bool(remote), "branch": branch or "-",
        "head": head or "-", "cdate": cdate or "-", "age_days": age_days,
        "dirty": dirty_n, "ahead": ahead, "behind": behind, "is_root": is_root,
    }
    comp["state"] = classify(comp, recent_days)
    comp["live"] = comp["state"] == "LIVE"  # kept for back-compat callers
    return comp


def discover(root: Path, recent_days: int) -> list[dict]:
    comps = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / ".git").exists() and "allostat" in d.name.lower():
            comps.append(_component(d, recent_days))
    order = {"LIVE": 0, "ROOT": 1, "LEGACY": 2}
    comps.sort(key=lambda c: (order.get(c["state"], 3), c["name"]))
    return comps


def print_table(comps: list[dict]) -> None:
    hdr = (f"{'COMPONENT':24} {'STATE':7} {'BRANCH':28} {'HEAD':9} "
           f"{'COMMIT':11} {'AGE':5} {'DIRTY':6} A/B-vs-origin")
    print(hdr)
    print("-" * len(hdr))
    for c in comps:
        state = c["state"]
        age = f"{int(c['age_days'])}d" if c["age_days"] is not None else "-"
        dirty = str(c["dirty"]) if c["dirty"] else "clean"
        ab = f"+{c['ahead']}/-{c['behind']}" if c["remote"] else "(no remote)"
        print(f"{c['name']:24} {state:7} {c['branch']:28.28} {c['head']:9} "
              f"{c['cdate']:11} {age:5} {dirty:6} {ab}")
    roots = [c["name"] for c in comps if c["state"] == "ROOT"]
    legacy = [c["name"] for c in comps if c["state"] == "LEGACY"]
    if roots:
        print()
        print("PROJECT ROOT / memory & continuity hub (NOT a deployable "
              f"component, NOT dead): {', '.join(roots)}")
    if legacy:
        print()
        print(f"LEGACY (do NOT treat as a live component): {', '.join(legacy)}")


def _resolve_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        raise FileNotFoundError("bash executable not found")
    return bash


def _path_for_bash(
    path: Path | PureWindowsPath,
    bash_executable: str,
    *,
    platform: str | None = None,
) -> str:
    """Translate a host path for the concrete bash that will consume it."""
    platform = platform or os.name
    if platform != "nt":
        return Path(path).as_posix()

    windows_path = PureWindowsPath(path)
    drive = windows_path.drive
    if len(drive) != 2 or drive[1] != ":":
        raise ValueError(f"unsupported Windows path for bash: {windows_path}")
    drive_letter = drive[0].lower()
    tail = "/".join(windows_path.parts[1:])
    bash_name = str(bash_executable).replace("\\", "/").lower()
    is_wsl = (
        bash_name.endswith("/wsl.exe")
        or "/windows/system32/bash.exe" in bash_name
        or "/microsoft/windowsapps/bash.exe" in bash_name
    )
    prefix = f"/mnt/{drive_letter}" if is_wsl else f"/{drive_letter}"
    return f"{prefix}/{tail}" if tail else prefix


def probe_server(mcp_root: Path | None = None) -> bool:
    """Probe live deploy state over SSH. FAIL-CLOSED (advisor 2026-06-26): returns
    False (→ caller exits nonzero) whenever the probe cannot ACTUALLY verify deploy
    state — missing deploy config, SSH error, or no healthz output. A gate that
    passes on its own failure is worse than no gate; the prior version exited 0 and
    printed 'skipping', a false all-clear. Returns True only when the SSH diff ran."""
    mcp = mcp_root
    if mcp is None:
        for cand in (
            Path(__file__).resolve().parents[2] / "server",        # monorepo
            Path(__file__).resolve().parents[2] / "allostat-mcp",  # split legacy
            Path.home() / "dev" / "allostat-mcp",
        ):
            if (cand / "deploy" / "_vps_common.sh").exists():
                mcp = cand
                break
    if mcp is None:
        print("\n[--server] FAIL-CLOSED: deploy/_vps_common.sh not found under /server "
              "or allostat-mcp — cannot probe deploy state.")
        return False
    try:
        bash = _resolve_bash()
        common = _path_for_bash(mcp / "deploy" / "_vps_common.sh", bash)
    except Exception as e:
        print(f"\n[--server] FAIL-CLOSED: deploy probe path setup failed: {e}")
        return False
    script = (
        'set -e\n'
        f"source {shlex.quote(common)}\n"
        'sh="ssh -i $VPS_KEY -o ConnectTimeout=8 -o BatchMode=yes $VPS_USER@$VPS_HOST"\n'
        'echo "  staging :8001 -> $($sh \'curl -s -m5 http://127.0.0.1:8001/healthz\')"\n'
        'echo "  prod    :8000 -> $($sh \'curl -s -m5 http://127.0.0.1:8000/healthz\')"\n'
        # CRLF-normalize both sides before diff: Linux prod is LF, so a naive diff
        # reports every file as differing (the false positive caught this session).
        'd=$($sh \'diff -rq --exclude=.venv --exclude=__pycache__ --exclude="*.sqlite*" '
        '<(sudo find /opt/allostat-server-staging -name "*.py" -exec md5sum {} +) '
        '<(sudo find /opt/allostat-server -name "*.py" -exec md5sum {} +) 2>/dev/null\') || true\n'
        'if [ -z "$d" ]; then echo "  deploy: staging == prod (NOTHING pending to promote)"; '
        'else echo "  deploy: staging AHEAD of prod (pending promotion — inspect)"; fi\n'
    )
    print("\n=== DEPLOY (ground truth - staging vs prod) ===")
    print("  'ahead' = staging-DEPLOYED vs prod-DEPLOYED (a REAL pending promotion).")
    try:
        r = subprocess.run(
            [bash, "-c", script], capture_output=True, text=True, timeout=90
        )
    except Exception as e:
        print(f"\n[--server] FAIL-CLOSED: deploy probe could not execute: {e}")
        return False
    if r.stdout.strip():
        print(r.stdout.rstrip())
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        print(f"\n[--server] FAIL-CLOSED: deploy probe exited {r.returncode}: {last}")
        return False
    if "staging :8001 ->" not in r.stdout:
        print("\n[--server] FAIL-CLOSED: no healthz output — SSH likely failed silently.")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive Allostat state from ground truth.")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--recent-days", type=int, default=30)
    ap.add_argument("--server", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"=== ALLOSTAT STATE (ground truth, derived {stamp}) ===")
    print(f"root: {root}  recent-days: {a.recent_days}\n")
    comps = discover(root, a.recent_days)
    if not comps:
        print("No allostat git repos found under root.")
        return 1
    print_table(comps)
    if a.server:
        if not probe_server():
            print("\n(SERVER PROBE FAILED — deploy state UNVERIFIED. Fix the probe / SSH "
                  "before trusting any deploy gate built on this.)")
            return 2
    print("\n(DERIVED from git + servers. Do not hand-edit a state file from memory.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
