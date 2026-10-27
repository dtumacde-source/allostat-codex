#!/usr/bin/env python3
"""PreToolUse hook — v0.2.0 thin shim.

v0.2.0 architectural change vs v0.1.12:
  - REPLACED the client-side classifier with ONE call to
    `dispatch_pre_tool_use`. Server-side innate-enforcer fires its rules
    against the forwarded event and returns red-box text via
    `additional_context`.
  - Records action presence and one-way digests in generic telemetry; raw
    command and path material remains confined to the local tool request.
  - Filesystem-context detection (rule 04 canonical-verification inputs)
    stays client-side because it requires the operator's filesystem
    which the server can't see.
"""
from __future__ import annotations

import re
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import (  # noqa: E402
    detect_harness,
    disabled,
    emit_additional_context,
    emit_stderr,
    flush_pending_context,
    has_mcp_token,
    is_terminally_inactive,
    read_payload,
    refuse_tool_call as _emit_refusal,
    resolve_project_root_from_payload,
    should_inject,
)

_confirmed_action = None
_tool_refused = False


def refuse_tool_call(message, *, hook_event="PreToolUse"):
    """Remember denials even when Codex communicates them with exit zero."""
    global _tool_refused
    _tool_refused = True
    return _emit_refusal(message, hook_event=hook_event)

# Review fix 2026-07-04 (MED): every command sink below (observation write,
# innate-fire logs, MCP dispatch) must pass through the secret redactor —
# raw commands carry credentials (the review's concrete case: the RDS DSN,
# password included, persisted in plaintext observations.jsonl for days).
# Crash-armor: a broken redactor must never break the operator's session,
# but the degradation is surfaced on stderr, not silent.
try:
    from secret_redaction import redact_secrets  # noqa: E402
except Exception as _redact_import_err:  # pragma: no cover — broken install
    def redact_secrets(text):  # type: ignore[misc]
        del text
        return "[REDACTION UNAVAILABLE]"

    emit_stderr(
        "secret_redaction unavailable; outbound diagnostic material withheld "
        f"({_redact_import_err.__class__.__name__})"
    )


# C02 (2026-08-03) — the semantic-operation classifier for rules 09/10.
#
# The rules speak `gmail_send` / `stripe_charge` / `oauth_authorize`; the
# pipeline spoke only `write`/`edit`/`read`. Two vocabularies, one parameter
# name, no adapter — so two lethal rules were certified enforceable and could
# never fire (audit C02). This classifier is the adapter: it normalizes an
# app/MCP tool call into the rules' fixed operation vocabulary BEFORE
# execution, wrapper-local, so the lethal decision is an in-line exit code
# and never a raced advisory (PATCH-143).
#
# Design rules, in order of precedence (full evidence base and every
# judgment call: audits/20260803_send_and_permission_operation_inventory.md):
#
#   1. Read-shaped names never classify. `list`/`get`/`search`/... return
#      None immediately — rule 09's own text says reading inboxes is fine,
#      and an over-firing guard on mail tooling gets the product disabled.
#   2. Composing is not sending. `create_draft`/`update_draft`/`compose-*`
#      stay None; only a send-shaped verb classifies.
#   3. Unknown stays unknown. A name this table cannot read returns None —
#      `_match_operation_entry` treats None as no-fire (strict, like
#      match_all). The guard covers named semantic operations; guessing
#      would fire two lethal rules on every novel tool.
#   4. Annotations live here, not in the matcher. `platforms` evidence is
#      how a name reaches `post_to_social`; rule 09's
#      `check: payload_is_communication` is why the webhook branch demands
#      a content-bearing argument before classifying.

_SEMANTIC_READ_VERBS = frozenset({
    "get", "list", "search", "read", "fetch", "retrieve", "describe",
    "show", "preview", "view", "query", "count", "download", "status",
    "history", "export", "suggest", "resolve",
})

_SOCIAL_PLATFORM_TOKENS = frozenset({
    # innate-09's platforms annotation, as name evidence.
    "twitter", "x", "reddit", "linkedin", "facebook", "instagram",
    "threads", "mastodon", "bluesky", "social",
})

_MAIL_TOKENS = frozenset({"email", "emails", "mail", "gmail", "outlook", "smtp"})

_COMMS_PAYLOAD_KEYS = frozenset({
    "payload", "body", "data", "text", "message", "content", "html", "json",
})


def _semantic_tool_tokens(tool_name: str | None) -> set[str]:
    """Lowercased word tokens of the tool segment of an MCP/app tool name.

    `mcp__<server>__<tool>` keeps only `<tool>` — server aliases are
    user-configured and carry no stable meaning; Codex-style bare names
    (`gmail_send_email`) pass through whole.
    """
    tail = (tool_name or "").rsplit("__", 1)[-1].lower()
    return {t for t in re.split(r"[^a-z0-9]+", tail) if t}


def _normalize_file_tool_name(tool_name: str | None) -> str | None:
    """Normalize only known file-content tool spellings for the M1 gate."""
    tail = (tool_name or "").rsplit("__", 1)[-1].lower()
    tail = re.sub(r"[^a-z0-9]+", "_", tail).strip("_")
    for prefix in ("functions_", "filesystem_", "file_"):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
    aliases = {
        "write": "write", "write_file": "write", "create_file": "write",
        "edit": "edit", "edit_file": "edit", "replace_in_file": "edit",
        "multiedit": "multiedit", "multi_edit": "multiedit",
        "multi_edit_file": "multiedit", "notebookedit": "edit",
        "notebook_edit": "edit",
    }
    return aliases.get(tail)


def _outbound_guard_module_failure_shape(tool_name: str | None, payload: dict) -> bool:
    """Conservative module-failure circuit breaker, not an authority classifier."""
    tokens = _semantic_tool_tokens(tool_name)
    if not tokens or tokens & _SEMANTIC_READ_VERBS:
        return False
    if "send" not in tokens and tokens & {"draft", "compose"}:
        return False
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    keys = {str(key).lower() for key in tool_input} if isinstance(tool_input, dict) else set()
    communication_evidence = bool(
        tokens & {"message", "messages", "email", "emails", "mail", "gmail",
                  "outlook", "smtp", "dm", "chat", "sms", "thread", "webhook",
                  "broadcast"}
        or keys & _COMMS_PAYLOAD_KEYS
    )
    if (
        "send" in tokens
        and tokens & {"keys", "key", "event", "signal", "command", "input"}
        and not communication_evidence
    ):
        return False
    if "send" in tokens:
        return True
    if tokens & {"post", "publish", "tweet", "toot"}:
        return isinstance(tool_input, dict) and bool(tool_input)
    return False


# F02 (2026-08-03) — vocabulary for "this call looks like it grants something,
# and rule 10 did not recognize it." NOT an enforcement list: nothing here
# blocks. The operator ruled that innate-10 advertises only the shapes it
# actually catches, which leaves a real question — WHICH shapes are we missing?
# Answering it by guessing is how the universal-coverage claim happened in the
# first place. These tokens make an unrecognized permission-shaped call leave a
# durable trace, so the gap list writes itself from real usage and stays
# visible instead of being silently allowed.
_PERMISSION_SHAPE_TOKENS = frozenset({
    "permission", "permissions", "grant", "grants", "access", "role", "roles",
    "collaborator", "collaborators", "member", "members", "membership",
    "invite", "invitation", "share", "sharing", "consent", "authorize",
    "authorization", "oauth", "sso", "credential", "credentials", "scope",
    "scopes", "privilege", "privileges", "admin", "owner", "ownership",
    "payment", "charge", "billing", "subscription", "payout", "transfer",
})


def _looks_permission_shaped(tool_name: str | None) -> bool:
    """True when a call carries permission/money vocabulary. Advisory only —
    the caller RECORDS, never refuses. Read-shaped calls are excluded so
    `list_permissions` and `get_roles` do not generate noise."""
    tokens = _semantic_tool_tokens(tool_name)
    if not tokens or tokens & _SEMANTIC_READ_VERBS:
        return False
    return bool(tokens & _PERMISSION_SHAPE_TOKENS)


def _record_unclassified_permission_shape(state_dir, tool_name: str | None) -> None:
    """One durable observation per DISTINCT unrecognized permission-shaped
    tool name. Deduped with the same atomic O_CREAT|O_EXCL claim the rule-drop
    announcer uses — the hook is a fresh process per tool call, so in-memory
    state cannot dedup. Per tool name rather than per session: the point is the
    SET of shapes worth adding, and a shape seen a thousand times is still one
    entry on that list.

    Never raises past its caller's guard: bookkeeping must not affect whether
    a tool call proceeds.
    """
    if state_dir is None or not tool_name:
        return
    if not _looks_permission_shaped(tool_name):
        return
    import hashlib  # noqa: E402
    import os  # noqa: E402

    digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:16]
    claim = Path(state_dir) / f"permission_shape_seen_{digest}"
    try:
        claim.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return  # this shape is already on the list
    except OSError:
        pass  # cannot claim (odd fs): record anyway — twice beats never
    from local_state import append_observation  # noqa: E402
    append_observation(state_dir, "permission_shape_unclassified", details={
        "tool_name": tool_name[:200],
        "tokens": sorted(_semantic_tool_tokens(tool_name) & _PERMISSION_SHAPE_TOKENS),
        "note": "allowed, not blocked — innate-10 does not recognize this shape",
    })


def _classify_semantic_operation(tool_name: str | None, payload: dict) -> str | None:
    """Map an app/MCP tool call to rules 09/10's operation vocabulary.

    Returns one of the rules' fixed `operation:` values, or None when the
    call is read-shaped, draft-shaped, or unrecognized. Never raises: a
    classifier crash must not become either a refusal or a bypass — the
    caller wraps it, and None is the fail-quiet direction the strict
    matcher expects.
    """
    # Messaging authorization belongs to the host. The retired send guard must
    # not turn internal task routing into a broadcast operation, including on
    # hosted servers which still carry the old innate-09 rule.

    tokens = _semantic_tool_tokens(tool_name)
    if not tokens:
        return None
    if tokens & _SEMANTIC_READ_VERBS:
        return None
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    arg_keys = (
        {str(k).lower() for k in tool_input}
        if isinstance(tool_input, dict) else set()
    )

    # --- rule 10: grants, agreements, money, account controls ------------
    if tokens & {"oauth", "authorize"}:
        return "oauth_authorize"
    if "sso" in tokens:
        return "sso_complete"
    if tokens & {"accept", "agree", "consent"}:
        if tokens & {"privacy"}:
            return "privacy_policy_accept"
        if tokens & {"cookie", "cookies"}:
            return "cookie_consent"
        if tokens & {"terms", "tos", "agreement", "license", "invite",
                     "invitation"}:
            return "terms_of_service_accept"
        return None  # accept_suggestion and friends stay unclassified
    if tokens & {"share", "sharing", "permission", "permissions"} and tokens & {
        "add", "create", "grant", "set", "update", "change", "modify",
    }:
        return "sharing_permission_grant"
    if "api" in tokens and tokens & {"key", "keys"} and tokens & {
        "create", "grant", "issue", "generate", "rotate",
    }:
        return "api_key_grant"
    if "webhook" in tokens and tokens & {"create", "register", "add"}:
        return "webhook_endpoint_register"
    if tokens & {"charge", "payment", "purchase", "order"}:
        if "stripe" in tokens:
            return "stripe_charge"
        if tokens & {"complete", "create", "confirm", "capture", "execute",
                     "process", "submit", "place"}:
            return "payment_complete"
        return None
    if "subscription" in tokens and tokens & {"create", "activate", "start",
                                              "resume"}:
        return "subscription_activate"
    if "password" in tokens and tokens & {"set", "change", "reset", "update"}:
        return "password_set"
    if tokens & {"2fa", "mfa", "totp"} and tokens & {"setup", "enable",
                                                     "register", "add",
                                                     "create"}:
        return "2fa_setup"
    if tokens & {"account", "profile"} and tokens & {"setting", "settings"} \
            and tokens & {"update", "change", "set", "modify"}:
        return "account_setting_change"
    return None


def _extract_tool_attributes(payload: dict) -> tuple[str, str | None, str | None]:
    """Return (tool_name, command, file_path) from the PreToolUse payload."""
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = tool_input.get("command")
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
    )
    return tool_name, command, file_path


# C2 (Wave 3) — shell write-target extraction. A Bash command can write into a
# protected tree via redirection with tool_name "bash" and no file_path, so the
# path-based gates never see it. A quote-aware tokenizer isolates the redirect
# operators, so real writes are extracted while a `>` INSIDE a quoted string is
# ignored (no spurious deny of e.g. `git commit -m '... > /path'`).
_PURE_REDIR = re.compile(r"^\d*>>?\|?$")   # >, >>, >|  (write redirects; fd digits split off)
_DUP_REDIR = re.compile(r"^\d*>>?&")        # descriptor-shaped tee scan boundary
_WRITE_PATH_REDIR_OPS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_FD_NUMBER_OR_CLOSE = re.compile(r"^(?:\d+|-)$")

# C2 extension (Wave 3) — `cp`/`mv`/`sed -i` write-target extraction. These run
# as normal command words (not redirect operators), so the redirect scanner
# above never saw them; a Bash `cp secret .../sandbox/x` (or `mv`, or `sed -i`)
# therefore bypassed the sandbox deny gate the way redirects once did. The
# extraction below reuses the SAME quote-aware tokenizer (`_tokenize_bash`) — no
# second quote parser — and only treats a token as a command word at a genuine
# command boundary (start of input, or right after a command-separator operator,
# after leading NAME=value env-assignments). Over-matching stays fail-safe (an
# extra non-scoped target is simply allowed; the GATE decides scope).
_OPERATOR_RUN = re.compile(r"^[<>|&;()]+$")        # any tokenizer operator token
_CMD_SEPARATOR = re.compile(r"^[;|&()]+$")          # ; | & && || ( ) — starts a new command word
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")  # leading VAR=value

# Redirection operators that may legally precede a Bash simple-command word.
# The tokenizer emits the operator and its word operand separately, including
# attached spellings (`>/tmp/x`), and splits an adjacent fd number (`2>/tmp/x`)
# into its own token. Every supported form consumes one following redirection
# word; for fd duplication/closure that word is an fd designator, not a path.
_LEADING_REDIR_OPS = frozenset({
    "<", ">", ">>", ">|", "<>", "<<", "<<<", "<&", ">&", "&>", "&>>",
})
_FD_PREFIX_REDIR_OPS = _LEADING_REDIR_OPS - {"&>", "&>>"}
_DYNAMIC_FD_PREFIX = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")
# M-08 RESIDUAL (found 2026-07-17, cross-utility, NOT closed here): `'` is
# deliberately absent below, so `$'...'` ANSI-C quoting is tokenized as a literal
# `$` + ordinary single-quoted string. A statically-decodable write target hidden
# in ANSI-C quoting therefore escapes the gate for EVERY write utility, e.g.
# `cp src $'/vault/out'` -> hook sees `$/vault/out` (keeps the `$`) -> not under
# `/vault/`. This is a tokenizer-layer gap (all 15 prior rounds missed it too),
# distinct from the runtime `$VAR` residual: closing it needs real ANSI-C decode
# (or a fail-closed on `$'` in an active word) in `_tokenize_bash`, whose blast
# radius spans all command parsing. Deferred; true closure of the write-detection
# surface as a whole ultimately wants OS/filesystem-level enforcement, not string
# parsing. Tracked as a follow-up, not silently "closed".
_DOLLAR_EXPANSION_START = frozenset("{(0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_@$*#?!-")
_UNRESOLVED_BASH_WRITE_MESSAGE = (
    "⛔ Bash write target cannot be resolved safely before execution; "
    "command refused."
)


class _UnresolvedBashWriteTarget(ValueError):
    """A selected Bash write operand is changed by runtime shell expansion."""


# FINDING 1: a deliberate, import-free duplicate of
# `innate_rules._FALLBACK_DESTRUCTIVE_PATTERNS`. It exists precisely so the
# destructive guard survives `innate_rules` being unimportable — importing it
# from there would reintroduce the dependency that caused the fail-open.
# Duplication is the point; `test_last_resort_patterns_match_innate_rules`
# asserts the two lists stay byte-identical so they cannot drift apart.
_LAST_RESORT_DESTRUCTIVE = (
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)",
    r"\brmdir\s+/s\b",
    r"\bgit\s+push\b[^|;&]*\s(--force\b|-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+checkout\s+--\s",
    r"\bgit\s+worktree\s+remove\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b(?![^;]*\bwhere\b)",
    r"\bmkfs(\.|\s)",
    r"\bdd\s+[^|;&]*\bof=/dev/",
    r"\bshred\b",
    r"\bremove-item\b[^|;&]*-recurse",
    r"\bformat\s+[a-zA-Z]:",
    r"\bshutdown\b",
    r"\breboot\b",
)


def _last_resort_destructive_match(command: str | None) -> bool:
    """Import-free destructive matcher. Used only when `innate_rules` itself
    cannot be loaded; a broken individual pattern must never disable the rest."""
    if not command:
        return False
    haystack = command.lower()
    for pattern in _LAST_RESORT_DESTRUCTIVE:
        try:
            if re.search(pattern, haystack):
                return True
        except re.error:
            continue
    return False


def _destructive_action_candidate(tool_name: str | None, command: str | None) -> bool:
    """Classify rule-02 actions before any value-add command telemetry."""
    if _last_resort_destructive_match(command):
        return True
    if not command:
        return False
    try:
        from innate_rules import match_pre_tool_call

        from guard_settings import detection_scope
        with detection_scope():
            match = match_pre_tool_call(tool_name=tool_name, command=command)
        return bool(match is not None and match.rule_id == "innate-02")
    except Exception:  # noqa: BLE001
        # The import-free subset is the only defensible classifier when the
        # authenticated ruleset cannot load; its refusal path runs later too.
        return True  # Unknown risk must not enable raw command diagnostics.


