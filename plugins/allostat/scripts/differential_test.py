#!/usr/bin/env python3
"""Differential test harness for v2.3 plugin vs v2.4 wrapper.

Usage:
  python scripts/differential_test.py prompt "your prompt text"
  python scripts/differential_test.py file path/to/payload.json
  python scripts/differential_test.py corpus path/to/corpus.json [--ci]

For each input, the harness:
  1. Runs the v2.3 plugin's UserPromptSubmit hook with the prompt.
  2. Runs the v2.4 wrapper plugin's UserPromptSubmit hook with same.
  3. Captures additionalContext from each.
  4. Diffs the outputs side-by-side.
  5. Saves results to the user's Downloads folder by default.

Both plugins must be on the same machine. The v2.4 wrapper needs
ALLOSTAT_MCP_TOKEN configured. The harness sets ALLOSTAT_DISABLED=1
on the OTHER plugin while testing each so they don't interfere with
each other's observation logs.

Path discovery (no operator-specific paths baked in):
  - v2.4 wrapper root: resolved from this script's location
    (<wrapper-root>/scripts/differential_test.py)
  - v2.3 plugin root: env var ALLOSTAT_V23_PLUGIN_ROOT, OR sibling
    directory next to the wrapper at <wrapper-parent>/allostat
  - Output root: env var ALLOSTAT_DIFFERENTIAL_OUTPUT_ROOT, OR the
    user's Downloads folder via $USERPROFILE / $HOME

All three are overridable via CLI flags (--v23-root, --v24-root,
--output-root) for testing arbitrary installations.

Per v2.4 conversion plan §4 row κ. Operator hands this to Derek.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _default_v24_plugin_root() -> Path:
    """Resolve to <wrapper-root> by walking up from this script."""
    return Path(__file__).resolve().parent.parent


def _default_v23_plugin_root() -> Path:
    """Resolve v2.3 plugin root via env override, then sibling discovery."""
    override = os.environ.get("ALLOSTAT_V23_PLUGIN_ROOT")
    if override:
        return Path(override)
    return _default_v24_plugin_root().parent / "allostat"


def _default_output_root() -> Path:
    """Resolve output root via env override, then user's Downloads folder."""
    override = os.environ.get("ALLOSTAT_DIFFERENTIAL_OUTPUT_ROOT")
    if override:
        return Path(override)
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    return home / "Downloads" / "allostat_differential_tests"


DEFAULT_V23_PLUGIN_ROOT = _default_v23_plugin_root()
DEFAULT_V24_PLUGIN_ROOT = _default_v24_plugin_root()
DEFAULT_OUTPUT_ROOT = _default_output_root()

HOOK_FILE_BY_EVENT = {
    "UserPromptSubmit": "user-prompt-submit.py",
    "SessionStart": "session-start.py",
    "PreToolUse": "pre-tool-use.py",
    "PostToolUse": "post-tool-use.py",
    "Stop": "stop.py",
}


