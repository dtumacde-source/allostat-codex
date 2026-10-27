"""Rollout-detected detector — PATCH-193 (Phase 3.3b).

Producer for the `rollout_detected` observation event that
volume-control's `detect_rollout_unrecorded` consumes. Pre-PATCH-193
this producer didn't exist; volume-control's rollout branch was
structurally unable to fire.

Strategy: classify each Bash command invoked through PreToolUse against
a curated list of high-precision deploy-shaped command patterns. When
a match fires, append a `rollout_detected` observation with the
matched command (truncated for storage discipline) and the matched
pattern's `kind` tag.

Precision-over-recall design: each pattern is chosen to fire only on
commands that are unambiguously deploys. We'd rather miss a deploy
that uses an unusual command line than false-fire on a routine `git
push` to a dev branch. Operator can add custom patterns via the
`ALLOSTAT_ROLLOUT_PATTERNS` env var (semicolon-separated regex list).

Server-side `_detect_rollout_unrecorded` reads `event="rollout_detected"`
observations and walks forward looking for a `migration_recorded` /
`allostat_record_migration` event to silence the nudge. The matching
closer slash-command was retired in PATCH-140 — operator can re-add as
a separate work item if the nudge becomes too persistent.
"""
from __future__ import annotations

import os
import re
from typing import Any


# ============================================================================
# Pattern catalog
# ============================================================================
#
# Each entry: (kind, compiled_regex). `kind` is a short tag stored in the
# observation details for downstream classification. Patterns are anchored
# at word boundaries (`\b`) and lowercased-matched (re.IGNORECASE) to
# minimize false positives from substrings like `do_git_pushdown_X`.
#
# Adding to this list ships to all operators via the next bundle.
# Operator-specific patterns belong in ALLOSTAT_ROLLOUT_PATTERNS instead.


_BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # Git pushes — anchored at command start (after start-of-line, `&&`,
    # `||`, `;`, `|`, newline). Prevents false-fire on quoted text like
    # `echo 'git push to main'`. Force-flag pattern checked BEFORE
    # main-branch pattern so `git push --force-with-lease origin main`
    # classifies as the force-push variant (more specific).
    ("git_push_force", re.compile(r"(?:^|[;&|\n])\s*git\s+push\b[^|;&]*(--force\b|-f\b|--force-with-lease\b)", re.IGNORECASE)),
    ("git_push_tags", re.compile(r"(?:^|[;&|\n])\s*git\s+push\s+(?:--tags|origin\s+(?:--tags|v\d+(\.\d+)*))\b", re.IGNORECASE)),
    ("git_push_main", re.compile(r"(?:^|[;&|\n])\s*git\s+push\b[^|;&]*\b(main|master|prod|production|deploy|live|release)\b", re.IGNORECASE)),

    # VC2 audit fix (2026-05-25, S1): every pattern below now uses the
    # command-start anchor `(?:^|[;&|\n])\s*` to prevent false-fires on
    # quoted text like `echo 'wrangler publish would deploy'`. Previously
    # bare `\b` allowed any substring boundary to match; now the verb must
    # appear at a real shell command boundary. The git_push_* patterns
    # above already had this anchor and served as the template.

    # JS / web platform deploy tools.
    ("wrangler_publish", re.compile(r"(?:^|[;&|\n])\s*wrangler\s+(publish|deploy)\b", re.IGNORECASE)),
    ("vercel_deploy", re.compile(r"(?:^|[;&|\n])\s*vercel\s+(deploy|--prod\b)", re.IGNORECASE)),
    ("netlify_deploy", re.compile(r"(?:^|[;&|\n])\s*netlify\s+deploy\b[^|;&]*--prod", re.IGNORECASE)),
    ("firebase_deploy", re.compile(r"(?:^|[;&|\n])\s*firebase\s+deploy\b", re.IGNORECASE)),

    # Package publishing.
    ("npm_publish", re.compile(r"(?:^|[;&|\n])\s*npm\s+publish\b", re.IGNORECASE)),
    ("pip_upload_twine", re.compile(r"(?:^|[;&|\n])\s*twine\s+upload\b", re.IGNORECASE)),
    ("cargo_publish", re.compile(r"(?:^|[;&|\n])\s*cargo\s+publish\b", re.IGNORECASE)),
    ("gem_push", re.compile(r"(?:^|[;&|\n])\s*gem\s+push\b", re.IGNORECASE)),
    ("docker_push", re.compile(r"(?:^|[;&|\n])\s*docker\s+push\b", re.IGNORECASE)),

    # Cloud-platform deploys.
    ("aws_s3_sync", re.compile(r"(?:^|[;&|\n])\s*aws\s+s3\s+(sync|cp)\b[^|;&]*\bs3://", re.IGNORECASE)),
    ("aws_lambda_update", re.compile(r"(?:^|[;&|\n])\s*aws\s+lambda\s+update-function-code\b", re.IGNORECASE)),
    ("aws_eb_deploy", re.compile(r"(?:^|[;&|\n])\s*eb\s+deploy\b", re.IGNORECASE)),
    ("gcloud_app_deploy", re.compile(r"(?:^|[;&|\n])\s*gcloud\s+app\s+deploy\b", re.IGNORECASE)),
    ("gcloud_functions_deploy", re.compile(r"(?:^|[;&|\n])\s*gcloud\s+functions\s+deploy\b", re.IGNORECASE)),
    ("gcloud_run_deploy", re.compile(r"(?:^|[;&|\n])\s*gcloud\s+run\s+deploy\b", re.IGNORECASE)),
    ("azure_webapp_deploy", re.compile(r"(?:^|[;&|\n])\s*az\s+webapp\s+deploy\b", re.IGNORECASE)),

    # Kubernetes / orchestration.
    ("kubectl_apply", re.compile(r"(?:^|[;&|\n])\s*kubectl\s+(apply|rollout)\b", re.IGNORECASE)),
    ("helm_upgrade", re.compile(r"(?:^|[;&|\n])\s*helm\s+(install|upgrade)\b", re.IGNORECASE)),
    ("terraform_apply", re.compile(r"(?:^|[;&|\n])\s*terraform\s+apply\b", re.IGNORECASE)),

    # Allostat-specific deploy patterns (matches the project's own scripts).
    # These are script paths, not command verbs — they get invoked via
    # `sudo /path/to/script.sh` or `bash scripts/script.sh`, so the script
    # name isn't at the command's leading edge. Keep `\b...\b` for word-
    # boundary anchoring rather than command-start anchoring (VC2 audit
    # fix exemption — applies only to script-path patterns).
    ("allostat_promote_staging", re.compile(r"\bpromote_staging_to_prod\.sh\b", re.IGNORECASE)),
    ("allostat_deploy_staging", re.compile(r"\bdeploy_staging\.sh\b", re.IGNORECASE)),
)