def _innate_evaluator_failed(command, exc):
    """Last-resort guard when the innate evaluator itself RAISED.

    WHY THIS EXISTS (2026-07-25 adversarial audit)
    ----------------------------------------------
    Both `except` arms below the evaluator call used to log to stderr and fall
    through with no `return`, so ANY exception inside `match_pre_tool_call`
    meant the tool ran unguarded — all twelve rules off at once, including the
    three lethal ones, with the only trace a stderr line hooks rarely surface.

    That is reachable, not theoretical. `_rule_content_hash` deliberately
    RAISES on a mapping key JSON cannot spell (the 2026-07-24 canonical-key
    fix introduced the raise), and `_build_working_set` hashes every rule
    carrying a manifest id with no guard. One crafted rules file — an
    unquoted YAML date scalar used as a mapping key — turns that raise into a
    total bypass for every subsequent tool call. Writing that file is itself
    unguarded, so an injected agent needs one benign-looking Write before it
    needs anything destructive.

    The fix is not "refuse everything": a broken evaluator would then brick a
    paying customer's session. It is to fall back to the same hardcoded
    last-resort patterns the shipped code already uses when the ruleset is
    degraded, and to make the degradation VISIBLE rather than stderr-only.

    FINDING 1 (2026-07-26) CLOSED — that residual is gone. This used to read:
    "on ImportError the patterns are unreachable too ... so it degrades to a
    loud warning, not a guard", and it `return`ed None, which is PERMIT. So the
    single most severe failure mode — `innate_rules` unimportable at module
    level, i.e. the whole ruleset gone — was the one that disarmed the guard
    completely, while a merely poisoned rule file still refused. An attacker who
    can corrupt one import gets a better outcome than one who corrupts a rule.

    An import failure must now fail closed IDENTICALLY to a match failure. It
    does, via `_LAST_RESORT_DESTRUCTIVE` below — a self-contained copy of the
    pattern set that needs no import, so "the module is gone" no longer means
    "the teeth are gone". `test_last_resort_patterns_match_innate_rules` pins
    the two copies together so they cannot drift.
    """
    try:
        from guard_settings import enabled
        if not enabled('innate-02'):
            emit_stderr('ALLOSTAT SAFETY DEGRADED: evaluator unavailable; innate-02 is owner-disabled. Package repair still required.')
            return None
    except Exception:
        pass  # Missing settings code never disables the existing fallback.
    try:
        from innate_rules import _fallback_destructive_match  # noqa: E402
    except Exception:  # noqa: BLE001 — the module is what failed
        # Do NOT return None here: that is a permit, and this is the worst
        # failure mode, not the mildest. Fall back to the import-free set.
        emit_stderr(
            "ALLOSTAT SAFETY DEGRADED: the innate ruleset could not be loaded "
            f"({type(exc).__name__}). Falling back to the import-free "
            "last-resort destructive patterns. Reinstall the plugin."
        )
        if not _last_resort_destructive_match(command):
            return None
        return refuse_tool_call(
            "**Allostat refused a destructive action because the innate "
            "ruleset could not be loaded.**\n\n"
            f"Failure class: `{type(exc).__name__}`. The command and target "
            "are intentionally omitted from diagnostics.\n\n"
            "This matches Allostat's import-free last-resort destructive "
            "patterns, so it is refused rather than allowed. Your install is "
            "broken and the other rules are NOT being enforced right now. "
            "Reinstall the plugin (`/allostat-doctor`) before continuing.",
            hook_event="PreToolUse",
        )

    if not _fallback_destructive_match(command):
        return None

    emit_stderr(
        f"Allostat innate evaluator failed ({type(exc).__name__}); refusing a "
        "destructive command from the last-resort pattern set."
    )
    return refuse_tool_call(
        "**Allostat refused a destructive action because the innate ruleset "
        "is not evaluable.**\n\n"
        f"Failure class: `{type(exc).__name__}`. The command and target are "
        "intentionally omitted from diagnostics.\n\n"
        "This command matches Allostat's hardcoded last-resort destructive "
        "patterns, so it is refused rather than allowed. A rules file is "
        "corrupt or has been tampered with — the other rules are NOT being "
        "enforced right now. Reinstall the plugin (`/allostat-doctor`) before "
        "continuing.",
        hook_event="PreToolUse",
    )


def _announce_innate_rule_drops(state_dir, session_id: str | None = None) -> None:
    """One durable `innate_rule_dropped` observation per SESSION per DISTINCT
    degradation (M01, 2026-08-03 — reviewer and advisor ruled identically).

    The working-set gate (innate_rules._build_working_set) is right to drop an
    edited rule — content that no longer matches its pin must not be enforced.
    What it may not do is drop QUIETLY: before this, the only trace on an
    allowed call was a logger.warning in a subprocess nobody reads, and the
    loud path (session-start banner) cannot fire until the next restart.

    Identity is (session, content): the digest covers the sorted (id, reason)
    pairs — and since the hash-mismatch reason now embeds the OBSERVED content
    hash, two different edits to the same rule are two different degradations.
    A new session re-records an ongoing degradation (the forever-marker's
    silence was the finding); the same degradation within one session records
    once (observations.jsonl already met one every-tool-call flooder — the
    2026-07-30 cut removed it, and this must not reintroduce the shape).

    Dedup is ATOMIC across processes: each (session, digest) pair claims an
    O_CREAT|O_EXCL marker file — first creator announces, everyone else
    returns. No read-then-write window (the old single-marker scheme could
    double-append on parallel first calls). The legacy
    `innate_dropped_announced.json` marker is IGNORED but never deleted —
    operator state; one extra announcement on upgrade beats a lost one.
    Claim files are tiny and bounded (one per degradation per session).

    Never raises past its caller's guard: enforcement must not depend on
    bookkeeping.
    """
    if state_dir is None:
        return
    from innate_rules import dropped_rules  # noqa: E402
    drops = dropped_rules()
    if not drops:
        return
    import hashlib  # noqa: E402
    import json  # noqa: E402 — this hook has no module-level json import
    import os  # noqa: E402

    content = json.dumps(sorted((d["id"], d["reason"]) for d in drops))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    sess_key = hashlib.sha256(
        (session_id or "nosession").encode("utf-8")
    ).hexdigest()[:12]
    claim = Path(state_dir) / f"innate_drop_claim_{digest}_{sess_key}"
    try:
        # Parent first: on the very first tool call the state dir may not
        # exist yet, and a FileNotFoundError here would skip the claim and
        # double-announce on the next call (caught by 0B.4's repeat test).
        claim.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return  # this (session, degradation) already announced — atomically
    except OSError:
        pass  # cannot claim (odd fs): announce anyway — twice beats never
    from local_state import append_observation  # noqa: E402
    append_observation(state_dir, "innate_rule_dropped", details={
        "rules": [d["id"] for d in drops],
        "reasons": {d["id"]: d["reason"] for d in drops},
        "count": len(drops),
        "session": (session_id or "")[:64],
    })


def _record_ruleset_degradation(state_dir, kind: str, exc: BaseException) -> None:
    """One durable `innate_ruleset_degraded` observation per DISTINCT
    evaluator/import failure. C01 (2026-08-03).

    The two `except` arms around the innate evaluator route to
    `_innate_evaluator_failed`, which correctly refuses destructive-looking
    commands on last-resort patterns and ALLOWS everything else — but the
    allowed path used to leave no durable trace at all: stderr only, in a
    subprocess nobody reads. A session could run start to finish with the
    whole evaluator down and the record would show nothing.

    Same shape as `_announce_innate_rule_drops`: durable and legible, never
    interruptive (the advisor's 2026-08-03 ruling) — this function reports
    the degradation, it never converts it into a refusal, and it never
    raises past its caller. Dedup is content-keyed on (kind, error class,
    error text) so a DIFFERENT failure later in the session still records.
    """
    if state_dir is None:
        return
    try:
        import hashlib  # noqa: E402
        import json  # noqa: E402

        error_digest = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
        content = json.dumps([kind, type(exc).__name__, error_digest])
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        marker = Path(state_dir) / "innate_ruleset_degraded_announced.json"
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("digest") == digest:
                return
        except (OSError, ValueError):
            pass  # absent or unreadable marker: announce — twice beats never
        from local_state import append_observation  # noqa: E402
        append_observation(state_dir, "innate_ruleset_degraded", details={
            "kind": kind,
            "error_class": type(exc).__name__,
            "error_digest": error_digest,
            "destructive_fallback": "armed (last-resort patterns)",
        })
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"digest": digest}), encoding="utf-8")
    except Exception as record_err:  # noqa: BLE001
        emit_stderr(
            f"innate degradation record failed: {record_err.__class__.__name__}"
        )


def _bash_sandbox_policy_active() -> bool:
    """True when a sandbox policy exists that could actually DENY a write.

    Audit 2026-07-21. The `_UnresolvedBashWriteTarget` refusal below is a
    fail-closed uncertainty gate: when a Bash write operand carries parameter,
    command, arithmetic, tilde, glob or brace expansion, the hook cannot know
    the real path before execution and refuses rather than guess. That is the
    right call — WHEN there is a policy the guess could get wrong.

    For a customer with no `~/.allostat/sandbox_config.json` there is not.
    `sandbox_config.load_config()` returns `source="default"`,
    `sandbox_scoped_substrings=()`, `load_error=False`; `is_sandbox_scoped()`
    then returns False for EVERY path and `evaluate_write_permission()` is an
    unconditional allow (its own docstring: "The pillar is an intentional
    no-op"). So the refusal was denying `cp src/*.py backup/`,
    `npm test > ~/test.log`, `mv "$SRC" "$DST"` and every other non-literal
    write operand to protect a policy that could not have denied any of them —
    for active, trial, `unknown` and missing-cache users alike. R3 deliberately
    provides no cross-risk destructive-consent escape for sandbox uncertainty.

    Fail-closed everywhere it still matters: a present-but-unparseable config
    (`load_error`) and any config carrying scoped substrings both keep the
    refusal, and an import/parse failure here returns True so an unknown state
    never degrades to allow.
    """
    try:
        import sandbox_config  # noqa: E402

        config = sandbox_config.load_config()
    except Exception:
        return True  # cannot determine the policy → keep refusing
    return bool(
        getattr(config, "load_error", True)
        or getattr(config, "sandbox_scoped_substrings", ("",))
    )


def _dollar_starts_shell_expansion(command: str, i: int) -> bool:
    """Whether an active ``$`` starts parameter, command, or arithmetic expansion."""
    return i + 1 < len(command) and command[i + 1] in _DOLLAR_EXPANSION_START


def _dollar_starts_multi_argv_expansion(command: str, i: int) -> bool:
    """Whether an active parameter expansion can yield multiple argv when quoted."""
    if command.startswith("$@", i):
        return True
    if not command.startswith("${", i):
        return False
    end = command.find("}", i + 2)
    if end < 0:
        return False
    body = command[i + 2:end]
    return body == "@" or "[@]" in body


def _brace_starts_shell_expansion(command: str, start: int) -> bool:
    """Recognize an unquoted Bash brace-expansion word, not a plain ``{word}``."""
    depth = 0
    marker = False
    quote: str | None = None
    i = start
    while i < len(command):
        c = command[i]
        if quote is not None:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return marker
        elif depth > 0 and c == ",":
            marker = True
        elif depth > 0 and c == "." and command[i:i + 2] == "..":
            marker = True
            i += 1
        elif depth == 0 or c in " \t\n<>|&;()":
            return False
        i += 1
    return False


def _tilde_starts_shell_expansion(prefix: str, prefix_quoted: list[bool]) -> bool:
    """Whether an unquoted ``~`` is at a Bash tilde-expansion boundary."""
    if not prefix:
        return True
    return bool(
        not any(prefix_quoted)
        and _ENV_ASSIGNMENT.match(prefix)
        and prefix[-1] in "=:"
    )

# M-03 (2026-07-15, SAFETY) — three more Bash write vectors the extraction above
# still missed, each routed through the SAME sandbox/innate gates as the redirect
# and cp/mv/sed targets:
#   * interpreter writes — `python -c "open('<scoped>','w')..."` (and the pathlib
#     `.write_text`/`.write_bytes` equivalents). The write lives INSIDE the inline
#     code string, so no redirect/cp token and no file_path ever exposed it.
#   * copy/sync utilities beyond cp/mv — `rsync` and `install` write a dest.
# Over-matching stays fail-safe (the GATE decides scope). Scoped conservatively:
# interpreter code is scanned ONLY when an interpreter is the command word, and
# only WRITE-mode opens are extracted (a read-mode `open(...,'r')` is left alone).
_INTERPRETERS = frozenset({"python", "python3", "python2", "py"})
# open(PATH, MODE) / open(PATH, mode=MODE) — capture PATH only when MODE is a
# write mode (contains one of w / a / x / +). Positional or `mode=` keyword.
_OPEN_WRITE = re.compile(
    r"""open\s*\(\s*(['"])(?P<path>[^'"]+?)\1\s*,\s*(?:mode\s*=\s*)?(['"])(?P<mode>[^'"]*)\3""")
# pathlib: Path('PATH').write_text(...) / .write_bytes(...) — always a write.
_PATHLIB_WRITE = re.compile(
    r"""(['"])(?P<path>[^'"]+?)\1\s*\)\s*\.\s*write_(?:text|bytes)\s*\(""")


class _BashTokenText(str):
    r"""Token spelling plus its shell-normalized command-word form.

    ``str(token)`` retains the source spelling for provenance and diagnostics;
    dispatch, option parsing, and selected paths use ``shell_word``, which
    removes only backslashes that Bash treats as escapes. Quoted backslashes
    remain literal. This preserves the
    distinction between ``r"sy"n\c`` (rsync) and ``"r\sync"`` (a literal
    backslash inside quoted executable text).
    """

    shell_word: str
    shell_raw_boundaries: tuple[int, ...]
    assignment_word: bool
    dynamic_fd_prefix: bool
    shell_expansion: bool
    unquoted_shell_expansion: bool
    multi_argv_shell_expansion: bool
    undecodable_ansi_c: bool

    def __new__(
        cls,
        value: str,
        shell_word: str,
        shell_raw_boundaries: tuple[int, ...],
        assignment_word: bool,
        dynamic_fd_prefix: bool = False,
        shell_expansion: bool = False,
        unquoted_shell_expansion: bool = False,
        multi_argv_shell_expansion: bool = False,
        undecodable_ansi_c: bool = False,
    ):
        obj = super().__new__(cls, value)
        obj.shell_word = shell_word
        obj.shell_raw_boundaries = shell_raw_boundaries
        obj.assignment_word = assignment_word
        obj.dynamic_fd_prefix = dynamic_fd_prefix
        obj.shell_expansion = shell_expansion
        obj.unquoted_shell_expansion = unquoted_shell_expansion
        obj.multi_argv_shell_expansion = multi_argv_shell_expansion
        obj.undecodable_ansi_c = undecodable_ansi_c
        return obj


class _BashDerivedTarget(str):
    """A target substring retaining expansion provenance from its source word.

    Inline interpreter code is one shell word, so an active expansion elsewhere
    in that code word conservatively refuses alongside a recognized write call.
    This bounded over-denial avoids pretending to map shell quoting back through
    the embedded language's string-expression grammar.
    """

    shell_expansion: bool

    def __new__(cls, value: str, source: str):
        obj = super().__new__(cls, value)
        obj.shell_expansion = bool(getattr(source, "shell_expansion", False))
        return obj


def _derived_bash_target(value: str, source: str) -> str:
    return _BashDerivedTarget(value, source)


def _bash_shell_word(token: str) -> str:
    """Return the argv/path spelling Bash forms after quote removal."""
    return getattr(token, "shell_word", token)


def _effective_bash_target(token: str) -> str:
    """Select a path in shell-effective form while retaining expansion state."""
    raw = str(token)
    # Preserve the hook's established Windows-drive input contract. Codex can
    # submit native ``C:\\...`` operands to the Bash tool even though an
    # interactive Bash author would normally quote or slash-normalize them.
    # Other unquoted backslashes remain shell escapes and are removed.
    value = raw if re.match(r"^[A-Za-z]:\\", raw) else _bash_shell_word(token)
    return _derived_bash_target(value, token)


_NATIVE_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:\\")


def _effective_bash_attached_target(
    value: str,
    normalized_prefix: str,
    token: str,
) -> str:
    """Preserve a native drive path at a provenance-mapped option boundary."""
    boundaries = getattr(token, "shell_raw_boundaries", ())
    word = _bash_shell_word(token)
    prefix_len = len(normalized_prefix)
    if word.startswith(normalized_prefix) and len(boundaries) > prefix_len:
        raw_suffix = str(token)[boundaries[prefix_len]:]
        if _NATIVE_DRIVE_PREFIX.match(raw_suffix):
            value = raw_suffix
        elif re.match(r"^[A-Za-z]:", value) and len(boundaries) > prefix_len + 2:
            raw_remainder = str(token)[boundaries[prefix_len + 2]:]
            if raw_remainder.startswith("\\"):
                value = value[:2] + raw_remainder
    return _derived_bash_target(value, token)


_COMMAND_PATH_SEPARATOR = re.compile(r"[\\/]")
# Windows PATHEXT executable suffixes stripped so a path-qualified `python.exe`
# / `cp.exe` still classifies as its bare tool. Case-insensitive.
_COMMAND_EXECUTABLE_SUFFIX = re.compile(r"(?i)\.(?:exe|com|bat|cmd)$")


def _command_dispatch_name(token: str) -> str:
    r"""Reduce a command word to the basename used for write-classifier dispatch.

    Path-qualified invocations name the same utility as their bare form —
    ``/bin/cp``, ``/usr/bin/python3``, a native ``C:\Windows\System32\...``
    spelling — so the classifier compares the trailing path segment, not the
    full path. A native ``C:\`` drive path keeps its backslash separators only in
    the raw spelling (Bash otherwise removes them as escapes), matching the
    hook's established Windows-drive input contract; POSIX and forward-slash
    forms use the shell-effective word.

    The result is lower-cased. Executable resolution on the hook's Windows /
    Git-Bash runtime is case-insensitive — ``CP.EXE`` and ``cp.exe`` name the
    identical binary, and a native ``C:\Git\usr\bin\MV.EXE`` spelling runs the
    same ``mv`` — so a case-sensitive comparison would let an upper/mixed-case
    spelling of a gated utility bypass the classifier. Folding case pairs
    naturally with the ``.exe``/``.com``/``.bat``/``.cmd`` suffix strip above.
    On a case-sensitive POSIX host an upper-case word is a different program, but
    routing it through the gate anyway is only an over-match, never a miss.

    Over-matching stays fail-safe: an extra basename only routes a command
    through the same path gate, never past it. This is a spelling normalization,
    not an OS-level guarantee — like the M-08 rsync-spelling limit, the hook is
    not an airtight execution boundary (an unrecognized utility or an
    interpreter-mediated copy such as ``cmd /c copy`` is out of its reach).
    """
    raw = str(token)
    candidate = raw if _NATIVE_DRIVE_PREFIX.match(raw) else _bash_shell_word(token)
    base = _COMMAND_PATH_SEPARATOR.split(candidate)[-1]
    return _COMMAND_EXECUTABLE_SUFFIX.sub("", base).lower()


