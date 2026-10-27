"""Shared provenance logic for the topology + rollout detectors (F1+F2, 2026-05-29).

Background: volume-control's `topology_change_unrecorded` / `rollout_unrecorded`
nudges fired on the agent's own dev-cycle commands (temp-dir `mkdir`/`rm`, deploy
scripts, `git push`) and then demanded a MANUAL `/allostat record-migration` to go
quiet. That's two problems: imprecise firing, and a homeostatic shape (manual reset)
in a product whose thesis is allostatic self-regulation.

F1 — precision denylist: a structural/deploy command that operates purely on
temp/scratch paths is never a project migration. Suppress detection entirely.

F2 — auto-acknowledge: when a detection IS emitted, the regulator records the
migration on the operator's behalf (emits `migration_recorded`) so volume-control
self-clears instead of nagging. The audit observation (`*_detected`) is still
written; only the manual-click demand goes away. `/allostat record-migration`
remains as an explicit opt-in override, and `ALLOSTAT_MIGRATION_AUTO_ACK=0`
restores the old nag for operators who want to record by hand.

(The fuller allostatic redesign — surface only genuinely-unexplained structural
change, stay silent on attributable change — is post-launch. This is the minimum:
stop the spurious nag + stop demanding a manual click.)
"""
from __future__ import annotations

import os
import re
from typing import Any

# Temp / scratch / build-artifact path markers. A topology or rollout command
# whose target is one of these is dev/agent tooling, not a project migration.
_DENYLIST_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"/tmp/", re.IGNORECASE),
    re.compile(r"[\\/]Temp[\\/]", re.IGNORECASE),
    re.compile(r"AppData[\\/]Local[\\/]Temp", re.IGNORECASE),
    re.compile(r"/var/folders/", re.IGNORECASE),          # macOS temp
    re.compile(r"\$TMPDIR\b|\$TEMP\b|%TEMP%", re.IGNORECASE),
    re.compile(r"__pycache__|\.pytest_cache|\.ruff_cache", re.IGNORECASE),
)


def _denylist_extra() -> tuple[re.Pattern, ...]:
    """Operator-extensible denylist via ALLOSTAT_MIGRATION_DENYLIST
    (semicolon-separated regexes). Invalid entries skipped silently."""
    raw = os.environ.get("ALLOSTAT_MIGRATION_DENYLIST", "")
    if not raw:
        return ()
    out: list[re.Pattern] = []
    for pat_str in raw.split(";"):
        pat_str = pat_str.strip()
        if not pat_str:
            continue
        try:
            out.append(re.compile(pat_str, re.IGNORECASE))
        except re.error:
            continue
    return tuple(out)


def is_denylisted(command: str | None) -> bool:
    """F1: True if the command targets temp/scratch/build paths (not a real
    project migration). Suppresses detector firing on dev-cycle noise."""
    if not command or not isinstance(command, str):
        return False
    for pat in _DENYLIST_PATTERNS + _denylist_extra():
        if pat.search(command):
            return True
    return False


def auto_ack_enabled() -> bool:
    """Honor ALLOSTAT_MIGRATION_AUTO_ACK (default on). Set 0|false|no|off to
    restore the manual-record nag (operator wants to record by hand)."""
    raw = os.environ.get("ALLOSTAT_MIGRATION_AUTO_ACK", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def auto_acknowledge(state_dir, detector: str, kind: str, command: str | None) -> bool:
    """F2: emit a `migration_recorded` observation so volume-control's consumer
    sees the detected change as acknowledged and does NOT nag. Best-effort —
    never raises. Returns True if an ack was written."""
    if not auto_ack_enabled():
        return False
    from secret_redaction import redact_secrets  # noqa: E402
    details: dict[str, Any] = {
        "source": f"auto_ack:{detector}",
        "kind": kind,
        # Redact secrets before persisting (ultraswarm H-6, 2026-07-07).
        "command_excerpt": redact_secrets(command or "")[:300],
    }
    try:
        from local_state import append_observation  # noqa: E402
        return append_observation(state_dir, "migration_recorded", details=details)
    except Exception:
        return False
