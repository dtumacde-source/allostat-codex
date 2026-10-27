"""Record-migration backend — emits the `migration_recorded` observation
that silences volume-control's topology_change_unrecorded / rollout_unrecorded
nudge.

Background: the topology-change + rollout detectors emit
`topology_change_detected` / `rollout_detected` observations. Volume-control
then nags `-> /allostat record-migration` until a record-migration follow-up
event lands. Before this module, NO shipped path produced that follow-up
event — the consumer existed, the nudge string existed, but no producer did,
so the nudge could fire indefinitely with no way to clear it. This is the
producer the consumer was always waiting for.

The server-side consumer (`volume_control.detect_topology_change_unrecorded`
and `detect_rollout_unrecorded`) silences on either `migration_recorded` or
`allostat_record_migration`. We emit `migration_recorded` (the canonical
name) carrying an optional operator note + a source tag.

Client-side only: the event lands in the operator's `observations.jsonl`,
which the wrapper already ships to the server in the dispatch excerpt. No
server change is required.
"""
from __future__ import annotations

import os
import re
import sys
import subprocess
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_state import (  # noqa: E402
    append_observation,
    resolve_project_root,
    resolve_state_dir,
)

# The event the server-side volume-control consumer accepts to silence the
# topology_change_unrecorded / rollout_unrecorded nudge. Pinned as a constant
# so the regression test can assert the producer matches the consumer
# contract (server side accepts {"migration_recorded", "allostat_record_migration"}).
MIGRATION_RECORDED_EVENT = "migration_recorded"
MEMORY_UPSERT_MCP_BRIDGE = "--memory-upsert-mcp"


def _sanitized_refresh_failure(exc: Exception) -> str:
    """Keep bridge diagnostics actionable without printing a private home path."""
    detail = " ".join(str(exc).split())
    home_values = {
        str(Path.home()),
        os.environ.get("HOME"),
        os.environ.get("USERPROFILE"),
    }
    for raw_home in home_values:
        if raw_home:
            detail = re.sub(re.escape(raw_home), "<user-home>", detail, flags=re.I)
    if not detail:
        detail = type(exc).__name__
    if len(detail) > 240:
        detail = detail[:237] + "..."
    return detail


def _run_memory_upsert_mcp() -> int:
    """Compatibility bridge for upgrades from the pre-M1 durable launcher.

    ``allostat-command ... record-migration`` predates Memory Slice 1, so an
    already activated install can reach this current-snapshot code before
    SessionStart.  Refresh the durable dispatchers transactionally, then hand
    the inherited stdio stream to the real local MCP server.  A refresh refusal
    is loud but does not make the current server unavailable: the fixed bridge
    itself remains safely rooted by the old command dispatcher's validated
    current-plugin resolution.
    """
    try:
        import activate

        activate.refresh_codex_dispatchers(
            python_token=activate.select_activation_python_token()
        )
    except Exception as exc:
        reason = _sanitized_refresh_failure(exc)
        print(
            "allostat local memory: durable dispatcher refresh was refused "
            f"because {reason}; continuing through the upgrade bridge",
            file=sys.stderr,
        )
    server = (Path(__file__).resolve().parent / "topical_memory_mcp.py").resolve()
    if not server.is_file():
        print(f"allostat local memory: server missing: {server}", file=sys.stderr)
        return 3
    try:
        completed = subprocess.run(
            [sys.executable, "-P", str(server), "--harness", "codex"]
        )
    except OSError as exc:
        print(f"allostat local memory: server failed to start: {exc}", file=sys.stderr)
        return 4
    return int(completed.returncode)


def record_migration(
    state_dir: Path,
    *,
    note: str | None = None,
    source: str = "operator_command",
) -> bool:
    """Append a `migration_recorded` observation to the state dir.

    Returns True on success. Best-effort: append_observation swallows
    filesystem errors and returns False rather than raising.
    """
    details: dict[str, Any] = {"source": source}
    if note:
        details["note"] = note
    return append_observation(state_dir, MIGRATION_RECORDED_EVENT, details=details)


def _cli(argv: list[str] | None = None) -> int:
    """python "<resolved-plugin-root>/lib/record_migration.py" [--note "..."]

    The plugin root is resolved at runtime by `/allostat record-migration`
    (see commands/allostat.md) — never via `$ALLOSTAT_PLUGIN_DIR` or
    `${CLAUDE_PLUGIN_ROOT}` (RB-8 / anthropics/claude-code#9354; neither
    resolves in a tool-spawned shell / command-doc body text).

    Resolves the active project's state dir from cwd and records a
    migration. Prints a one-line confirmation.
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list == [MEMORY_UPSERT_MCP_BRIDGE]:
        return _run_memory_upsert_mcp()

    import argparse

    parser = argparse.ArgumentParser(prog="record_migration")
    parser.add_argument(
        "--note",
        default=None,
        help="optional free-text note describing the migration",
    )
    parser.add_argument(
        "--source",
        default="operator_command",
        help="who/what recorded it (default: operator_command)",
    )
    args = parser.parse_args(args_list)

    project_root = resolve_project_root() or Path.cwd()
    state_dir = resolve_state_dir(project_root)

    ok = record_migration(state_dir, note=args.note, source=args.source)
    if ok:
        print(f"migration recorded -> {state_dir / 'observations.jsonl'}")
        if args.note:
            print(f"  note: {args.note}")
        return 0
    print(
        f"ERROR: could not write migration record to {state_dir} "
        "(filesystem error)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