def _refuse_dynamic_bash_parser_word(token: str, parser_active: bool) -> None:
    """Fail closed when expansion can change a recognized utility's argv role."""
    if parser_active and getattr(token, "shell_expansion", False):
        raise _UnresolvedBashWriteTarget


def _refuse_splittable_bash_option_value(
    toks: list[tuple[str, bool]], value_index: int
) -> None:
    """Fail closed when a consumed option value can expand into multiple argv."""
    if value_index >= len(toks):
        return
    token = toks[value_index][0]
    if getattr(token, "unquoted_shell_expansion", False) or getattr(
        token, "multi_argv_shell_expansion", False
    ):
        raise _UnresolvedBashWriteTarget


def _bash_separate_option_value_index(
    toks: list[tuple[str, bool]], option_index: int
) -> int | None:
    """Return a separate option value without crossing a command operator."""
    value_index = option_index + 1
    if value_index >= len(toks):
        return None
    token, quoted = toks[value_index]
    if not quoted and _OPERATOR_RUN.match(token):
        return None
    return value_index


def _consume_bash_non_target_option_value(
    toks: list[tuple[str, bool]], option_index: int
) -> int:
    """Consume one separate non-target value, leaving a missing boundary visible."""
    value_index = _bash_separate_option_value_index(toks, option_index)
    if value_index is None:
        return option_index + 1
    _refuse_splittable_bash_option_value(toks, value_index)
    return value_index + 1


_DD_STATIC_ASSIGNMENT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")


def _dd_dynamic_word_can_select_output(token: str) -> bool:
    """Whether expansion can form/split an output-key operand for ``dd``."""
    if not getattr(token, "shell_expansion", False):
        return False
    key = _DD_STATIC_ASSIGNMENT_KEY.match(_bash_shell_word(token))
    return bool(
        key is None
        or key.group(0) == "of="
        or getattr(token, "unquoted_shell_expansion", False)
    )


_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _decode_ansi_c(raw: str) -> str:
    r"""Decode the body of a Bash ANSI-C quoted word (``$'...'``).

    B1 (2026-07-26). This existed as a KNOWN, documented bypass: the tokenizer
    treated ``$'...'`` as a literal ``$`` plus an ordinary single-quoted string,
    so ``cp src $'\x2fprotected\x2fsandbox\x2fout'`` reached the write gate as
    the string ``$/protected/sandbox/out`` — which is not under the protected
    prefix — while Bash wrote to the real path. Reproduced through the shipped
    hook for ``cp``, ``tee`` and ``>`` before this fix.

    The gate must decide on the RUNTIME-RESOLVED identity, never on the
    representation it was handed — the same class as `%61llostat` percent-decoding
    into the production database name.

    Bash's rules, followed here: ``\nnn`` octal, ``\xHH`` hex, ``\uHHHH`` and
    ``\UHHHHHHHH`` unicode, ``\cX`` control, the simple escapes above, and an
    UNRECOGNIZED escape is left as backslash-plus-character (bash keeps both).
    A trailing lone backslash is likewise literal.
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            out.append("\\")          # trailing backslash is literal
            break
        nxt = raw[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
        elif nxt == "x":
            j = i + 2
            digits = ""
            while j < n and len(digits) < 2 and raw[j] in "0123456789abcdefABCDEF":
                digits += raw[j]
                j += 1
            if not digits:
                out.append("\\x")     # bash: \x with no digits stays literal
                i += 2
            else:
                out.append(chr(int(digits, 16)))
                i = j
        elif nxt in "01234567":
            j = i + 1
            digits = ""
            while j < n and len(digits) < 3 and raw[j] in "01234567":
                digits += raw[j]
                j += 1
            out.append(chr(int(digits, 8) & 0xFF))
            i = j
        elif nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            j = i + 2
            digits = ""
            while j < n and len(digits) < width and raw[j] in "0123456789abcdefABCDEF":
                digits += raw[j]
                j += 1
            if not digits:
                out.append("\\" + nxt)
                i += 2
            else:
                code = int(digits, 16)
                # Surrogates and out-of-range values are not representable;
                # the caller treats a raise as UNRESOLVED and fails closed.
                if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    raise ValueError(f"ANSI-C escape out of range: \\{nxt}{digits}")
                out.append(chr(code))
                i = j
        elif nxt == "c":
            if i + 2 >= n:
                out.append("\\c")
                i += 2
            else:
                target = raw[i + 2]
                out.append(chr(ord(target.upper()) ^ 0x40))
                i += 3
        else:
            out.append("\\")          # unrecognized escape: bash keeps both
            out.append(nxt)
            i += 2
    return "".join(out)


def _tokenize_bash(command: str) -> list[tuple[str, bool]]:
    r"""Quote-aware split into (text, was_quoted) tokens. Unquoted whitespace and
    the shell metacharacters ``< > | & ; ( )`` break tokens (operator runs are
    emitted as their own tokens), so a redirect operator can be told apart from a
    filename. Physical LF and CRLF boundaries emit a semicolon-equivalent token;
    bare CR characters are removed, matching Git Bash command parsing, while
    unquoted shell comments are discarded through a line boundary. Quotes are
    stripped; ``was_quoted`` is True if ANY character of the token came from
    inside quotes — so a quoted ``>`` or newline is never an operator.

    The returned text is a ``str`` subclass carrying ``shell_word`` and
    ``assignment_word`` attributes. It retains operand backslashes while
    recording which ones Bash actually consumes during command-word
    construction, including mixed quoted/unquoted tokens. Quote provenance
    around the assignment prefix matters too: ``FOO="x y"`` is an assignment,
    while ``"FOO=x y"`` and ``F"OO"=x`` are command words. Backslash provenance
    keeps a literal quoted ``"r\sync"`` from becoming a false rsync command."""
    # Git Bash removes a bare CR instead of treating it as shell whitespace.
    # Normalize pairs first so CRLF remains a command boundary, then discard
    # only the remaining bare CR characters before quote/word construction.
    command = command.replace("\r\n", "\n").replace("\r", "")

    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    shell_buf: list[str] = []
    shell_raw_boundaries: list[int] = [0]
    shell_quoted: list[bool] = []
    buf_quoted = False
    has_char = False
    shell_expansion = False
    unquoted_shell_expansion = False
    multi_argv_shell_expansion = False
    undecodable_ansi_c = False
    comment_allowed = True
    n = len(command)
    i = 0

    def flush(*, dynamic_fd_prefix: bool = False) -> None:
        nonlocal buf, shell_buf, shell_raw_boundaries, shell_quoted
        nonlocal buf_quoted, has_char
        nonlocal shell_expansion, unquoted_shell_expansion
        nonlocal multi_argv_shell_expansion, undecodable_ansi_c
        if has_char:
            shell_word = "".join(shell_buf)
            assignment_match = _ENV_ASSIGNMENT.match(shell_word)
            assignment_end = shell_word.find("=")
            assignment_word = bool(
                assignment_match
                and assignment_end >= 0
                and not any(shell_quoted[: assignment_end + 1])
            )
            tokens.append(
                (
                    _BashTokenText(
                        "".join(buf),
                        shell_word,
                        tuple(shell_raw_boundaries),
                        assignment_word,
                        dynamic_fd_prefix,
                        shell_expansion,
                        unquoted_shell_expansion,
                        multi_argv_shell_expansion,
                        undecodable_ansi_c,
                    ),
                    buf_quoted,
                )
            )
        buf = []
        shell_buf = []
        shell_raw_boundaries = [0]
        shell_quoted = []
        buf_quoted = False
        has_char = False
        shell_expansion = False
        unquoted_shell_expansion = False
        multi_argv_shell_expansion = False
        undecodable_ansi_c = False

    while i < n:
        c = command[i]
        # B1: ANSI-C quoting `$'...'`. Bash DECODES the escapes and the result is
        # a literal word, so the gate must see the decoded path. Handled before
        # the ordinary quote branch, which would otherwise emit `$` plus an
        # undecoded single-quoted string and let `$'\x2fprotected...'` through.
        if c == "$" and i + 1 < n and command[i + 1] == "'":
            comment_allowed = False
            j = i + 2
            raw_chars: list[str] = []
            closed = False
            while j < n:
                ch = command[j]
                if ch == "\\" and j + 1 < n:
                    raw_chars.append(ch)
                    raw_chars.append(command[j + 1])
                    j += 2
                    continue
                if ch == "'":
                    closed = True
                    break
                raw_chars.append(ch)
                j += 1
            try:
                if not closed:
                    # Unterminated $'... — Bash would not run this as written;
                    # we cannot know the real target. Mark UNRESOLVED so write
                    # selection fails closed rather than guessing.
                    raise ValueError("unterminated ANSI-C quote")
                decoded = _decode_ansi_c("".join(raw_chars))
            except ValueError:
                undecodable_ansi_c = True
                decoded = ""
                j = n - 1 if not closed else j
            for decoded_char in decoded:
                buf.append(decoded_char)
                shell_buf.append(decoded_char)
                shell_raw_boundaries.append(len(buf))
                shell_quoted.append(True)
            has_char = True
            buf_quoted = True
            i = j + 1
            continue
        if c in "\"'":
            comment_allowed = False
            q = c
            i += 1
            while i < n and command[i] != q:
                quoted_char = command[i]
                if (
                    q == '"'
                    and quoted_char == "\\"
                    and i + 1 < n
                    and command[i + 1] in '$`"\\\n'
                ):
                    escaped = command[i + 1]
                    if escaped == "\n":
                        # Backslash-newline is removed before Bash forms words.
                        # The surrounding double quotes still form a word even
                        # when the continuation was their only content.
                        has_char = True
                        buf_quoted = True
                        i += 2
                        continue
                    buf.extend((quoted_char, escaped))
                    shell_buf.append(escaped)
                    shell_raw_boundaries.append(len(buf))
                    shell_quoted.append(True)
                    has_char = True
                    buf_quoted = True
                    i += 2
                    continue
                buf.append(quoted_char)
                shell_buf.append(quoted_char)
                shell_raw_boundaries.append(len(buf))
                shell_quoted.append(True)
                if q == '"' and (
                    (
                        quoted_char == "$"
                        and _dollar_starts_shell_expansion(command, i)
                    )
                    or quoted_char == "`"
                ):
                    shell_expansion = True
                    if quoted_char == "$" and _dollar_starts_multi_argv_expansion(
                        command, i
                    ):
                        multi_argv_shell_expansion = True
                has_char = True
                buf_quoted = True
                i += 1
            if i < n:
                i += 1  # consume the closing quote
            has_char = True  # even empty quotes form a token
            continue
        if c == "\\":
            if i + 1 < n:
                escaped = command[i + 1]
                if escaped == "\n":
                    # An unquoted continuation contributes no shell character
                    # and, on its own, must not become an empty command word.
                    i += 2
                    continue
                buf.extend((c, escaped))
                shell_buf.append(escaped)
                shell_raw_boundaries.append(len(buf))
                shell_quoted.append(True)
                comment_allowed = False
                has_char = True
                i += 2
                continue
            buf.append(c)
            shell_buf.append(c)
            shell_raw_boundaries.append(len(buf))
            shell_quoted.append(False)
            has_char = True
            comment_allowed = False
            i += 1
            continue
        if c == "\n":
            flush()
            tokens.append((";", False))
            comment_allowed = True
            i += 1
            continue
        if c in " \t":
            flush()
            comment_allowed = True
            i += 1
            continue
        if c in "<>|&;()":
            j = i + 1
            while j < n and command[j] in "<>|&":
                j += 1
            operator = command[i:j]
            shell_word = "".join(shell_buf)
            # Bash's `{name}>file` descriptor allocation is a prefix only
            # when the valid brace word is adjacent to its redirection. Keep
            # that lexical provenance: `{name} >file` remains an ordinary
            # command word. Bash requires the complete `{name}` designator to
            # be unquoted and unescaped; quote removal producing a valid-looking
            # identifier does not turn an ordinary word into an fd prefix.
            dynamic_fd_prefix = bool(
                has_char
                and operator in _FD_PREFIX_REDIR_OPS
                and _DYNAMIC_FD_PREFIX.fullmatch(shell_word)
                and shell_quoted
                and not any(shell_quoted)
            )
            flush(dynamic_fd_prefix=dynamic_fd_prefix)
            tokens.append((operator, False))
            comment_allowed = True
            i = j
            continue
        if c == "#" and comment_allowed:
            while i < n and command[i] != "\n":
                i += 1
            continue
        if (
            (c == "$" and _dollar_starts_shell_expansion(command, i))
            or c == "`"
            or c in "*?["
            or (
                c == "~"
                and _tilde_starts_shell_expansion(
                    "".join(shell_buf), shell_quoted
                )
            )
            or (c == "{" and _brace_starts_shell_expansion(command, i))
        ):
            shell_expansion = True
            unquoted_shell_expansion = True
            if c == "$" and _dollar_starts_multi_argv_expansion(command, i):
                multi_argv_shell_expansion = True
        buf.append(c)
        shell_buf.append(c)
        shell_raw_boundaries.append(len(buf))
        shell_quoted.append(False)
        has_char = True
        comment_allowed = False
        i += 1
    flush()
    return tokens


_CP_SHORT_NO_VALUE = frozenset("abdfiHlLnPpRrsTuvxZ")
_CP_SHORT_VALUE = frozenset("St")
_CP_LONG_VALUE = frozenset({
    "--suffix", "--no-preserve", "--sparse", "--target-directory",
})
_CP_LONG_OPTIONAL_VALUE = frozenset({
    "--backup", "--preserve", "--reflink", "--context",
})
_CP_LONG_NO_VALUE = frozenset({
    "--archive", "--attributes-only", "--copy-contents", "--force",
    "--interactive", "--dereference", "--link", "--no-clobber",
    "--no-dereference", "--parents", "--recursive", "--remove-destination",
    "--strip-trailing-slashes", "--symbolic-link", "--no-target-directory",
    "--update", "--verbose", "--one-file-system", "--help", "--version",
})

_MV_SHORT_NO_VALUE = frozenset("bfinTuvZ")
_MV_SHORT_VALUE = frozenset("St")
_MV_LONG_VALUE = frozenset({"--suffix", "--target-directory"})
_MV_LONG_OPTIONAL_VALUE = frozenset({"--backup"})
_MV_LONG_NO_VALUE = frozenset({
    "--force", "--interactive", "--no-clobber", "--strip-trailing-slashes",
    "--no-target-directory", "--update", "--verbose", "--context",
    "--help", "--version",
})


def _resolve_known_long_option(word: str, known: frozenset[str]) -> str:
    """Resolve an exact or unique GNU long-option spelling, else fail closed."""
    spelling = word.split("=", 1)[0]
    if spelling in known:
        return spelling
    matches = [option for option in known if option.startswith(spelling)]
    if len(matches) != 1:
        raise _UnresolvedBashWriteTarget
    return matches[0]


def _parse_known_short_bundle(
    word: str,
    no_value: frozenset[str],
    value: frozenset[str],
) -> tuple[tuple[str, ...], str | None, str, int]:
    """Return seen flags, first value flag, attached value, and prefix length."""
    seen: list[str] = []
    for index, flag in enumerate(word[1:], start=1):
        if flag in no_value:
            seen.append(flag)
            continue
        if flag in value:
            seen.append(flag)
            return tuple(seen), flag, word[index + 1:], index + 1
        raise _UnresolvedBashWriteTarget
    return tuple(seen), None, "", len(word)


def _parse_cp_mv_args(
    toks: list[tuple[str, bool]], j: int, utility: str
) -> tuple[int, list[str]]:
    """Parse GNU ``cp``/``mv`` options without guessing value consumption."""
    if utility == "cp":
        short_no_value = _CP_SHORT_NO_VALUE
        short_value = _CP_SHORT_VALUE
        long_value = _CP_LONG_VALUE
        long_optional_value = _CP_LONG_OPTIONAL_VALUE
        long_no_value = _CP_LONG_NO_VALUE
    else:
        short_no_value = _MV_SHORT_NO_VALUE
        short_value = _MV_SHORT_VALUE
        long_value = _MV_LONG_VALUE
        long_optional_value = _MV_LONG_OPTIONAL_VALUE
        long_no_value = _MV_LONG_NO_VALUE
    known_long = long_value | long_optional_value | long_no_value

    n = len(toks)
    positionals: list[str] = []
    target_dir: str | None = None
    opt_parsing = True
    while j < n:
        token, quoted = toks[j]
        if not quoted and _OPERATOR_RUN.match(token):
            break
        _refuse_dynamic_bash_parser_word(token, opt_parsing)
        word = _bash_shell_word(token)
        if opt_parsing:
            if word == "--":
                opt_parsing = False
                j += 1
                continue
            if word.startswith("--"):
                option = _resolve_known_long_option(word, known_long)
                if option in long_value:
                    if "=" in word:
                        if option == "--target-directory":
                            prefix = word.split("=", 1)[0] + "="
                            target_dir = _effective_bash_attached_target(
                                word.split("=", 1)[1], prefix, token
                            )
                        j += 1
                    else:
                        value_index = _bash_separate_option_value_index(toks, j)
                        if value_index is None:
                            j += 1
                        elif option == "--target-directory":
                            target_dir = _effective_bash_target(toks[value_index][0])
                            j = value_index + 1
                        else:
                            j = _consume_bash_non_target_option_value(toks, j)
                    continue
                j += 1
                continue
            if word.startswith("-") and word != "-":
                _, value_flag, attached, prefix_len = _parse_known_short_bundle(
                    word, short_no_value, short_value
                )
                if value_flag is None:
                    j += 1
                    continue
                if attached:
                    if value_flag == "t":
                        target_dir = _effective_bash_attached_target(
                            attached, word[:prefix_len], token
                        )
                    j += 1
                    continue
                value_index = _bash_separate_option_value_index(toks, j)
                if value_index is None:
                    j += 1
                elif value_flag == "t":
                    target_dir = _effective_bash_target(toks[value_index][0])
                    j = value_index + 1
                else:
                    j = _consume_bash_non_target_option_value(toks, j)
                continue
        positionals.append(_effective_bash_target(token))
        j += 1
    if target_dir:
        return j, [target_dir]
    return j, ([positionals[-1]] if positionals else [])


_SED_SHORT_NO_VALUE = frozenset("nbErsuz")
_SED_SHORT_VALUE = frozenset("efl")
_SED_LONG_VALUE = frozenset({"--expression", "--file", "--line-length"})
_SED_LONG_OPTIONAL_VALUE = frozenset({"--in-place"})
_SED_LONG_NO_VALUE = frozenset({
    "--quiet", "--silent", "--debug", "--follow-symlinks", "--binary",
    "--posix", "--regexp-extended", "--separate", "--sandbox",
    "--unbuffered", "--null-data", "--help", "--version",
})


def _sed_short_bundle(
    token: str,
) -> tuple[bool, str | None, str | None, bool]:
    """Parse a single-dash sed short-flag bundle (``-n``, ``-ni``, ``-ni.bak``,
    ``-ne``, ``- nei`` …). Returns
    ``(in_place, value_flag, attached_value, consumes_next_token)``.

    Scans left-to-right like GNU sed: ``i`` (ANYWHERE in the bundle) enables
    in-place and consumes the rest of the token as its optional suffix; ``e`` /
    ``f`` / ``l`` take an argument — the rest of the bundle if any chars remain
    (``attached_value``), else the NEXT token (``consumes_next_token``); other
    short flags (n/b/E/r/s/u/z) carry no argument. ``value_flag`` names WHICH
    value option won (``e`` expression → scan, ``f`` file → residual, ``l``
    line-length → ignore). Review fix: keying only on a LEADING ``-i`` missed
    ``-ni``/``-ri``/``-Ei``/``-sni``/``-ni.bak``, a sandbox-gate bypass (the
    write target was never extracted)."""
    k = 1  # skip the leading '-'
    while k < len(token):
        c = token[k]
        if c == "i":
            return True, None, None, False       # rest of token is the -i suffix
        if c in _SED_SHORT_VALUE:
            if k == len(token) - 1:
                return False, c, None, True       # arg is the next token
            return False, c, token[k + 1:], False  # arg attached (rest of bundle)
        if c not in _SED_SHORT_NO_VALUE:
            raise _UnresolvedBashWriteTarget
        k += 1
    return False, None, None, False


# --- sed SCRIPT-level write scanner (M-08 round 16) -------------------------
# A sed script writes files via `w FILE`, `W FILE`, and the `s///w FILE` flag —
# and it does so REGARDLESS of `-i` (round-15 escape: `sed -n 'w /scoped' src`
# streams to stdout yet still writes `/scoped`). The scanner below models GNU
# sed's command grammar so a write primitive is recognized only at a COMMAND
# position and a `w` inside a regex / replacement / address / text argument is
# NOT a false-positive. sed compiles the WHOLE script before executing, so any
# syntax error means NOTHING runs; the scanner raises `_SedScriptUnparseable`
# (carrying the writes found so far) the instant it cannot classify a construct.


class _SedScriptUnparseable(Exception):
    """The sed-script scanner hit a construct it cannot classify confidently.

    ``partial`` holds the write filenames recognized BEFORE the failure. For an
    explicit ``-e``/``--expression`` script the caller fails closed; for a
    positional script — which real sed also rejects, so it compiles to nothing
    and writes nothing — the caller keeps the fail-safe partial and does not
    refuse (this preserves degenerate forms like ``sed -i /vault/f.md``)."""

    def __init__(self, partial: list[str]):
        super().__init__("unparseable sed script")
        self.partial = list(partial)


def _sed_skip_bracket(s: str, i: int) -> int:
    """Skip a regex bracket expression ``[...]`` (``s[i] == '['``).

    Inside a class the active regex delimiter is literal, so the whole class is
    consumed as a unit. Handles a leading ``^``, a literal ``]`` as the first
    member, and ``[:class:]`` / ``[.coll.]`` / ``[=equiv=]`` sub-expressions."""
    n = len(s)
    i += 1  # past '['
    if i < n and s[i] == "^":
        i += 1
    if i < n and s[i] == "]":
        i += 1  # literal ']' as the first class member
    while i < n:
        c = s[i]
        if c == "[" and i + 1 < n and s[i + 1] in ":.=":
            close = s[i + 1] + "]"
            end = s.find(close, i + 2)
            if end < 0:
                raise _SedScriptUnparseable([])
            i = end + 2
            continue
        if c == "]":
            return i + 1
        if c == "\n":
            raise _SedScriptUnparseable([])
        i += 1
    raise _SedScriptUnparseable([])


def _sed_skip_delimited(s: str, i: int, delim: str, honor_brackets: bool) -> int:
    """Skip to just past the next UNESCAPED ``delim`` (a regex or replacement).

    ``i`` points just after the opening delimiter. A backslash escapes the next
    character. When ``honor_brackets`` (regex context) a ``[...]`` class is
    consumed as a unit so a delimiter inside it stays literal. A raw newline
    before the close is a sed syntax error (unterminated)."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                raise _SedScriptUnparseable([])
            i += 2
            continue
        if honor_brackets and c == "[":
            i = _sed_skip_bracket(s, i)
            continue
        if c == delim:
            return i + 1
        if c == "\n":
            raise _SedScriptUnparseable([])
        i += 1
    raise _SedScriptUnparseable([])