def _custom_patterns_from_env() -> tuple[tuple[str, re.Pattern], ...]:
    """Read ALLOSTAT_ROLLOUT_PATTERNS env var for operator-specific
    additions. Format: semicolon-separated regex strings. Each gets the
    `kind` "operator_custom_<i>" for traceability.

    Empty / invalid regexes are skipped silently rather than crashing
    the hook.
    """
    raw = os.environ.get("ALLOSTAT_ROLLOUT_PATTERNS", "")
    if not raw:
        return ()
    out: list[tuple[str, re.Pattern]] = []
    for i, pat_str in enumerate(raw.split(";")):
        pat_str = pat_str.strip()
        if not pat_str:
            continue
        try:
            out.append((f"operator_custom_{i}", re.compile(pat_str, re.IGNORECASE)))
        except re.error:
            continue
    return tuple(out)


def classify_command(command: str | None) -> tuple[str, str] | None:
    """Classify a Bash command. Returns (kind, matched_pattern_str) on
    match; None if no pattern fires.

    `kind` is the tag from `_BUILTIN_PATTERNS` (or "operator_custom_N").
    `matched_pattern_str` is the regex source for forensics / debugging.
    """
    if not command or not isinstance(command, str):
        return None
    patterns = _BUILTIN_PATTERNS + _custom_patterns_from_env()
    for kind, pat in patterns:
        if pat.search(command):
            return (kind, pat.pattern)
    return None


def _scan_enabled() -> bool:
    """Honor ALLOSTAT_ROLLOUT_DETECTOR env var. Defaults to enabled.

    Operator can set `0|false|no|off` to disable. Useful for sessions
    where lots of git pushes to dev branches would create noise (the
    `git_push_main` pattern is conservative but `git_push_force`
    matches any force push).
    """
    raw = os.environ.get("ALLOSTAT_ROLLOUT_DETECTOR", "")
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def detect_and_emit(
    state_dir,  # Path | None — caller guards None outside
    tool_name: str,
    command: str | None,
) -> dict[str, Any] | None:
    """Classify the command + emit `rollout_detected` observation if it
    matches a deploy pattern. Returns the emitted details dict, or None
    when nothing fires.

    Best-effort: any append failure is swallowed silently rather than
    breaking the PreToolUse hook.
    """
    if not _scan_enabled():
        return None
    if (tool_name or "").lower() != "bash":
        return None
    match = classify_command(command)
    if match is None:
        return None
    # F1 (2026-05-29): a deploy-shaped command targeting temp/scratch/build
    # paths is dev-cycle noise, not a real rollout — suppress.
    from migration_provenance import is_denylisted, auto_acknowledge  # noqa: E402
    from secret_redaction import redact_secrets  # noqa: E402
    if is_denylisted(command):
        return None
    kind, pattern_str = match
    details = {
        "kind": kind,
        # Redact secrets before persisting to observations.jsonl (ultraswarm
        # H-5, 2026-07-07) — raw deploy commands carry token-in-URL creds.
        "command_excerpt": redact_secrets(command or "")[:300],
        "matched_pattern": pattern_str,
    }
    try:
        from local_state import append_observation  # noqa: E402
        append_observation(state_dir, "rollout_detected", details=details)
    except Exception:
        return None
    # F2 (2026-05-29): regulator self-acknowledges so volume-control doesn't
    # nag for a manual /allostat record-migration. Audit observation still
    # written; only the manual-click demand goes away.
    auto_acknowledge(state_dir, "rollout", kind, command)
    return details