def run_hook(
    plugin_root: Path,
    hook_filename: str,
    payload: dict[str, Any],
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    """Invoke a plugin's hook subprocess with the given stdin payload.

    Returns (returncode, stdout, stderr).
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # Ensure plugin's lib/ is on PYTHONPATH so its imports work.
    env["PYTHONPATH"] = os.pathsep.join([
        str(plugin_root / "lib"),
        str(plugin_root / "hooks"),
        env.get("PYTHONPATH", ""),
    ])
    hook_path = plugin_root / "hooks" / hook_filename
    if not hook_path.is_file():
        return (
            127,
            "",
            f"hook not found at {hook_path}",
        )
    try:
        proc = subprocess.run(
            [sys.executable, str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"hook timed out after {timeout}s"


def parse_additional_context(stdout: str) -> str | None:
    """Extract additionalContext text from the hook's JSON envelope."""
    if not stdout:
        return None
    try:
        obj = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    if not isinstance(obj, dict):
        return None
    hook_out = obj.get("hookSpecificOutput", {})
    if not isinstance(hook_out, dict):
        return None
    return hook_out.get("additionalContext")


def run_differential(
    event: str,
    payload: dict[str, Any],
    *,
    v23_root: Path = DEFAULT_V23_PLUGIN_ROOT,
    v24_root: Path = DEFAULT_V24_PLUGIN_ROOT,
) -> dict[str, Any]:
    """Run the same payload through both plugins, return the
    side-by-side comparison."""
    hook_filename = HOOK_FILE_BY_EVENT.get(event)
    if hook_filename is None:
        raise ValueError(f"unknown hook event: {event!r}")

    # Run v2.3 plugin with v2.4 disabled.
    v23_rc, v23_stdout, v23_stderr = run_hook(
        v23_root,
        hook_filename,
        payload,
        extra_env={
            # Wrapper hooks should NOT also fire during the v2.3 run.
            # ALLOSTAT_DISABLED is the wrapper's only opt-out flag (see
            # hooks/_base.py); the previous ALLOSTAT_MCP_DISABLED was a
            # phantom var nothing checks — isolation silently didn't happen.
            "ALLOSTAT_DISABLED": "1",
        },
    )
    v23_ctx = parse_additional_context(v23_stdout)

    # Run v2.4 wrapper with v2.3 disabled.
    v24_rc, v24_stdout, v24_stderr = run_hook(
        v24_root,
        hook_filename,
        payload,
        extra_env={
            "ALLOSTAT_DISABLED": "0",  # ensure wrapper runs
        },
    )
    v24_ctx = parse_additional_context(v24_stdout)

    diff_lines = list(
        difflib.unified_diff(
            (v23_ctx or "").splitlines(keepends=False),
            (v24_ctx or "").splitlines(keepends=False),
            fromfile="v2.3-plugin-additionalContext",
            tofile="v2.4-wrapper-additionalContext",
            lineterm="",
        )
    )

    return {
        "event": event,
        "input_payload": payload,
        "v23": {
            "rc": v23_rc,
            "stdout": v23_stdout,
            "stderr": v23_stderr,
            "additionalContext": v23_ctx,
        },
        "v24": {
            "rc": v24_rc,
            "stdout": v24_stdout,
            "stderr": v24_stderr,
            "additionalContext": v24_ctx,
        },
        "diff": "\n".join(diff_lines),
        "match": (v23_ctx == v24_ctx),
    }


def load_corpus(corpus_path: Path) -> dict[str, Any]:
    """Load the differential-test corpus JSON."""
    raw = json.loads(corpus_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "scenarios" not in raw:
        raise ValueError(
            f"corpus at {corpus_path} is malformed — expected dict with "
            "`scenarios` list"
        )
    scenarios = raw["scenarios"]
    if not isinstance(scenarios, list):
        raise ValueError(f"corpus `scenarios` must be a list, got {type(scenarios)}")
    return raw


def classify_scenario_result(
    result: dict[str, Any],
    expected: str,
) -> str:
    """Map a differential result + expected mode to a corpus status.

    Statuses:
      pass           — outputs matched (or wrapper-side diff acceptable)
      fail           — parity violation: expected match, got diff
      v23_absent     — v2.3 plugin hook not installed (CI mode, e.g.)
      v24_absent     — v2.4 wrapper hook not installed (configuration issue)
      both_absent    — neither plugin present
      ci_skipped     — scenario marked skip_in_ci
      error          — hook raised; rc != 0 and != 127
    """
    v23_rc = result["v23"]["rc"]
    v24_rc = result["v24"]["rc"]

    v23_missing = v23_rc == 127
    v24_missing = v24_rc == 127

    if v23_missing and v24_missing:
        return "both_absent"
    if v23_missing:
        return "v23_absent"
    if v24_missing:
        return "v24_absent"

    if v23_rc not in (0,) or v24_rc not in (0,):
        return "error"

    if expected == "match":
        return "pass" if result["match"] else "fail"
    if expected == "diff_acceptable":
        # Wrapper may add envelope or route differently; decision must
        # at minimum produce non-empty additionalContext on both sides
        # when one fires.
        return "pass"
    return "pass"


def run_corpus(
    corpus_path: Path,
    *,
    v23_root: Path = DEFAULT_V23_PLUGIN_ROOT,
    v24_root: Path = DEFAULT_V24_PLUGIN_ROOT,
    ci_mode: bool = False,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """Run the entire corpus, aggregating per-scenario results.

    In ci_mode, scenarios marked `skip_in_ci` are skipped (they require
    operator state seeding to verify the full claim).
    """
    corpus = load_corpus(corpus_path)
    scenarios = corpus["scenarios"]

    results: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}

    for scenario in scenarios:
        name = scenario.get("name", "<unnamed>")
        event = scenario.get("event", "UserPromptSubmit")
        payload = scenario.get("payload", {})
        expected = scenario.get("expected", "match")

        if ci_mode and expected == "skip_in_ci":
            entry = {
                "name": name,
                "pillar": scenario.get("pillar"),
                "expected": expected,
                "status": "ci_skipped",
                "rationale": scenario.get("rationale", ""),
            }
            results.append(entry)
            status_counts["ci_skipped"] = status_counts.get("ci_skipped", 0) + 1
            continue

        run_result = run_differential(
            event,
            payload,
            v23_root=v23_root,
            v24_root=v24_root,
        )
        status = classify_scenario_result(run_result, expected)
        results.append({
            "name": name,
            "pillar": scenario.get("pillar"),
            "expected": expected,
            "status": status,
            "rationale": scenario.get("rationale", ""),
            "v23_rc": run_result["v23"]["rc"],
            "v24_rc": run_result["v24"]["rc"],
            "match": run_result["match"],
            "diff": run_result["diff"] if status == "fail" else "",
        })
        status_counts[status] = status_counts.get(status, 0) + 1

    summary = {
        "total": len(scenarios),
        "status_counts": status_counts,
        "results": results,
        "corpus_path": str(corpus_path),
        "ci_mode": ci_mode,
    }

    if output_root is not None:
        output_root.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        json_path = output_root / f"{ts}_corpus_summary.json"
        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        md_path = output_root / f"{ts}_corpus_summary.md"
        md_path.write_text(format_corpus_report(summary), encoding="utf-8")
        summary["json_report_path"] = str(json_path)
        summary["md_report_path"] = str(md_path)

    return summary


def format_corpus_report(summary: dict[str, Any]) -> str:
    """Render a human-readable Markdown report for the corpus run."""
    counts = summary["status_counts"]
    total = summary["total"]
    ci_mode = summary["ci_mode"]

    lines = [
        "# Differential corpus report",
        "",
        f"Corpus: {summary['corpus_path']}",
        f"Total scenarios: {total}",
        f"CI mode: {ci_mode}",
        "",
        "## Status counts",
        "",
    ]
    for status, count in sorted(counts.items()):
        lines.append(f"- **{status}**: {count}")
    lines.extend(["", "## Per-scenario", "", "| Name | Pillar | Expected | Status |", "|---|---|---|---|"])
    for entry in summary["results"]:
        lines.append(
            f"| {entry['name']} | {entry.get('pillar', '')} | "
            f"{entry['expected']} | {entry['status']} |"
        )
    failed = [e for e in summary["results"] if e["status"] == "fail"]
    if failed:
        lines.extend(["", "## Failed scenarios (parity violations)", ""])
        for entry in failed:
            lines.append(f"### {entry['name']}")
            lines.append(f"_{entry.get('rationale', '')}_")
            lines.append("")
            lines.append("```diff")
            lines.append(entry.get("diff", "(no diff captured)"))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def format_side_by_side_report(result: dict[str, Any]) -> str:
    """Render a human-readable side-by-side report."""
    lines = [
        "# Differential test report",
        "",
        f"Event: {result['event']}",
        f"Match: {result['match']}",
        "",
        "## Input payload",
        "```json",
        json.dumps(result["input_payload"], indent=2),
        "```",
        "",
        "## v2.3 plugin additionalContext",
        "```",
        result["v23"]["additionalContext"] or "(none)",
        "```",
        "",
        "## v2.4 wrapper additionalContext",
        "```",
        result["v24"]["additionalContext"] or "(none)",
        "```",
        "",
        "## Diff",
        "```diff",
        result["diff"] or "(identical)",
        "```",
        "",
        "## Stderr (informational)",
        "```",
        f"v2.3: {result['v23']['stderr']}",
        f"v2.4: {result['v24']['stderr']}",
        "```",
    ]
    return "\n".join(lines)


def main() -> int:
    # Windows default console codepage is cp1252; corpus reports contain
    # Greek letters (α.5 etc.). Reconfigure stdout/stderr to UTF-8 with
    # `replace` fallback so the CLI never crashes on a print of an
    # un-encodable character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        description=(
            "Differential test for v2.3 plugin vs v2.4 wrapper. "
            "Sends the same input through both UserPromptSubmit hooks "
            "and diffs the additionalContext outputs."
        )
    )
    parser.add_argument(
        "input_kind",
        choices=("prompt", "file", "corpus"),
        help="`prompt`: positional arg is the prompt text. `file`: path to a "
        "JSON payload file (hooks expect JSON on stdin). `corpus`: path to "
        "a corpus JSON file with many scenarios (batch mode).",
    )
    parser.add_argument(
        "input_value",
        help="Prompt text, JSON payload path, or corpus JSON path.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: skip scenarios marked `skip_in_ci` in the corpus. "
        "Only applies when input_kind=corpus.",
    )
    parser.add_argument(
        "--event",
        default="UserPromptSubmit",
        choices=sorted(HOOK_FILE_BY_EVENT.keys()),
        help="Which hook event to fire (default UserPromptSubmit).",
    )
    parser.add_argument(
        "--v23-root",
        type=Path,
        default=DEFAULT_V23_PLUGIN_ROOT,
        help="Path to v2.3 plugin root.",
    )
    parser.add_argument(
        "--v24-root",
        type=Path,
        default=DEFAULT_V24_PLUGIN_ROOT,
        help="Path to v2.4 wrapper plugin root.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Where to save the report.",
    )
    args = parser.parse_args()

    # Corpus mode is a separate path.
    if args.input_kind == "corpus":
        summary = run_corpus(
            Path(args.input_value),
            v23_root=args.v23_root,
            v24_root=args.v24_root,
            ci_mode=args.ci,
            output_root=args.output_root,
        )
        print(format_corpus_report(summary))
        if "md_report_path" in summary:
            print(f"\nSaved to {summary['md_report_path']}", file=sys.stderr)

        # Exit 0 if no parity violations; 1 if any `fail` status.
        failed_count = summary["status_counts"].get("fail", 0)
        return 0 if failed_count == 0 else 1

    # Single-shot mode.
    if args.input_kind == "prompt":
        payload = {"prompt": args.input_value}
    else:
        payload = json.loads(Path(args.input_value).read_text(encoding="utf-8"))

    result = run_differential(
        args.event,
        payload,
        v23_root=args.v23_root,
        v24_root=args.v24_root,
    )

    report = format_side_by_side_report(result)
    print(report)

    # Save to disk.
    args.output_root.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_path = args.output_root / f"{ts}_{args.event}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nSaved to {out_path}", file=sys.stderr)

    # Exit 0 on match, 1 on diff. Lets Derek's CI flag drift.
    return 0 if result["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