def _sed_read_to_eol(
    s: str, i: int, *, skip_leading_blanks: bool
) -> tuple[str, int]:
    """Read a filename/command argument from ``i`` to end-of-line.

    GNU sed reads a ``w``/``W``/``r``/``R``/``s///w`` filename via
    ``in_nonblank()``: it skips leading blanks, then takes every character up to
    (not including) the next newline — ``;`` does NOT terminate it. Returns
    ``(text, stop_index)``."""
    n = len(s)
    if skip_leading_blanks:
        while i < n and s[i] in " \t":
            i += 1
    start = i
    while i < n and s[i] != "\n":
        i += 1
    return s[start:i], i


def _sed_skip_one_address(s: str, i: int, *, allow_relative: bool) -> int:
    """Skip a single sed address at ``i`` (return ``i`` unchanged if none).

    Numeric (``N``, ``first~step``), ``$``, ``/re/``, ``\\cREc`` and — when
    ``allow_relative`` (an addr2 slot) — ``+N``/``~N``. A regex address consumes
    a trailing run of ``I``/``M`` modifiers."""
    n = len(s)
    if i >= n:
        return i
    c = s[i]
    if allow_relative and c in "+~":
        i += 1
        if i >= n or not s[i].isdigit():
            raise _SedScriptUnparseable([])
        while i < n and s[i].isdigit():
            i += 1
        return i
    if c.isdigit():
        while i < n and s[i].isdigit():
            i += 1
        if i < n and s[i] == "~":              # GNU first~step
            i += 1
            if i >= n or not s[i].isdigit():
                raise _SedScriptUnparseable([])
            while i < n and s[i].isdigit():
                i += 1
        return i
    if c == "$":
        return i + 1
    if c == "/":
        i = _sed_skip_delimited(s, i + 1, "/", honor_brackets=True)
    elif c == "\\":
        if i + 1 >= n:
            raise _SedScriptUnparseable([])
        i = _sed_skip_delimited(s, i + 2, s[i + 1], honor_brackets=True)
    else:
        return i  # not an address start
    while i < n and s[i] in "IM":              # regex-address modifiers
        i += 1
    return i


def _sed_skip_addresses(s: str, i: int) -> int:
    """Skip 0, 1, or 2 addresses (``addr`` or ``addr1,addr2``) at ``i``."""
    n = len(s)
    before = i
    i = _sed_skip_one_address(s, i, allow_relative=False)
    if i == before:
        return i  # no addr1 → no address part
    j = i
    while j < n and s[j] in " \t":
        j += 1
    if j < n and s[j] == ",":
        j += 1
        while j < n and s[j] in " \t":
            j += 1
        return _sed_skip_one_address(s, j, allow_relative=True)
    return i


def _sed_scan_s_command(s: str, i: int, writes: list[str]) -> int:
    """Scan an ``s`` command from just after the ``s``; append any ``w`` flag."""
    n = len(s)
    if i >= n:
        raise _SedScriptUnparseable(writes)
    delim = s[i]
    if delim in "\\\n":
        raise _SedScriptUnparseable(writes)
    i = _sed_skip_delimited(s, i + 1, delim, honor_brackets=True)   # regex
    i = _sed_skip_delimited(s, i, delim, honor_brackets=False)      # replacement
    while i < n:                                                     # flags
        c = s[i]
        if c in "gpeiImM" or c.isdigit():
            i += 1
            continue
        if c == "w":                                                # write flag
            fname, i = _sed_read_to_eol(s, i + 1, skip_leading_blanks=True)
            if fname:
                writes.append(fname)
            return i
        break
    return i


def _sed_scan_y_command(s: str, i: int) -> int:
    """Scan a ``y/src/dst/`` transliteration (no write, no flags)."""
    n = len(s)
    if i >= n:
        raise _SedScriptUnparseable([])
    delim = s[i]
    if delim in "\\\n":
        raise _SedScriptUnparseable([])
    i = _sed_skip_delimited(s, i + 1, delim, honor_brackets=False)
    i = _sed_skip_delimited(s, i, delim, honor_brackets=False)
    return i


def _sed_skip_text_argument(s: str, i: int) -> int:
    """Skip an ``a``/``i``/``c`` text argument (one-line or ``\\``-continued)."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n:
                i += 2          # escaped char (incl. `\<newline>` continuation)
                continue
            return i + 1
        if c == "\n":
            return i            # unescaped newline ends the text
        i += 1
    return i


_SED_NOARG_COMMANDS = frozenset("dDgGhHnNpPxzF=")


def _scan_sed_script_writes(script: str) -> list[str]:
    """Return the ``w``/``W``/``s///w`` write filenames in a sed SCRIPT string.

    ``r``/``R`` (reads), branch/label targets, ``a``/``i``/``c`` text, ``y///``,
    and ``q``/``Q``/``l``/``e``/``v`` arguments are consumed but never treated as
    writes. Raises ``_SedScriptUnparseable`` (carrying writes so far) on any
    construct it cannot classify — real sed rejects the whole script too, so no
    write actually runs."""
    writes: list[str] = []
    n = len(script)
    i = 0
    while i < n:
        c = script[i]
        if c in " \t\n;":
            i += 1
            continue
        if c == "#":                       # comment to end of line
            while i < n and script[i] != "\n":
                i += 1
            continue
        if c in "{}":                      # block open / close
            i += 1
            continue
        try:
            i = _sed_skip_addresses(script, i)
            while i < n and script[i] in " \t":
                i += 1
            while i < n and script[i] == "!":      # negation(s)
                i += 1
                while i < n and script[i] in " \t":
                    i += 1
            if i >= n:
                raise _SedScriptUnparseable(writes)
            cmd = script[i]
            i += 1
            if cmd == "s":
                i = _sed_scan_s_command(script, i, writes)
            elif cmd == "y":
                i = _sed_scan_y_command(script, i)
            elif cmd in "wW":
                fname, i = _sed_read_to_eol(script, i, skip_leading_blanks=True)
                if fname:
                    writes.append(fname)
            elif cmd in "rR":              # reads — consume filename, not a write
                _, i = _sed_read_to_eol(script, i, skip_leading_blanks=True)
            elif cmd in "aic":             # appended / inserted / changed text
                i = _sed_skip_text_argument(script, i)
            elif cmd in "btT":             # branch / test → label to ;/}/blank/EOL
                while i < n and script[i] not in "\n;} \t":
                    i += 1
            elif cmd == ":":               # label definition (must be non-empty)
                start = i
                while i < n and script[i] not in "\n;} \t":
                    i += 1
                if i == start:
                    raise _SedScriptUnparseable(writes)
            elif cmd in "qQl":             # optional numeric argument
                while i < n and script[i] in " \t":
                    i += 1
                while i < n and script[i].isdigit():
                    i += 1
            elif cmd == "e":               # execute (RCE residual) — skip to EOL
                _, i = _sed_read_to_eol(script, i, skip_leading_blanks=False)
            elif cmd == "v":               # optional version arg to ; / EOL
                while i < n and script[i] not in "\n;":
                    i += 1
            elif cmd in "{}":              # block open/close — may follow an
                pass                       # address (`/re/{ … }`, `1,5{ … }`);
                                           # the grouped commands are scanned by
                                           # subsequent iterations (round 17 fix).
            elif cmd in _SED_NOARG_COMMANDS:
                pass
            else:
                raise _SedScriptUnparseable(writes)
        except _SedScriptUnparseable:
            # Normalize the partial to the writes accumulated by THIS scan (a
            # helper may have raised with an empty list).
            raise _SedScriptUnparseable(writes)
    return writes


# M-08 residual (round 16) — sed write vectors intentionally NOT modeled here
# (documented, out of scope this round; a fresh adversary should treat these as
# known-open, not new):
#   * `sed -f SCRIPTFILE` — the script is read from a file the hook cannot
#     inspect, so a `w`/`W`/`s///w` inside it is undetectable. Failing closed on
#     ALL `sed -f` would be disproportionate (it breaks every legitimate script
#     file), so `--file`/`-f` only forces file-operand parsing (no positional is
#     mis-scanned) — the file body is left unread.
#   * a DYNAMIC sed script — `-e "$SCRIPT"`, `"w $DEST"`, or a positional whose
#     text carries a shell expansion — is not statically inspectable (same class
#     as `-f`): the literal token text is not the runtime script, so it is not
#     scanned. (An unquoted/`--`-free dynamic positional is still refused by the
#     `_refuse_dynamic_bash_parser_word` guard; a dynamic path buried inside an
#     otherwise-static quoted script value is the residual.)
#   * `sed` `e` / `e command` — executes a shell command (RCE-class); its
#     argument is skipped to end-of-line so later commands still parse, but the
#     executed command itself is beyond write-target detection.
#   * `awk 'BEGIN{print > "/vault/out"}'` — awk is not in the enumerated utility
#     set, so its `>`/`print`/`printf` file writes are not extracted.
#   * interpreter writes beyond the two modeled regexes (`python -c
#     "shutil.copy(...)"`, `os.rename`, `os.open`, …) — see the interpreter
#     residual note above.


def _parse_sed_args(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Parse a `sed` command's arguments starting at token index ``j`` (the token
    AFTER the command word). Returns (stop_index, targets).

    Two independent write channels:
      * FILE OPERANDS are write targets ONLY under in-place editing (``-i`` /
        ``-i.bak`` / ``--in-place`` / ``--in-place=.bak``); without it sed
        streams to STDOUT and every operand is READ-ONLY.
      * SCRIPT-level ``w``/``W``/``s///w`` primitives write their filename
        REGARDLESS of ``-i`` (M-08 round 16). The script comes from the first
        positional (when no ``-e``/``-f``) or from each ``-e``/``--expression``
        value; those filenames are added unconditionally.

    Script args from ``-e``/``--expression``/``-f``/``--file`` consume their
    value and are never file operands. ``--`` ends option parsing; a
    shell-operator token ends the command."""
    n = len(toks)
    in_place = False
    positionals: list[str] = []
    positional_tokens: list[str] = []
    explicit_scripts: list[tuple[str, str]] = []  # (script_text, source_token)
    saw_explicit = False                          # any -e/-f/--expression/--file
    opt_parsing = True
    known_long = _SED_LONG_VALUE | _SED_LONG_OPTIONAL_VALUE | _SED_LONG_NO_VALUE
    while j < n:
        t, q = toks[j]
        if not q and _OPERATOR_RUN.match(t):
            break
        _refuse_dynamic_bash_parser_word(t, opt_parsing)
        word = _bash_shell_word(t)
        if opt_parsing:
            if word == "--":
                opt_parsing = False
                j += 1
                continue
            if word.startswith("--"):
                option = _resolve_known_long_option(word, known_long)
                if option == "--in-place":
                    in_place = True
                    j += 1
                    continue
                if option in _SED_LONG_VALUE:
                    # --expression (scan) / --file (residual) / --line-length.
                    if option in ("--expression", "--file"):
                        saw_explicit = True
                    if "=" in word:
                        if option == "--expression":
                            explicit_scripts.append((word.split("=", 1)[1], t))
                        j += 1
                        continue
                    value_index = _bash_separate_option_value_index(toks, j)
                    if value_index is None:
                        j += 1
                        continue
                    _refuse_splittable_bash_option_value(toks, value_index)
                    if option == "--expression":
                        vtok = toks[value_index][0]
                        explicit_scripts.append((_bash_shell_word(vtok), vtok))
                    j = value_index + 1
                    continue
                j += 1
                continue
            if word.startswith("-") and len(word) > 1:
                # Short-flag bundle — GNU sed parses it left-to-right, so `i`
                # anywhere means in-place (not just a leading -i).
                ip, value_flag, attached, consumes_next = _sed_short_bundle(word)
                if ip:
                    in_place = True
                    j += 1
                    continue
                if value_flag is None:
                    j += 1
                    continue
                if value_flag in ("e", "f"):      # e=expression, f=script file
                    saw_explicit = True
                if not consumes_next:
                    if value_flag == "e" and attached:
                        explicit_scripts.append((attached, t))
                    j += 1
                    continue
                value_index = _bash_separate_option_value_index(toks, j)
                if value_index is None:
                    j += 1
                    continue
                _refuse_splittable_bash_option_value(toks, value_index)
                if value_flag == "e":
                    vtok = toks[value_index][0]
                    explicit_scripts.append((_bash_shell_word(vtok), vtok))
                j = value_index + 1
                continue
        positionals.append(_effective_bash_target(t))
        positional_tokens.append(t)
        j += 1

    # (1) File-operand targets — unchanged contract. With -i EVERY remaining
    # positional is edited in place; without -i they are reads. (The first
    # positional without -e/-f is also the inline script — conservatively kept
    # as a fail-safe target under -i; harmless unless it canonicalizes scoped.)
    targets = list(positionals) if in_place else []

    # (2) SCRIPT-level w / W / s///w writes — extracted regardless of -i.
    scripts: list[tuple[str, str, bool]] = [
        (text, tok, True) for (text, tok) in explicit_scripts
    ]
    if not saw_explicit and positional_tokens:
        first = positional_tokens[0]
        scripts.append((_bash_shell_word(first), first, False))
    for text, tok, is_explicit in scripts:
        if getattr(tok, "shell_expansion", False):
            # Dynamic script (runtime expansion) → not statically inspectable;
            # documented residual (same class as `sed -f`).
            continue
        try:
            found = _scan_sed_script_writes(text)
        except _SedScriptUnparseable as exc:
            if is_explicit:
                # An explicit script we cannot model confidently → fail closed
                # rather than trust our read of it.
                raise _UnresolvedBashWriteTarget
            # A positional script real sed also rejects compiles to nothing, so
            # keep only the fail-safe partial writes found before the failure.
            found = exc.partial
        for fname in found:
            targets.append(_derived_bash_target(fname, tok))
    return j, targets


# rsync SHORT options that take a VALUE (popt: `-e ssh` / `-essh` / bundled
# `-avze ssh`). Case-sensitive — `-t` is preserve-times (NO value) while `-T`
# is temp-dir (value). -e rsh, -B block-size, -T temp-dir, -f filter,
# -M remote-option, -@ modify-window.
_RSYNC_VAL_SHORT = set("eBTfM@")
_RSYNC_NO_VAL_SHORT = frozenset(
    "vqcarRbudlLkKHpEAXogDtSNOJUnWxCIFshzymPiPV0468"
)

# rsync LONG options that take a VALUE (either the NEXT token in space form or
# glued after `=`). M-03: a value placed AFTER the destination MUST be consumed,
# else it is mistaken for the trailing DEST operand and the real dest escapes
# the gate. Enumerated from the rsync(1) option set (value-required options).
#
# M-03 alias follow-up (2026-07-16): rsync spells SOME value-taking options with
# a terse two-letter long alias (the 3.2.0 negotiation options) or a deprecated
# synonym IN ADDITION to the canonical long name. The first pass enumerated only
# the canonical names, so `rsync SRC /scoped/dest --zl 6` treated `--zl` as a
# value-LESS flag and let `6` become the trailing positional — the scoped dest
# escaped the deny gate exactly as the original defect (just a different
# spelling). Both the canonical name AND every value-taking alias must be listed:
#   --zl == --compress-level | --zc == --compress-choice | --cc == --checksum-choice
#   --log-format == --out-format (deprecated synonym)
# The rsync short value-flags (-e/-B/-T/-f/-M/-@, in _RSYNC_VAL_SHORT) have no
# additional aliases. Only genuinely value-REQUIRED options belong here: adding a
# value-less flag would wrongly consume the following token (the real dest).
_RSYNC_VAL_LONG = frozenset({
    "--block-size", "--rsh", "--rsync-path", "--max-delete", "--max-size",
    "--min-size", "--max-alloc", "--partial-dir", "--timeout", "--contimeout",
    "--modify-window", "--temp-dir", "--compare-dest", "--copy-dest",
    "--link-dest", "--compress-level", "--compress-choice", "--skip-compress",
    "--filter", "--exclude", "--exclude-from", "--include", "--include-from",
    "--files-from", "--address", "--port", "--sockopts", "--out-format",
    "--log-file", "--log-file-format", "--password-file", "--early-input",
    "--bwlimit", "--stop-after", "--stop-at", "--time-limit", "--write-batch",
    "--only-write-batch", "--read-batch", "--protocol", "--iconv",
    "--checksum-seed", "--checksum-choice", "--info", "--debug", "--stderr",
    "--usermap", "--groupmap", "--chown", "--chmod", "--suffix",
    "--backup-dir", "--remote-option", "--copy-as", "--config", "--dparam",
    "--outbuf",
    # Terse two-letter aliases + deprecated synonyms (value-taking).
    "--zl", "--zc", "--cc", "--log-format",
})

_RSYNC_NO_VAL_LONG = frozenset({
    "--verbose", "--quiet", "--no-motd", "--checksum", "--archive",
    "--recursive", "--relative", "--no-implied-dirs", "--backup",
    "--update", "--inplace", "--append", "--append-verify", "--dirs",
    "--old-dirs", "--old-d", "--mkpath", "--links", "--copy-links",
    "--copy-unsafe-links", "--safe-links", "--munge-links",
    "--copy-dirlinks", "--keep-dirlinks", "--hard-links", "--perms",
    "--executability", "--acls", "--xattrs", "--owner", "--group",
    "--devices", "--copy-devices", "--write-devices", "--specials",
    "--times", "--atimes", "--open-noatime", "--crtimes",
    "--omit-dir-times", "--omit-link-times", "--super", "--fake-super",
    "--sparse", "--preallocate", "--dry-run", "--whole-file",
    "--one-file-system", "--existing", "--ignore-existing",
    "--remove-source-files", "--delete", "--delete-before",
    "--delete-during", "--delete-delay", "--delete-after",
    "--delete-excluded", "--ignore-missing-args", "--delete-missing-args",
    "--ignore-errors", "--force", "--partial", "--delay-updates",
    "--prune-empty-dirs", "--numeric-ids", "--ignore-times", "--size-only",
    "--fuzzy", "--compress", "--cvs-exclude", "--from0", "--old-args",
    "--secluded-args", "--protect-args", "--trust-sender", "--blocking-io",
    "--stats", "--8-bit-output", "--human-readable", "--progress",
    "--itemize-changes", "--list-only", "--fsync", "--ipv4", "--ipv6",
    "--version", "--help", "--daemon", "--no-detach",
})


# rsync options whose VALUE is a SECONDARY write destination (M-08 round 16).
# `--temp-dir`/`-T` stage transferred files, `--partial-dir` holds partial
# transfers, `--backup-dir` receives displaced originals — all real writes the
# pre-fix parser merely consumed as non-targets. `-T` is the short spelling of
# `--temp-dir` (case-sensitive; `-t` is preserve-times). `--partial-dir` and
# `--backup-dir` have no short form.
_RSYNC_SECONDARY_DIR_LONG = frozenset({
    "--temp-dir", "--partial-dir", "--backup-dir",
})


def _rsync_short_value(
    token: str,
) -> tuple[str | None, str | None, bool]:
    """Parse a single-dash rsync short-flag bundle. Returns
    ``(value_flag, attached_value, consumes_next_token)``.

    popt parses a bundle left-to-right; the FIRST value-taking option
    (``-e``/``-B``/``-T``/``-f``/``-M``/``-@``) consumes the REST of the bundle
    as its arg if any chars remain (``attached_value``), else the NEXT token
    (``consumes_next_token``). ``value_flag`` names WHICH option won so the
    caller can extract ``-T`` (temp-dir, a write destination) while consuming the
    others (`-e ssh`, `-B 1024`, …) as non-targets."""
    k = 1  # skip the leading '-'
    while k < len(token):
        c = token[k]
        if c in _RSYNC_VAL_SHORT:
            if k == len(token) - 1:
                return c, None, True              # arg is the next token
            return c, token[k + 1:], False         # arg attached (rest of bundle)
        if c not in _RSYNC_NO_VAL_SHORT:
            raise _UnresolvedBashWriteTarget
        k += 1
    return None, None, False


def _parse_rsync_args(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Parse an `rsync` command's arguments starting at token index ``j``. Returns
    (stop_index, targets).

    rsync's primary write target is the LAST positional operand
    (`rsync SRC... DEST`). Unlike cp/install, rsync's ``-t`` means
    *preserve-times*, NOT a target directory, so it is an ordinary flag.

    M-08 round 16: the SECONDARY write dirs ``--temp-dir``/``-T`` (and
    ``--partial-dir``/``--backup-dir``) name additional write destinations —
    their values are now extracted as targets, not merely consumed.

    M-03 (2026-07-15): value-taking options MUST have their value consumed.
    The pre-fix parser skipped option TOKENS but never consumed the VALUE of a
    value-taking option (``--exclude PATTERN``, ``-e ssh`` …). That was harmless
    only while the option preceded the destination (its value became a leading
    SOURCE operand and DEST stayed trailing); with the option placed AFTER the
    destination, its space-separated value became the trailing positional and was
    mistaken for the DEST — so the true destination escaped extraction. Now both
    ``--opt value`` / ``--opt=value`` (long) and ``-e value`` / ``-evalue`` /
    bundled ``-avze value`` (short) forms consume the value regardless of
    ordering. A shell-operator token is never swallowed as a value. ``--`` ends
    option parsing; a shell-operator token ends the command."""
    n = len(toks)
    positionals: list[str] = []
    secondary_dirs: list[str] = []   # --temp-dir/--partial-dir/--backup-dir/-T
    opt_parsing = True
    while j < n:
        t, q = toks[j]
        if not q and _OPERATOR_RUN.match(t):
            break
        _refuse_dynamic_bash_parser_word(t, opt_parsing)
        word = _bash_shell_word(t)
        if opt_parsing:
            if word == "--":
                opt_parsing = False
                j += 1
                continue
            if word.startswith("--"):
                option = _resolve_known_long_option(
                    word, _RSYNC_VAL_LONG | _RSYNC_NO_VAL_LONG
                )
                if option in _RSYNC_VAL_LONG:
                    is_dir = option in _RSYNC_SECONDARY_DIR_LONG
                    if "=" in word:
                        if is_dir:
                            prefix = word.split("=", 1)[0] + "="
                            secondary_dirs.append(
                                _effective_bash_attached_target(
                                    word.split("=", 1)[1], prefix, t
                                )
                            )
                        j += 1
                        continue
                    value_index = _bash_separate_option_value_index(toks, j)
                    if value_index is None:
                        j += 1
                        continue
                    _refuse_splittable_bash_option_value(toks, value_index)
                    if is_dir:
                        secondary_dirs.append(
                            _effective_bash_target(toks[value_index][0])
                        )
                    j = value_index + 1
                    continue
                j += 1
                continue
            if word.startswith("-") and len(word) > 1:
                # Short flag / bundle. A value-taking option at the END of the
                # bundle takes the NEXT token as its value (attached values are
                # self-contained). `-T` (temp-dir) is a write destination.
                value_flag, attached, consumes_next = _rsync_short_value(word)
                if value_flag is None:
                    j += 1
                    continue
                if not consumes_next:
                    if value_flag == "T" and attached:
                        prefix = word[: len(word) - len(attached)]
                        secondary_dirs.append(
                            _effective_bash_attached_target(attached, prefix, t)
                        )
                    j += 1
                    continue
                value_index = _bash_separate_option_value_index(toks, j)
                if value_index is None:
                    j += 1
                    continue
                _refuse_splittable_bash_option_value(toks, value_index)
                if value_flag == "T":
                    secondary_dirs.append(
                        _effective_bash_target(toks[value_index][0])
                    )
                j = value_index + 1
                continue
        positionals.append(_effective_bash_target(t))
        j += 1
    # Last operand is the destination; leading operands are read sources. The
    # secondary write dirs (temp/partial/backup) are additional write targets.
    dest = [positionals[-1]] if positionals else []
    return j, dest + secondary_dirs


_INSTALL_SHORT_NO_VALUE = frozenset("bcCdDpsTvZ")
_INSTALL_SHORT_VALUE = frozenset("gmoSt")
_INSTALL_LONG_VALUE = frozenset({
    "--group", "--mode", "--owner", "--strip-program", "--suffix",
    "--target-directory",
})
_INSTALL_LONG_OPTIONAL_VALUE = frozenset({"--backup", "--context"})
_INSTALL_LONG_NO_VALUE = frozenset({
    "--compare", "--directory", "--preserve-timestamps", "--strip",
    "--no-target-directory", "--verbose", "--preserve-context", "--help",
    "--version",
})


def _parse_install_args(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Parse GNU ``install`` options without guessing value consumption."""
    n = len(toks)
    positionals: list[str] = []
    target_dir: str | None = None
    directory_mode = False
    opt_parsing = True
    known_long = (
        _INSTALL_LONG_VALUE
        | _INSTALL_LONG_OPTIONAL_VALUE
        | _INSTALL_LONG_NO_VALUE
    )
    while j < n:
        token, quoted = toks[j]
        if not quoted and _OPERATOR_RUN.match(token):
            break
        _refuse_dynamic_bash_parser_word(token, opt_parsing)
        word = _bash_shell_word(token)
        if opt_parsing:
            if word == "--":
                opt_parsing = False
                j += 1
                continue
            if word.startswith("--"):
                option = _resolve_known_long_option(word, known_long)
                if option == "--directory":
                    directory_mode = True
                if option in _INSTALL_LONG_VALUE:
                    if "=" in word:
                        if option == "--target-directory":
                            prefix = word.split("=", 1)[0] + "="
                            target_dir = _effective_bash_attached_target(
                                word.split("=", 1)[1], prefix, token
                            )
                        j += 1
                    else:
                        value_index = _bash_separate_option_value_index(toks, j)
                        if value_index is None:
                            j += 1
                        elif option == "--target-directory":
                            target_dir = _effective_bash_target(toks[value_index][0])
                            j = value_index + 1
                        else:
                            j = _consume_bash_non_target_option_value(toks, j)
                    continue
                j += 1
                continue
            if word.startswith("-") and word != "-":
                seen, value_flag, attached, prefix_len = _parse_known_short_bundle(
                    word, _INSTALL_SHORT_NO_VALUE, _INSTALL_SHORT_VALUE
                )
                if "d" in seen:
                    directory_mode = True
                if value_flag is None:
                    j += 1
                    continue
                if attached:
                    if value_flag == "t":
                        target_dir = _effective_bash_attached_target(
                            attached, word[:prefix_len], token
                        )
                    j += 1
                    continue
                value_index = _bash_separate_option_value_index(toks, j)
                if value_index is None:
                    j += 1
                elif value_flag == "t":
                    target_dir = _effective_bash_target(toks[value_index][0])
                    j = value_index + 1
                else:
                    j = _consume_bash_non_target_option_value(toks, j)
                continue
        positionals.append(_effective_bash_target(token))
        j += 1
    if directory_mode:
        return j, list(positionals)  # every operand is a created directory
    if target_dir:
        return j, [target_dir]
    return j, ([positionals[-1]] if positionals else [])


def _extract_interpreter_writes(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Scan an interpreter command's arguments (starting at token index ``j``) for
    WRITE-mode file opens embedded in inline code (`python -c "open('X','w')"`,
    including the attached `-c'...'` form and pathlib `.write_text`/`.write_bytes`).
    Returns (stop_index, targets).

    Read-mode opens (`open('X','r')`, `open('X')`) yield NO target, so a `python
    -c` that only reads a scoped path is not falsely denied. Scanning is bounded
    to the interpreter's own command (stops at an unquoted shell operator). The
    regexes only match the Python write idioms, so applying them to non-code
    argument tokens is inert (fail-safe)."""
    n = len(toks)
    targets: list[str] = []
    while j < n:
        t, q = toks[j]
        if not q and _OPERATOR_RUN.match(t):
            break
        word = _bash_shell_word(t)
        for m in _OPEN_WRITE.finditer(word):
            mode = m.group("mode")
            if any(c in mode for c in "wax+"):
                targets.append(_derived_bash_target(m.group("path"), t))
        for m in _PATHLIB_WRITE.finditer(word):
            targets.append(_derived_bash_target(m.group("path"), t))
        j += 1
    return j, targets


def _leading_redirection_end(
    toks: list[tuple[str, bool]],
    i: int,
) -> int | None:
    """Return the token index after a supported leading Bash redirection.

    ``i`` points at the operator (an adjacent numeric fd prefix is handled by
    the caller). A valid redirection consumes one following shell word, whether
    that word is a path, a here-string body, an fd number, or ``-``. If the word
    is absent or another unquoted operator, consume only the redirection token;
    continuing command discovery is the conservative fail-closed behavior for
    malformed/unsupported shapes.
    """
    if i >= len(toks):
        return None
    text, quoted = toks[i]
    if quoted or text not in _LEADING_REDIR_OPS:
        return None
    if i + 1 >= len(toks):
        return i + 1
    operand, operand_quoted = toks[i + 1]
    if not operand_quoted and _OPERATOR_RUN.match(operand):
        return i + 1
    return i + 2


def _parse_tee_args(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Return every tee file operand from one simple command."""
    n = len(toks)
    targets: list[str] = []
    opt_parsing = True
    while j < n:
        token, quoted = toks[j]
        if not quoted and _OPERATOR_RUN.match(token):
            break
        word = _bash_shell_word(token)
        if opt_parsing and word == "--":
            opt_parsing = False
            j += 1
            continue
        if opt_parsing and word.startswith("-"):
            j += 1
            continue
        if word != "-":
            targets.append(_effective_bash_target(token))
        j += 1
    return j, targets


def _parse_dd_args(toks: list[tuple[str, bool]], j: int) -> tuple[int, list[str]]:
    """Return ``of=FILE`` operands from one dd simple command."""
    n = len(toks)
    targets: list[str] = []
    while j < n:
        token, quoted = toks[j]
        if not quoted and _OPERATOR_RUN.match(token):
            break
        _refuse_dynamic_bash_parser_word(
            token, _dd_dynamic_word_can_select_output(token)
        )
        word = _bash_shell_word(token)
        if word.startswith("of="):
            targets.append(
                _effective_bash_attached_target(word[3:], "of=", token)
            )
        j += 1
    return j, targets


# Windows copy utilities callable from the Git-Bash runtime. ``xcopy`` and
# ``robocopy`` are real ``System32`` executables; ``copy`` is a cmd built-in and
# only runs via an interpreter, but is dispatched here for completeness. They
# name a write destination the same way ``cp`` does — the finding's own
# path-qualified ``C:\Windows\System32\xcopy`` example — so a path-qualified or
# upper-case spelling must classify, not bypass.
_WINDOWS_COPY_UTILITIES = frozenset({"xcopy", "robocopy", "copy"})


def _parse_windows_copy_args(
    toks: list[tuple[str, bool]], j: int
) -> tuple[int, list[str]]:
    r"""Return every file operand from a Windows copy utility (xcopy/robocopy/copy).

    These tools interleave ``/SWITCH`` flags with positional operands, and their
    switch sigil ``/`` collides with a POSIX absolute path on the Git-Bash
    runtime, so the destination cannot be isolated by position without risking a
    missed target. Every non-operator operand is therefore routed through the
    path gate; over-collecting a read source or a ``/Y``-style switch is
    fail-safe (an unscoped operand is simply allowed, never refused). This is a
    spelling-level classifier, not an OS boundary — an interpreter-mediated copy
    such as ``cmd /c copy`` or ``powershell Copy-Item`` remains an acknowledged
    non-airtight limit (M-08).
    """
    n = len(toks)
    targets: list[str] = []
    while j < n:
        token, quoted = toks[j]
        if not quoted and _OPERATOR_RUN.match(token):
            break
        _refuse_dynamic_bash_parser_word(token, True)
        targets.append(_effective_bash_target(token))
        j += 1
    return j, targets


def _bash_copy_move_sed_targets(toks: list[tuple[str, bool]]) -> list[str]:
    """Companion to `_bash_write_targets`: extract utility write targets.

    Only a token at a COMMAND boundary (input start, or
    right after a `; | & && || ( )` separator, past leading env-assignments) is
    treated as a command word — so a `cp` that is merely an argument to another
    command (`echo cp a b`) or inside quotes is never mis-dispatched."""
    targets: list[str] = []
    n = len(toks)
    i = 0
    at_cmd_start = True
    while i < n:
        text, quoted = toks[i]
        if not quoted and _OPERATOR_RUN.match(text):
            if at_cmd_start and not _CMD_SEPARATOR.match(text):
                redirect_end = _leading_redirection_end(toks, i)
                if redirect_end is not None:
                    i = redirect_end
                    continue
            if _CMD_SEPARATOR.match(text):
                at_cmd_start = True  # ; | & ( ) → next word starts a new command
            # redirect operators (>, >>, >|, >&) do NOT start a new command
            i += 1
            continue
        if at_cmd_start:
            # The tokenizer separates the adjacent fd number in `2>file`
            # from the operator. A spaced numeric command followed by a
            # redirect is conservatively treated the same way: over-matching
            # can only add a sandbox check, never allow a missed destination.
            numeric_fd_prefix = not quoted and text.isdigit()
            dynamic_fd_prefix = getattr(text, "dynamic_fd_prefix", False)
            if (numeric_fd_prefix or dynamic_fd_prefix) and i + 1 < n:
                redirect_end = _leading_redirection_end(toks, i + 1)
                if redirect_end is not None:
                    i = redirect_end
                    continue
            if getattr(
                text,
                "assignment_word",
                not quoted and bool(_ENV_ASSIGNMENT.match(text)),
            ):
                i += 1  # leading VAR=value assignment — command word is still ahead
                continue
            # Quotes and unquoted backslash escapes may jointly construct an
            # executable word. The tokenizer records shell normalization with
            # per-segment provenance, so r"sy"n\c is rsync while "r\sync"
            # retains its literal quoted backslash. Operands keep their original
            # spelling, including Windows path separators.
            cmd = _command_dispatch_name(text)
            at_cmd_start = False
            if cmd in ("cp", "mv"):
                i, ts = _parse_cp_mv_args(toks, i + 1, cmd)
                targets.extend(ts)
                continue
            if cmd == "rsync":
                i, ts = _parse_rsync_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd == "install":
                i, ts = _parse_install_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd == "sed":
                i, ts = _parse_sed_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd == "tee":
                i, ts = _parse_tee_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd == "dd":
                i, ts = _parse_dd_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd in _WINDOWS_COPY_UTILITIES:
                i, ts = _parse_windows_copy_args(toks, i + 1)
                targets.extend(ts)
                continue
            if cmd in _INTERPRETERS:
                i, ts = _extract_interpreter_writes(toks, i + 1)
                targets.extend(ts)
                continue
            at_cmd_start = False
            i += 1
            continue
        i += 1
    return targets


def _bash_write_targets(command: str | None) -> list[str]:
    """Extract filesystem write targets from a Bash command so shell writes
    (`>`, `>>`, `>|`, `&>`, `&>>`, pathname `>&`, `<>`, `tee` (every file
    operand), `dd of=`, `cp`/`mv`/`sed -i`, `rsync`/`install` destinations, and
    interpreter writes — `python -c
    "open('X','w')"` / pathlib `.write_text`) get run through the same path gates
    as the Write/Edit tools. Over-matching is fail-safe (an extra non-scoped
    target is simply allowed). A selected target with parameter, command,
    arithmetic, tilde, glob, or brace expansion raises an explicit unresolved
    signal so the hook refuses rather than trusting its source spelling as a
    literal path. Non-tuple MCP write tools remain a scoped follow-up."""
    if not command or not isinstance(command, str):
        return []
    toks = _tokenize_bash(command)
    targets: list[str] = []
    n = len(toks)
    i = 0
    while i < n:
        text, quoted = toks[i]
        if not quoted and (
            text in _WRITE_PATH_REDIR_OPS or _PURE_REDIR.match(text)
        ):
            if i + 1 < n:
                operand, operand_quoted = toks[i + 1]
                if operand_quoted or not _OPERATOR_RUN.match(operand):
                    targets.append(_effective_bash_target(operand))
                    i += 2
                    continue
            i += 1
            continue
        if not quoted and text == ">&":
            if i + 1 < n:
                operand, operand_quoted = toks[i + 1]
                if operand_quoted or not _OPERATOR_RUN.match(operand):
                    fd_word = getattr(operand, "shell_word", operand)
                    if not _FD_NUMBER_OR_CLOSE.fullmatch(fd_word):
                        # Without an fd number or `-`, Bash's unprefixed
                        # `>&word` spelling opens a pathname for both streams.
                        # Unknown expansion shapes stay path-like (fail-safe)
                        # rather than being guessed into fd duplication.
                        targets.append(_effective_bash_target(operand))
                    i += 2
                    continue
            i += 1
            continue
        if not quoted and (text == "<&" or _DUP_REDIR.match(text)):
            # Input-fd duplication/closure is not a write path. Consume its
            # operand so a numeric fd or `-` cannot be reinterpreted later.
            if i + 1 < n:
                operand, operand_quoted = toks[i + 1]
                if operand_quoted or not _OPERATOR_RUN.match(operand):
                    i += 2
                    continue
            i += 1
            continue
        i += 1
    # Utility write targets use the same tokens and command-boundary state.
    targets.extend(_bash_copy_move_sed_targets(toks))
    if any(getattr(t, "shell_expansion", False) for t in targets):
        raise _UnresolvedBashWriteTarget
    seen: set[str] = set()
    out: list[str] = []
    for t in targets:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# H-11 (2026-07-14) — Codex apply_patch target extraction. Codex's primary write
# path is a patch whose file targets live inside the patch TEXT, not an explicit
# file_path field, so the path-keyed innate/sandbox/cap gates never saw them.
# These markers are the apply_patch envelope's operation headers.
_PATCH_BEGIN_MARKER = "*** Begin Patch"
_PATCH_FILE_OP_LINE = re.compile(r'^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$')
_PATCH_MOVE_OP_LINE = re.compile(r'^\*\*\*\s+Move\s+to:\s*(.+?)\s*$')

# H-11 extension (2026-07-15) — scope patch extraction to a REAL apply_patch/exec
# op. Codex's apply_patch always rides an exec-style tool (shell/exec/bash/a
# dedicated apply_patch tool). A Write/Edit/MultiEdit is a file-CONTENT tool and
# can never be a real patch op — its payload IS the file body, so a DOCUMENTATION
# Write whose content merely embeds a patch example must NOT be scanned like a
# real patch (the pre-fix over-deny). Reads can't write at all. Excluding this
# closed set keeps every exec/patch tool — including an unknown future Codex tool
# name — covered (fail-safe toward guarding real writes).
_NON_PATCH_TOOLS = frozenset({
    "write", "edit", "multiedit", "notebookedit",
    "read", "grep", "glob", "notebookread",
})
_TOOL_NAME_UNSET = object()


def _is_patch_capable_tool(tool_name: str | None) -> bool:
    """True when `tool_name` could be a real apply_patch/exec op — i.e. it is a
    non-empty name that is NOT a file-content or read tool."""
    return bool(tool_name) and tool_name.lower() not in _NON_PATCH_TOOLS


def _parse_patch_text(patch_text: str) -> list[dict]:
    """Parse one apply_patch envelope into per-file sections.

    Returns a list of ``{"targets": [...], "added_lines": [...]}`` — one entry
    per file operation, in document order. ``targets`` holds the operation's
    file path plus any ``*** Move to:`` destination (a move writes the
    destination and mutates the source, so BOTH must face the gates).
    ``added_lines`` are the hunk's ``+``-prefixed additions with the leading
    ``+`` stripped, so the MEMORY.md cap can validate exactly what the patch
    would add."""
    sections: list[dict] = []
    current: dict | None = None
    for line in patch_text.splitlines():
        m_file = _PATCH_FILE_OP_LINE.match(line)
        if m_file:
            current = {"targets": [m_file.group(1).strip()], "added_lines": []}
            sections.append(current)
            continue
        m_move = _PATCH_MOVE_OP_LINE.match(line)
        if m_move:
            dest = m_move.group(1).strip()
            if current is None:
                current = {"targets": [], "added_lines": []}
                sections.append(current)
            if dest and dest not in current["targets"]:
                current["targets"].append(dest)
            continue
        if current is not None and line.startswith("+") and not line.startswith("+++"):
            current["added_lines"].append(line[1:])
    return sections


def _extract_patch_sections(payload: dict, tool_name=_TOOL_NAME_UNSET) -> list[dict]:
    """Return every apply_patch section across tool_input's string values.

    Keys on the patch markers, so the extractor is representation-agnostic (the
    patch can ride in `command`, `input`, `patch`, or `content`). When
    `tool_name` is supplied, extraction is SCOPED to a real apply_patch/exec op
    (`_is_patch_capable_tool`) — a Write/Edit whose content embeds a patch
    example yields nothing. Omitting `tool_name` (the default) keeps the
    representation-agnostic behavior for unit tests / callers that gate
    elsewhere."""
    if tool_name is not _TOOL_NAME_UNSET and not _is_patch_capable_tool(tool_name):
        return []
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return []
    sections: list[dict] = []
    for value in tool_input.values():
        if not isinstance(value, str) or _PATCH_BEGIN_MARKER not in value:
            continue
        sections.extend(_parse_patch_text(value))
    return sections


def _extract_patch_targets(payload: dict, tool_name=_TOOL_NAME_UNSET) -> list[str]:
    """Extract every filesystem target from a Codex apply_patch payload (H-11).

    A patch carries its targets in the patch TEXT — `*** Add File:`,
    `*** Update File:`, `*** Delete File:`, and `*** Move to:` — never an
    explicit `file_path`, so the path-keyed innate/sandbox/cap gates skipped
    them. BOTH the Update/Delete source and the `Move to:` destination are
    returned. Over-matching is fail-safe: an extra non-scoped target is simply
    allowed; the GATE decides scope. See `_extract_patch_sections` for the
    `tool_name` scoping (the documentation over-deny fix)."""
    targets: list[str] = []
    seen: set[str] = set()
    for section in _extract_patch_sections(payload, tool_name):
        for path in section["targets"]:
            if path and path not in seen:
                seen.add(path)
                targets.append(path)
    return targets


def main() -> int:
    try:
        from guard_settings import policy_scope
    except Exception:
        emit_stderr('ALLOSTAT SETTINGS UNAVAILABLE: shipped guard defaults remain active.')
        return _main_with_policy()
    with policy_scope() as policy:
        if policy.health == 'invalid':
            emit_stderr('ALLOSTAT SETTINGS INVALID: using enabled shipped defaults; run guards status/reset.')
        return _main_with_policy()


def _main_with_policy() -> int:
    """Top-level crash armor (pre-release item 11).

    _base.py contract: hooks never raise — a broken plugin update must
    not traceback on every customer prompt. Any unexpected exception
    degrades to a stderr line + exit 0. Intentional non-zero returns
    (innate-rule / sandbox refusals exit 2) pass through unchanged.
    """
    global _confirmed_action, _tool_refused
    _confirmed_action = None
    _tool_refused = False
    try:
        result = _main()
        if _confirmed_action is not None and not _tool_refused:
            from destructive_confirmation import consume, refusal_message
            if not consume(**_confirmed_action):
                return refuse_tool_call(refusal_message(**_confirmed_action))
        return result
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"pre-tool-use hook crashed (armor): {e.__class__.__name__}")
        if _confirmed_action is not None:
            return refuse_tool_call("DESTRUCTIVE ACTION PAUSED: confirmation could not be completed.")
        return 0


def _main() -> int:
    global _confirmed_action
    if disabled():
        return 0
    payload = read_payload()
    project_root = resolve_project_root_from_payload(payload)

    from local_state import resolve_state_dir, append_observation  # noqa: E402

    state_dir = resolve_state_dir(project_root) if project_root else None

    def _invalidate_confirmation() -> None:
        # Degradation is never a route to retaining stale approval. Keep this
        # independent of the failing rule module and never echo action values.
        try:
            from destructive_confirmation import invalidate
            invalidate(state_dir, session_id=payload.get('session_id') or payload.get('sessionId') or '',
                       harness=detect_harness())
        except Exception as exc:
            emit_stderr(f'confirmation cancellation unavailable: {type(exc).__name__}')

    def _observe(event: str, details: dict) -> None:
        if state_dir is None:
            return
        try:
            append_observation(state_dir, event, details=details)
        except Exception as exc:  # noqa: BLE001
            emit_stderr(f"{event} observation failed: {exc.__class__.__name__}")

    # S2 Block 3b (2026-05-27), DECOUPLED FROM SAFETY (item #5, 2026-07-14).
    #
    # The deactivation guard used to early-noop the ENTIRE hook whenever
    # entitlement wasn't active — which took the 12 local SAFETY rules
    # (destructive-command, sandbox, MEMORY.md-cap) down with it. A 300s
    # control-plane outage fail-closes a still-PAYING user to "unknown", so that
    # design silently stripped their safety floor for the outage window.
    #
    # Now the split is explicit:
    #   - Server-CONFIRMED terminal-inactive (lapsed/canceled) → fully dormant
    #     (operator's silent-removal-on-lapse — unchanged).
    #   - Everything else (active/trial/unknown/missing) → the SAFETY gates below
    #     ALWAYS run; only the value-add regulation is gated on live entitlement
    #     (`_entitled`).
    if is_terminally_inactive(state_dir):
        return 0
    _entitled = should_inject(state_dir)

    tool_name, command, file_path = _extract_tool_attributes(payload)
    # Codex's native patch tool and Bash both place text in `command`.
    # Only the exact native operation gets data semantics; shells and MCP
    # lookalikes retain command matching. Every target still faces the gates.
    if tool_name == 'apply_patch':
        try:
            from native_patch import parse_native_patch
            patch_ops = parse_native_patch(payload.get('tool_input') or payload.get('toolInput'))
        except Exception:
            _invalidate_confirmation()
            return refuse_tool_call('PATCH PAUSED: invalid or ambiguous native patch envelope.')
        if any(o['kind'] == 'Delete' or len(o['targets']) > 1 for o in patch_ops):
            # Semantic classifier input only, NEVER executed. The bound action
            # below includes the entire original patch, not this class label.
            command = 'rm -rf [native-patch-removal]'
        else:
            command = None
    _destructive_candidate = _destructive_action_candidate(tool_name, command)

    try:
        import send_authorization  # noqa: E402
        _legacy_state_clean = send_authorization.purge_untrusted_authority_state(state_dir)
        # These legacy files are inert and never consulted for authorization.
        # Do not veto ordinary development based on their names in tool text.
        if not _legacy_state_clean and state_dir is not None:
            append_observation(
                state_dir, "untrusted_authority_state_cleanup_failed",
                details={"state_classes": len(send_authorization.LEGACY_STATE_DIRNAMES)},
            )
    except Exception as e:  # noqa: BLE001
        emit_stderr(f"authorization-state cleanup failed: {type(e).__name__}")

    # Pre-release item 12 — file_created reachability. PostToolUse fires
    # after the Write completed (target always exists by then), so the
    # create-vs-overwrite distinction must be probed HERE and stashed for
    # the PostToolUse classifier to consume.
    if _entitled and state_dir is not None and file_path and (tool_name or "").lower() == "write":
        try:
            from write_existence_stash import stash_pre_write_existence  # noqa: E402
            stash_pre_write_existence(state_dir, str(file_path))
        except Exception as e:
            emit_stderr(f"pre-write existence stash failed: {e.__class__.__name__}")

    # PATCH-193 / Phase 3.3b — rollout_detected producer.
    #
    # Producer for the `rollout_detected` observation that volume-control's
    # `detect_rollout_unrecorded` consumes. Pre-PATCH-193 zero wrapper-side
    # code generated this event; volume-control's rollout branch was
    # structurally unable to fire. Scans Bash commands against a curated
    # high-precision deploy-pattern catalog; appends observation on match.
    # Operator opt-out via ALLOSTAT_ROLLOUT_DETECTOR=0; custom patterns
    # via ALLOSTAT_ROLLOUT_PATTERNS (semicolon-separated regexes).
    if False and (  # superseded: detector runs after risk gates below
        _entitled and state_dir is not None and tool_name and command
        and not _destructive_candidate
    ):
        try:
            import rollout_detector  # noqa: E402
            rollout_detector.detect_and_emit(state_dir, tool_name, command)
        except Exception as e:
            emit_stderr(f"rollout_detector failed: {e.__class__.__name__}")

    # Phase 3.3c (2026-05-24): topology-change detector.
    # Producer for the `topology_change_detected` observation that
    # volume-control's `detect_topology_change_unrecorded` consumes.
    # Pre-Phase-3.3c zero wrapper-side code generated this event; pillar
    # branch structurally unable to fire. Scans Bash commands against a
    # high-precision topology-pattern catalog (git mv, git rebase, git
    # filter-*, mkdir -p, rmdir, rm -r, find -exec mv). Opt-out via
    # ALLOSTAT_TOPOLOGY_DETECTOR=0; custom patterns via
    # ALLOSTAT_TOPOLOGY_PATTERNS (semicolon-separated regexes).
    if False and (  # superseded: detector runs after risk gates below
        _entitled and state_dir is not None and tool_name and command
        and not _destructive_candidate
    ):
        try:
            import topology_change_detector  # noqa: E402
            topology_change_detector.detect_and_emit(state_dir, tool_name, command)
        except Exception as e:
            emit_stderr(f"topology_change_detector failed: {e.__class__.__name__}")

    # PATCH-183.1 file_slice instrumentation REMOVED (pillar-wiring 2026-06-05):
    # the capture wrote a `file_slice` observation on every Read/Edit/Write tool
    # call, but its only consumer (handoff_consolidation, deleted S1) is gone —
    # write-only dead data that grew observations.jsonl every tool call. Cut the
    # capture + the file_slice_trailer lib (truly-dead per the dormancy report).

    # C-06 (Wave-2 2026-07-11) — rule 04 context flags, computed BEFORE the
    # local innate match. Rule 04 (canonical-verification, severity lethal)
    # triggers on match_all over two filesystem-context flags; the local match
    # below used to receive context_flags=None ("computed later; deferred"),
    # so the only ENFORCING path could never fire it — the flags were computed
    # only for the server dispatch, whose response is advisory-only. The same
    # values are reused for that dispatch further down (computed once).
    # Crash-armored + cheap: one sibling-directory scan; any failure degrades
    # to empty flags == the pre-C-06 behavior (never a crash, never a false
    # refusal — rule 04 just stays silent for this call).
    context_flags: list[str] = []
    sibling_list: list[str] = []
    if file_path and (tool_name or "").lower() in ("edit", "write", "notebookedit", "multiedit"):
        try:
            from filesystem_context import detect_canonical_context  # noqa: E402
            fs_ctx = detect_canonical_context(file_path)
            if fs_ctx.flags:
                context_flags = sorted(fs_ctx.flags)
                sibling_list = list(fs_ctx.sibling_folder_names)
        except Exception as e:
            emit_stderr(f"detect_canonical_context failed: {e.__class__.__name__}")

    # PATCH-143 v1.2.0 — Innate-rule evaluation (wrapper-local).
    #
    # The 10+ innate rules (destructive commands, secrets paths, canonical
    # workspace, etc.) used to be evaluated server-side via
    # dispatch_pre_tool_use. Under any network latency the server's decision
    # arrived after Claude Code already proceeded — Allostat "lost the race"
    # in the 2026-05-21 destructive-delete incident. Audit Phase 4 A1
    # corrected the architectural read: refusal-teeth pillars must be
    # wrapper-local so eval happens in-process before tool execution starts.
    #
    # Rules with severity in {"lethal","critical"} → exit(2) refusal.
    # Lower severities surface chrome only (existing soft-pillar pattern).
    #
    # The local state machines from the blocked R3 candidate are deliberately
    # absent here: project-local files cannot authenticate operator authority.
    # This gate classifies and refuses without minting or spending local state.
    _operation = None
    _operation_risk = None
    try:
        from innate_rules import (  # noqa: E402
            match_pre_tool_call,
            is_refusal_severity,
        )
        # Compute operation hint from tool_name. Values map to the literal
        # strings the rule YAMLs expect — `write`/`edit` (mutating; rule 01
        # secrets-protection gates [write, edit], rule 04
        # canonical-verification gates edit) and `read` (W2-D audit H-09:
        # known read-only tools get a first-class operation so a
        # mutating-op-gated rule does NOT fire on them — pre-fix, Read/Grep
        # arrived as None and innate-03 hard-refused READS of *_LEGACY_*
        # paths). Bash leaves operation as None — Bash rules trigger on
        # command_pattern, not operation. Tools ABSENT from this map also
        # resolve to None, which the matcher's match_any file gate treats
        # fail-safe (an op-gated rule still fires on a path match), so
        # genuinely unknown tools stay guarded.
        _op_map = {
            "write": "write",
            "edit": "edit",
            "notebookedit": "edit",
            # MultiEdit is an edit (arbiter reconciliation, Wave-2 2026-07-11):
            # W2-C left it unmapped so the write-gated secrets rule would still
            # fire via the None fail-safe; W2-D then widened rules 01/03 to
            # `operation: [write, edit]`, so classifying MultiEdit as `edit`
            # keeps those firing AND lets the lethal rule 04 (match_all needs
            # operation==edit) fire on a MultiEdit into a duplicate folder —
            # completing C-06 for MultiEdit instead of leaving it a blind spot.
            "multiedit": "edit",
            # Known read-only tools (W2-D H-09) — must classify as `read`,
            # never fall through to the None fail-safe.
            "read": "read",
            "grep": "read",
            "glob": "read",
            "notebookread": "read",
        }
        _operation = _op_map.get((tool_name or "").lower())
        # C02 (2026-08-03): tools outside the file map get the semantic
        # classifier — the adapter between app/MCP tool names and rules
        # 09/10's operation vocabulary. Crash-armored: a classifier failure
        # degrades to None (strict no-fire), never to a refusal or a crash.
        if _operation is None:
            try:
                _operation = _classify_semantic_operation(tool_name, payload)
            except Exception as e:  # noqa: BLE001
                emit_stderr(
                    f"semantic operation classifier failed: {e.__class__.__name__}"
                )
                _operation = None
            # F02: still unrecognized, and it looked like a grant. Do not
            # refuse what cannot be classified — but do not be silent about
            # it either. Bookkeeping only; never changes the outcome.
            if _operation is None:
                try:
                    _record_unclassified_permission_shape(state_dir, tool_name)
                except Exception as e:  # noqa: BLE001
                    emit_stderr(
                        f"permission-shape recorder failed: {e.__class__.__name__}"
                    )

        try:
            from operation_risk import classify_operation_risk
            _operation_risk = classify_operation_risk(_operation)
        except Exception as e:  # noqa: BLE001
            emit_stderr(f"operation risk classification failed: {type(e).__name__}")
            _operation_risk = None

        # The mandatory send guard is retired. Host authorization still applies;
        # the memory plugin does not add a second, unreleasable send refusal.
        innate_match = match_pre_tool_call(
            tool_name=tool_name,
            command=command,
            file_path=file_path,
            operation=_operation,
            # C-06: real flags (hoisted above) so lethal rule 04 can refuse
            # locally; sibling_list feeds the red box's {sibling_list}.
            context_flags=set(context_flags),
            sibling_list=sibling_list,
        )
        # ISSUE-005 (2026-08-07): the pattern set above reads the command
        # string, so `bash /tmp/cl.sh` says nothing about the `rm -rf` inside
        # the script. Checked only when nothing already fired — this adds
        # coverage, it never overrides a rule that matched on its own evidence.
        # Its own try/except: a bookkeeping failure here must not route into
        # _innate_evaluator_failed and start refusing ordinary commands.
        if innate_match is None and command:
            try:
                from innate_rules import session_written_script_match  # noqa: E402
                from session_script_tracker import read_written  # noqa: E402

                innate_match = session_written_script_match(
                    command,
                    set(read_written(
                        state_dir,
                        payload.get("session_id") or payload.get("sessionId"),
                    )),
                )
            except Exception as script_err:  # noqa: BLE001
                emit_stderr(
                    f"session-script check failed: {script_err.__class__.__name__}"
                )
        # 2026-08-02 — a dropped rule must not be a silent rule. The gate in
        # _build_working_set removes any edited/duplicate/unknown rule with
        # one logger.warning nothing renders on ALLOWED calls, and the loud
        # banner runs at session START only — so a mid-session drop guards
        # nothing and tells no one until the next restart. Measured the same
        # day: the server ran 11 of 12 rules and only CI said so. This writes
        # ONE durable observation per distinct degradation (cross-process
        # dedup via a state-dir marker; the hook is a fresh process per tool
        # call, so in-memory state cannot dedup).
        #
        # Its own try/except, NOT the enclosing one: the enclosing handlers
        # route to _innate_evaluator_failed, which refuses destructive-looking
        # commands — an announcement bug must never convert into refusals.
        try:
            _announce_innate_rule_drops(
                state_dir,
                payload.get("session_id") or payload.get("sessionId"),
            )
        except Exception as announce_err:  # noqa: BLE001
            emit_stderr(
                f"innate drop announcement failed: {announce_err.__class__.__name__}"
            )
        if innate_match is not None and innate_match.rule_id == "innate-02":
            try:
                from innate_rules import destructive_guard_degraded
                if destructive_guard_degraded()[0]:
                    _invalidate_confirmation()
                    return refuse_tool_call(
                        'DESTRUCTIVE ACTION PAUSED: protection is degraded. Reinstall a verified '
                        'plugin before retrying. This runtime cannot authenticate operator authorization '
                        'to override missing protection; confirmation unavailable. '
                        'No local override phrase releases degraded protection.'
                    )
                from destructive_confirmation import pending_or_approved, refusal_message
                confirmation_scope = {
                    "state_dir": state_dir,
                    "action": {
                        "tool": tool_name,
                        "input": payload.get("tool_input") or payload.get("toolInput") or {},
                        "command": command, "file_path": file_path,
                        "cwd": payload.get("cwd"),
                    },
                    "session_id": payload.get("session_id") or payload.get("sessionId") or "",
                    "harness": detect_harness(), "turn_id": payload.get("turn_id"),
                }
                if not pending_or_approved(**confirmation_scope):
                    return refuse_tool_call(refusal_message(**confirmation_scope))
                _confirmed_action = confirmation_scope
                # A confirmation never suppresses a later matching rule.
                innate_match = match_pre_tool_call(
                    tool_name=tool_name, command=command, file_path=file_path,
                    operation=_operation, context_flags=set(context_flags),
                    sibling_list=sibling_list, confirmed_destructive=True,
                )
            except Exception as exc:
                emit_stderr(f"destructive confirmation failed: {type(exc).__name__}")
                return refuse_tool_call("DESTRUCTIVE ACTION PAUSED: confirmation state is unavailable.")
        if innate_match is not None:
            # Legacy compatibility for older matchers. Current matchers retire
            # innate-09 independently of the temporary environment suspension.
            if innate_match.overridden and innate_match.rule_id == "innate-09":
                if state_dir is not None:
                    try:
                        append_observation(state_dir, "innate_rule_overridden", details={
                            "rule_id": innate_match.rule_id,
                            "rule_name": innate_match.rule_name,
                            "severity": innate_match.severity,
                            "tool_name": tool_name,
                            "file_path_digest": (
                                hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()
                                if file_path else None
                            ),
                            "command_digest": (
                                hashlib.sha256(str(command).encode("utf-8")).hexdigest()
                                if command else None
                            ),
                        })
                    except Exception as e:  # noqa: BLE001
                        emit_stderr(
                            "innate override telemetry failed: "
                            f"{e.__class__.__name__}"
                        )
                # No emit_additional_context, no return — fall through to
                # normal pre-tool-use flow.
            else:
                if state_dir is not None:
                    _action_digest = hashlib.sha256(json.dumps(
                        dict(tool=tool_name, command=command, file_path=file_path),
                        sort_keys=True, separators=(",", ":"), default=str,
                    ).encode("utf-8")).hexdigest()
                    if innate_match.rule_id == "innate-02":
                        # Destructive observations contain only class, digest,
                        # and count. The event name supplies the fired state.
                        action_material = {
                            "action_class": "destructive",
                            "action_digest": _action_digest,
                            "target_count": int(bool(command)) + int(bool(file_path)),
                        }
                    else:
                        action_material = {
                            "rule_id": innate_match.rule_id,
                            "rule_name": innate_match.rule_name,
                            "severity": innate_match.severity,
                            "response_action": innate_match.response_action,
                            "tool_name": tool_name,
                            "target_count": int(bool(command)) + int(bool(file_path)),
                            "action_digest": _action_digest,
                            "refused": is_refusal_severity(innate_match.severity),
                        }
                    try:
                        append_observation(
                            state_dir, "innate_rule_fired", details=action_material
                        )
                    except Exception as e:  # noqa: BLE001
                        # A failed audit sink cannot release a matched action.
                        emit_stderr(
                            f"innate refusal telemetry failed: {e.__class__.__name__}"
                        )
                if is_refusal_severity(innate_match.severity):
                    emit_stderr(
                        f"Allostat innate rule {innate_match.rule_id} refused: "
                        f"{innate_match.rule_name}"
                    )
                    if innate_match.rule_id == "innate-02":
                        refusal_text = (
                            "DESTRUCTIVE ACTION PAUSED\n\nThis hook runtime cannot authenticate "
                            "operator authorization against same-user executors. No local "
                            "override phrase can release this action in the partial build."
                        )
                    elif innate_match.rule_id == "innate-01":
                        refusal_text = (
                            "PROTECTED WRITE PAUSED\n\nNo trusted local confirmation boundary "
                            "is available. Use a non-writing workflow."
                        )
                    elif innate_match.rule_id == "innate-10":
                        refusal_text = (
                            "PERMISSION OR FINANCIAL ACTION PAUSED\n\nNo trusted local "
                            "confirmation boundary is available."
                        )
                    else:
                        refusal_text = innate_match.formatted_message
                    return refuse_tool_call(refusal_text, hook_event="PreToolUse")
                emit_additional_context(
                    innate_match.formatted_message, hook_event="PreToolUse",
                )
    except ImportError as e:
        emit_stderr(f"innate_rules import failed: {type(e).__name__}")
        _invalidate_confirmation()
        # C01: the record lands FIRST, so both the allowed fall-through and
        # the refusal below leave the same durable trace.
        _record_ruleset_degradation(state_dir, "import_failure", e)
        _degraded = _innate_evaluator_failed(command, e)
        if _degraded is not None:
            return _degraded
    except Exception as e:
        emit_stderr(f"innate_rules eval failed: {type(e).__name__}")
        _invalidate_confirmation()
        # NOT a bare fall-through. See _innate_evaluator_failed: a raise here
        # used to disarm all twelve rules silently, and the raise is reachable
        # from one crafted rules file.
        _record_ruleset_degradation(state_dir, "evaluator_exception", e)
        _degraded = _innate_evaluator_failed(command, e)
        if _degraded is not None:
            return _degraded

    # Generic value-add telemetry runs only after the innate risk gates.
    # Matched destructive/protected/permission actions
    # have already returned and therefore never enter a generic event. Allowed
    # calls retain only presence, classification and one-way digests.
    if _entitled and state_dir is not None and tool_name:
        details = {
            "tool_name": tool_name,
            "has_command": bool(command),
            "has_file_path": bool(file_path),
            "operation": _operation,
            "risk_class": (
                _operation_risk.risk_class.value
                if _operation_risk is not None else None
            ),
        }
        if file_path:
            details["file_path_digest"] = hashlib.sha256(
                str(file_path).encode("utf-8")
            ).hexdigest()
        if command:
            details["command_digest"] = hashlib.sha256(
                str(command).encode("utf-8")
            ).hexdigest()
        try:
            append_observation(state_dir, "tool_call", details=details)
        except Exception as e:  # noqa: BLE001
            emit_stderr(f"tool-call telemetry failed: {e.__class__.__name__}")

    # These diagnostics persist excerpts. Even a confirmed destructive request
    # (or a losing parallel retry) must never supply its raw material to them.
    # Identity-only tool telemetry above remains available.
    if (_entitled and state_dir is not None and tool_name and command
            and not _destructive_candidate and _confirmed_action is None):
        try:
            import rollout_detector  # noqa: E402
            rollout_detector.detect_and_emit(state_dir, tool_name, command)
        except Exception as e:
            emit_stderr(f"rollout_detector failed: {e.__class__.__name__}")
        try:
            import topology_change_detector  # noqa: E402
            topology_change_detector.detect_and_emit(state_dir, tool_name, command)
        except Exception as e:
            emit_stderr(f"topology_change_detector failed: {e.__class__.__name__}")

    # Memory Slice 1 — a managed record block has one write path: the local
    # allostat_memory_upsert_v1 tool. Known-target content tools cannot mutate
    # the serialized block directly. Opaque shell/interpreter child-process
    # writes remain an explicit unsupported bypass.
    _managed_file_operation = _normalize_file_tool_name(tool_name)
    if file_path and _managed_file_operation in {"write", "edit", "multiedit"}:
        try:
            from topical_memory_records import direct_file_tool_targets_managed_block  # noqa: E402

            _managed_input = payload.get("tool_input") or payload.get("toolInput") or {}
            if not isinstance(_managed_input, dict):
                _managed_input = {}
            if direct_file_tool_targets_managed_block(
                file_path, _managed_input, operation=_managed_file_operation,
            ):
                return refuse_tool_call(
                    "MANAGED MEMORY WRITE PAUSED\n\nUse allostat_memory_upsert_v1 for typed, merged topical-memory updates.",
                    hook_event="PreToolUse",
                )
        except ImportError as e:
            emit_stderr(f"managed-memory guard import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"managed-memory guard failed: {e.__class__.__name__}")

    # v0.5.0 Phase 1 Slice 10 — sandbox enforcement (BEFORE Slice 1 cap-check
    # and before existing dispatch_pre_tool_use call). Deny early; emit
    # red-box; log denial event. H-06 (Wave-2 2026-07-11): "multiedit" added —
    # MultiEdit mutates its file_path exactly like Edit and walked past this
    # gate purely because its tool name wasn't in the tuple.
    if tool_name and file_path and tool_name.lower() in ("write", "edit", "notebookedit", "multiedit"):
        try:
            from sandbox_perms import resolve_session_role, evaluate_write_permission  # noqa: E402
            role = resolve_session_role(str(project_root) if project_root else None)
            decision = evaluate_write_permission(file_path, role)
            if not decision.allowed:
                _observe("sandbox_write_denied", {
                    "file_path": file_path,
                    "role": role.role,
                    "role_source": role.source,
                    "reason": decision.reason,
                })
                # H-05: this block means the innate-gate claim (if any) was
                # never spent — restore it for the operator's retry.
                # Harness-aware denial (Claude: red box + exit 2; Codex:
                # JSON permissionDecision deny).
                return refuse_tool_call(
                    f"⛔ {decision.denial_message}",
                    hook_event="PreToolUse",
                )
        except ImportError as e:
            emit_stderr(f"sandbox_perms import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"sandbox enforcement check failed: {e.__class__.__name__}")

    # FINDING 9 (audit 2026-07-30) — INNATE path rules for SHELL writes.
    #
    # The top innate gate (~:2460) keys on file_path, which a shell command does
    # not have, and classifies Bash as operation=None. The comment above
    # `_op_map` states the assumption that made this look deliberate:
    #
    #     Bash leaves operation as None -- Bash rules trigger on
    #     command_pattern, not operation.
    #
    # True for COMMAND rules (rule 02), false for PATH rules, and that gap is
    # the whole defect. Measured before the fix: an Edit to a canonical-ambiguous
    # target was hard-refused while `echo x > <same target>` was ALLOWED, and the
    # same held for an existing `.env` — so innate-01, the secrets guard, did not
    # see shell writes either. Not just innate-04: NO path rule did.
    #
    # This is the same gate the Codex patch path already has (~:2678, H-11), for
    # the same reason its comment gives: "the explicit-file_path match above
    # never saw these". Shell writes are the third surface that reaches the
    # filesystem without a file_path.
    #
    # operation="edit" deliberately, matching the patch gate: it is the single
    # value that satisfies rule 01/03's `[write, edit]` gate AND rule 04's
    # match_all `operation: edit`. command=None so rule 02 is not re-evaluated —
    # the top gate already saw the real command; no double-fire.
    #
    # UNRESOLVED TARGETS ARE NOT CLOSED HERE (audit finding 10). If any target
    # needs runtime expansion, `_bash_write_targets` raises and this gate sees
    # NOTHING — including the targets it could have resolved. Refusing outright
    # would hard-deny ordinary shell work (`cp src/*.py backup/`,
    # `mv "$SRC" "$DST"`), which is how an operator ends up leaving a blanket
    # override on permanently — the exact failure that makes a guard protect
    # nothing. So it is logged, not refused, and the residual gap is measurable
    # rather than invisible. Choosing the fail-closed boundary is a product
    # decision, deliberately not made unilaterally here.
    if tool_name and tool_name.lower() in ("bash", "shell") and command:
        try:
            from innate_rules import match_pre_tool_call, is_refusal_severity  # noqa: E402
            try:
                _bash_innate_targets = _bash_write_targets(command)
            except _UnresolvedBashWriteTarget:
                _bash_innate_targets = []
                if state_dir is not None:
                    append_observation(
                        state_dir,
                        "innate_unresolved_bash_write_allowed",
                        details={
                            "reason": "target_needs_runtime_expansion",
                            "command_digest": hashlib.sha256(
                                str(command or "").encode("utf-8")
                            ).hexdigest(),
                        },
                    )
            for _bt in _bash_innate_targets:
                _bt_flags: list[str] = []
                _bt_siblings: list[str] = []
                try:
                    from filesystem_context import detect_canonical_context  # noqa: E402
                    _bfs = detect_canonical_context(_bt)
                    if _bfs.flags:
                        _bt_flags = sorted(_bfs.flags)
                        _bt_siblings = list(_bfs.sibling_folder_names)
                except Exception as e:
                    emit_stderr(
                        "bash-target canonical context failed: "
                        f"{e.__class__.__name__}"
                    )
                _bm = match_pre_tool_call(
                    tool_name=tool_name,
                    command=None,
                    file_path=_bt,
                    operation="edit",
                    context_flags=set(_bt_flags),
                    sibling_list=_bt_siblings,
                )
                if (_bm is not None and is_refusal_severity(_bm.severity)):
                    _observe("innate_rule_fired", {
                        "rule_id": _bm.rule_id, "rule_name": _bm.rule_name,
                        "severity": _bm.severity, "response_action": _bm.response_action,
                        "tool_name": tool_name,
                        "target_digest": hashlib.sha256(str(_bt).encode("utf-8")).hexdigest(),
                        "refused": True, "via": "bash_write_target",
                    })
                    emit_stderr(
                        f"Allostat innate rule {_bm.rule_id} refused (bash write): "
                        f"{_bm.rule_name}"
                    )
                    return refuse_tool_call(
                        "PROTECTED WRITE PAUSED\n\nNo trusted local confirmation "
                        "boundary is available. Use a non-writing workflow.",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"innate bash-target check import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"innate bash-target check failed: {e.__class__.__name__}")

    # C2 (Wave 3): the gate above keys on file_path, so a Bash command writing
    # into a sandbox-scoped tree via shell redirection (>, >>, tee, dd of=) —
    # tool_name "bash", no file_path — walked past it. Extract the write targets
    # and run each through the SAME sandbox deny gate. (MEMORY.md-cap-via-Bash
    # and non-tuple MCP write tools are a scoped Wave-3 follow-up.)
    #
    # ("bash", "shell") not == "bash" (2026-08-01, carried from the 2026-07-30
    # session's open findings): Codex's shell tool arrives as tool_name
    # "shell", so the equality form scoped THIS gate to Claude Code only and
    # Codex redirection writes bypassed sandbox scoping entirely. Same
    # single-surface class as finding 9, same fix shape as the innate gate at
    # the ("bash", "shell") check above.
    if tool_name and tool_name.lower() in ("bash", "shell") and command:
        try:
            bash_targets = _bash_write_targets(command)
            from sandbox_perms import resolve_session_role, evaluate_write_permission  # noqa: E402
            if bash_targets:
                role = resolve_session_role(str(project_root) if project_root else None)
                for tgt in bash_targets:
                    decision = evaluate_write_permission(tgt, role)
                    if not decision.allowed:
                        _observe("sandbox_write_denied", {
                            "file_path": tgt,
                            "role": role.role,
                            "role_source": role.source,
                            "reason": decision.reason,
                            "via": "bash_redirect",
                        })
                        return refuse_tool_call(
                            f"⛔ {decision.denial_message}",
                            hook_event="PreToolUse",
                        )
        except _UnresolvedBashWriteTarget:
            # This is a safety decision, not a parser crash. Keep it outside the
            # broad diagnostic catch below so runtime-expanded write targets
            # cannot degrade to allow.
            #
            # ...but only where a policy exists that the unresolved path could
            # violate. With no sandbox config on disk the gate this protects is
            # a documented no-op, and refusing here hard-denied ordinary Bash
            # (`cp src/*.py backup/`, `mv "$SRC" "$DST"`, `npm test > ~/x.log`)
            # for every such customer. See `_bash_sandbox_policy_active`, which
            # fails CLOSED on a corrupt config and on its own errors.
            if _bash_sandbox_policy_active():
                return refuse_tool_call(
                    _UNRESOLVED_BASH_WRITE_MESSAGE,
                    hook_event="PreToolUse",
                )
            if state_dir is not None:
                append_observation(
                    state_dir,
                    "sandbox_unresolved_bash_write_allowed",
                    details={"reason": "no_sandbox_policy_configured"},
                )
        except ImportError as e:
            emit_stderr(f"sandbox bash-target check import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"sandbox bash-target check failed: {e.__class__.__name__}")

    # H-11 (2026-07-14; all-gates extension 2026-07-15): Codex's apply_patch
    # write path. The gates above key on file_path (Write/Edit) or bash
    # redirect/cp/mv/sed tokens; a patch carries its targets INSIDE the patch
    # text, so NONE of the three local safety gates (innate ~600, sandbox ~696,
    # MEMORY cap ~790) saw the main Codex write path. Route every
    # create/update/remove/move target through ALL THREE — innate path rules,
    # the sandbox deny gate, AND the MEMORY.md cap — not the sandbox gate alone.
    # Extraction is SCOPED to a real apply_patch/exec op, so a Write/Edit whose
    # content merely embeds a patch example is not scanned (over-deny fix). This
    # is a SAFETY gate — it runs regardless of entitlement (item #5), before the
    # value-add dispatch below.
    _patch_sections = _extract_patch_sections(payload, tool_name)
    if _patch_sections:
        _patch_targets: list[str] = []
        _seen_targets: set[str] = set()
        for _sec in _patch_sections:
            for _t in _sec["targets"]:
                if _t and _t not in _seen_targets:
                    _seen_targets.add(_t)
                    _patch_targets.append(_t)

        # Managed-memory gate for apply_patch targets, whose patch body is
        # visible. This does not claim coverage of opaque executor writes.
        try:
            from topical_memory_records import direct_file_tool_targets_managed_block  # noqa: E402

            _patch_input = payload.get("tool_input") or payload.get("toolInput") or {}
            if not isinstance(_patch_input, dict):
                _patch_input = {}
            for _managed_target in _patch_targets:
                if direct_file_tool_targets_managed_block(_managed_target, _patch_input, operation="patch"):
                    return refuse_tool_call(
                        "MANAGED MEMORY WRITE PAUSED\n\nUse allostat_memory_upsert_v1 for typed, merged topical-memory updates.",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"managed-memory patch guard import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"managed-memory patch guard failed: {e.__class__.__name__}")

        # Gate 1 — INNATE path rules. The explicit-file_path match above never
        # saw these (a patch has no file_path). Each target gets the same rule-04
        # filesystem-context probe and runs at operation="edit" — the single op
        # that satisfies rule 01/03's `[write, edit]` gate AND rule 04's
        # match_all `operation: edit` — so a patch create/update/move into a
        # secrets / _LEGACY_ / canonical-ambiguous path is refused. command is
        # None here, so command_pattern rules (rule 02) are NOT re-evaluated —
        # the top gate already saw the real command; no double-fire.
        try:
            from innate_rules import match_pre_tool_call, is_refusal_severity  # noqa: E402
            for tgt in _patch_targets:
                _pt_ctx_flags: list[str] = []
                _pt_siblings: list[str] = []
                try:
                    from filesystem_context import detect_canonical_context  # noqa: E402
                    _fs = detect_canonical_context(tgt)
                    if _fs.flags:
                        _pt_ctx_flags = sorted(_fs.flags)
                        _pt_siblings = list(_fs.sibling_folder_names)
                except Exception as e:
                    emit_stderr(f"patch-target canonical context failed: {e.__class__.__name__}")
                _pm = match_pre_tool_call(
                    tool_name=tool_name,
                    command=None,
                    file_path=tgt,
                    operation="edit",
                    context_flags=set(_pt_ctx_flags),
                    sibling_list=_pt_siblings,
                )
                if (_pm is not None and is_refusal_severity(_pm.severity)):
                    _observe("innate_rule_fired", {
                        "rule_id": _pm.rule_id, "rule_name": _pm.rule_name,
                        "severity": _pm.severity, "response_action": _pm.response_action,
                        "tool_name": tool_name,
                        "target_digest": hashlib.sha256(str(tgt).encode("utf-8")).hexdigest(),
                        "refused": True, "via": "codex_patch",
                    })
                    emit_stderr(
                        f"Allostat innate rule {_pm.rule_id} refused (codex patch): "
                        f"{_pm.rule_name}"
                    )
                    return refuse_tool_call(
                        "PROTECTED WRITE PAUSED\n\nNo trusted local confirmation "
                        "boundary is available. Use a non-writing workflow.",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"innate patch-target check import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"innate patch-target check failed: {e.__class__.__name__}")

        # Gate 2 — sandbox deny gate (original H-11 behavior).
        try:
            from sandbox_perms import resolve_session_role, evaluate_write_permission  # noqa: E402
            role = resolve_session_role(str(project_root) if project_root else None)
            for tgt in _patch_targets:
                decision = evaluate_write_permission(tgt, role)
                if not decision.allowed:
                    _observe("sandbox_write_denied", {
                        "file_path": tgt,
                        "role": role.role,
                        "role_source": role.source,
                        "reason": decision.reason,
                        "via": "codex_patch",
                    })
                    return refuse_tool_call(
                        f"⛔ {decision.denial_message}",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"sandbox patch-target check import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"sandbox patch-target check failed: {e.__class__.__name__}")

        # Gate 3 — MEMORY.md per-entry cap. The explicit cap gate keys on a
        # Write/Edit file_path + validates content/new_string; a patch carries
        # neither. Run each section whose target is a MEMORY.md path through the
        # cap using that hunk's ADDED lines (leading `+` already stripped).
        try:
            from memory_md_cap import (  # noqa: E402
                is_memory_md_path, find_cap_violations, format_violation_message,
            )
            for _sec in _patch_sections:
                _mem_target = next(
                    (t for t in _sec["targets"] if is_memory_md_path(t)), None
                )
                if _mem_target is None:
                    continue
                _added_text = "\n".join(_sec["added_lines"])
                if not _added_text:
                    continue
                _violations = find_cap_violations(_added_text)
                if _violations:
                    if state_dir is not None:
                        append_observation(state_dir, "memory_md_cap_rejected", details={
                            "file_path": _mem_target,
                            "violation_count": len(_violations),
                            "longest_chars": max(v.char_count for v in _violations),
                            "via": "codex_patch",
                        })
                    return refuse_tool_call(
                        f"⚠ {format_violation_message(_violations)}",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"memory_md_cap patch-target check import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"memory_md_cap patch-target check failed: {e.__class__.__name__}")

    # v0.5.0 Phase 1 Slice 1 — MEMORY.md cap enforcement. H-06 (Wave-2
    # 2026-07-11): "multiedit" added — MultiEdit writes MEMORY.md content
    # exactly like Edit and bypassed the cap purely on tool name.
    if (tool_name and file_path and tool_name.lower() in ("write", "edit", "multiedit")):
        try:
            from memory_md_cap import is_memory_md_path, find_cap_violations, format_violation_message  # noqa: E402
            if is_memory_md_path(file_path):
                # For Write: get the new_string / content from payload
                # For Edit: validate the new_string against the cap
                # For MultiEdit: validate EVERY edit's new_string (the payload
                # is an ARRAY of {old_string, new_string}; any one violating
                # edit refuses the whole call). Shape-defensive: junk entries
                # are skipped, never crash the hook.
                tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
                if not isinstance(tool_input, dict):
                    tool_input = {}  # malformed payload — don't crash the cap check
                if tool_name.lower() == "multiedit":
                    edits = tool_input.get("edits")
                    target_texts = [
                        e.get("new_string")
                        for e in (edits if isinstance(edits, list) else [])
                        if isinstance(e, dict) and isinstance(e.get("new_string"), str)
                    ]
                else:
                    target_texts = [tool_input.get("content") or tool_input.get("new_string") or ""]
                violations = []
                for target_text in target_texts:
                    if target_text:
                        violations.extend(find_cap_violations(target_text))
                if violations:
                    if state_dir is not None:
                        append_observation(state_dir, "memory_md_cap_rejected", details={
                            "file_path": file_path,
                            "violation_count": len(violations),
                            "longest_chars": max(v.char_count for v in violations),
                        })
                    # H-05: unspent innate-gate claim → restore for retry.
                    # Harness-aware denial (Claude: exit 2; Codex: JSON
                    # permissionDecision deny).
                    return refuse_tool_call(
                        f"⚠ {format_violation_message(violations)}",
                        hook_event="PreToolUse",
                    )
        except ImportError as e:
            emit_stderr(f"memory_md_cap import failed: {e.__class__.__name__}")
        except Exception as e:
            emit_stderr(f"memory_md_cap check failed: {e.__class__.__name__}")

    # Confirmed destructive requests must not enter advisory dispatch/log sinks.
    # All local independent gates above still run before the deferred spend.
    if _confirmed_action is not None:
        return 0

    # Server-side regulation + context injection is value-add — gate it on live
    # entitlement (item #5). Safety gates above already ran unconditionally.
    if not _entitled or not has_mcp_token() or not tool_name:
        return 0

    try:
        from client_state_writer import apply_writes  # noqa: E402
        from mcp_client import MCPClient, MCPClientError  # noqa: E402
    except ImportError as e:
        emit_stderr(f"import failed: {e.__class__.__name__}")
        return 0

    # Rule 04 (canonical-verification) filesystem context: computed ONCE,
    # above the local innate match (C-06), and reused here for the dispatch.

    # Compose observation excerpt for the server.
    observations: list[dict] = []
    if state_dir is not None:
        try:
            from local_state import (  # noqa: E402
                read_recent_observations,
                scrub_observations_for_wire,
            )
            # Wave-2 H-02 (2026-07-11): scrub raw voice snippets (pre-fix
            # legacy records) at the wire boundary — no response prose in
            # excerpt.observations.
            observations = scrub_observations_for_wire(
                read_recent_observations(state_dir, window=50)
            )
        except Exception:
            observations = []

    # Existing-customer privacy-disclosure migration (rem-consent, 2026-07-22;
    # fail-closed round 6, 2026-07-23). Withhold the ENTIRE server round-trip
    # while the corrected disclosure is pending: the command/file_path ride
    # both the event fields AND the observation tail (tool_call details), so
    # blanking the event alone would still leak via the tail. The local safety
    # guards + telemetry above already ran; only the network dispatch is
    # skipped. session-start surfaces the corrected notice (presentation);
    # user-prompt-submit records acknowledgment on the next prompt, then
    # transmission resumes.
    try:
        import consent  # noqa: E402
        _disclosure_ok = consent.disclosure_ok_to_transmit()
    except Exception as e:
        # Round 6: a privacy control fails CLOSED toward transmission — this
        # skips the dispatch (fail-closed) instead of transmitting. The hook
        # stays non-blocking (withheld path returns 0).
        emit_stderr(f"disclosure gate check failed: {e.__class__.__name__}")
        _disclosure_ok = False
    if not _disclosure_ok:
        if state_dir is not None:
            try:
                append_observation(
                    state_dir, "disclosure_migration_withheld",
                    details={"hook": "pre_tool_use"},
                )
            except Exception:
                pass
        return 0

    try:
        client = MCPClient(harness=detect_harness())
        response = client.call_tool(
            "dispatch_pre_tool_use",
            arguments={
                "excerpt": {"observations": observations},
                "event": {
                    "tool_name": tool_name,
                    "command": redact_secrets(command),
                    "file_path": file_path,
                    "context_flags": context_flags,
                    "sibling_list": sibling_list,
                    # Advisory parity only. The server may classify but can
                    # never upgrade this caller-supplied evidence to trusted
                    # internal authority.
                    "operation": _operation,
                    "operation_risk": (
                        _operation_risk.risk_class.value
                        if _operation_risk is not None else None
                    ),
                },
            },
        )
    except MCPClientError as e:
        emit_stderr(f"dispatch_pre_tool_use failed: {e.__class__.__name__}")
        if state_dir is not None:
            append_observation(
                state_dir,
                "mcp_error",
                details={
                    "hook": "pre_tool_use",
                    "error_class": type(e).__name__,
                    "status_code": getattr(e, "status_code", None),
                },
            )
        return 0
    except Exception as e:
        emit_stderr(f"dispatch_pre_tool_use unexpected error: {e.__class__.__name__}")
        return 0

    if not isinstance(response, dict):
        return 0

    writes = response.get("client_state_writes") or []
    if writes and state_dir is not None:
        try:
            apply_writes(state_dir, writes)
        except Exception as e:
            emit_stderr(f"apply_writes failed: {e.__class__.__name__}")

    text = response.get("additional_context") or ""
    if text:
        try:
            emit_additional_context(text, hook_event="PreToolUse")
        except Exception as e:
            emit_stderr(f"emit_additional_context failed: {e.__class__.__name__}")

    return 0


if __name__ == "__main__":
    # Deterministic single-object flush for Codex (same pattern as the other
    # hooks). No-op under Claude; no-op after a codex deny (refuse_tool_call
    # clears the buffer so the deny stays the only stdout object).
    try:
        _rc = main()
    finally:
        try:
            flush_pending_context("PreToolUse")
        except Exception:  # best-effort — a hook must never crash the turn
            pass
    sys.exit(_rc)
