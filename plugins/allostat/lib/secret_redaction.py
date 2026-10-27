"""Secret redaction for audit/observation sinks — review fix 2026-07-04 (MED).

The pre-tool-use hook logs every Bash command to the plaintext
.allostat/observations.jsonl (persists for days at the 5MB/1000-entry
rotation) and transmits it to the MCP server. Raw commands routinely carry
credentials — the review's concrete case was the RDS DSN, password
included. This module masks known secret shapes while leaving the command
structurally readable (user/host/tool survive; only the secret value is
replaced), so observations stay useful for debrief counting and pattern
observation.

Scope: known high-confidence secret patterns only. Deliberately NOT a
generic entropy detector — false positives would corrupt the observation
record that pattern-observer learns from.

The redactor is idempotent and safe on arbitrary text; callers apply it at
every sink that persists or transmits a command string
(hooks/pre-tool-use.py — pinned by tests/test_secret_redaction.py).
"""
from __future__ import annotations

import re
from typing import Any

_MASK = "<REDACTED>"
_SECRET_NAME = (
    r"[A-Z0-9_]*(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API[_-]?KEY|"
    r"APIKEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIALS?|AUTH[_-]?TOKEN|"
    r"CLIENT[_-]?SECRET)[A-Z0-9_]*"
)

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Credential-bearing URIs — mask only the password/token segment while
    # preserving the scheme, user, host, port, and path.
    (
        re.compile(
            r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|"
            r"amqp(?:s)?|https?|ftp|smtp(?:s)?|ssh)://[^:/\s@]*:)"
            r"[^@\s]+(@)",
            re.IGNORECASE,
        ),
        rf"\g<1>{_MASK}\g<2>",
    ),
    # Secret-bearing assignments and CLI values. `=` / `:` assignments are
    # unconditional; whitespace-separated values require an export/set prefix
    # or a CLI flag. This avoids treating ordinary prose such as "token refresh"
    # as an assignment. The value must be a real token, not a following flag.
    (
        re.compile(
            rf"(?P<prefix>(?<![A-Z0-9_-])(?:"
            rf"{_SECRET_NAME}\s*[:=]\s*|"
            rf"(?:(?:export|set)\s+{_SECRET_NAME}|--?{_SECRET_NAME})\s+"
            rf"))"
            r"(?P<quote>[\"']?)(?!-)(?P<value>[^\s\"']+)(?P=quote)",
            re.IGNORECASE,
        ),
        rf"\g<prefix>\g<quote>{_MASK}\g<quote>",
    ),
    # PGPASSWORD=value (env-prefix idiom)
    (re.compile(r"(PGPASSWORD=)\S+"), rf"\g<1>{_MASK}"),
    # sshpass -p value
    (re.compile(r"(sshpass\s+-p\s+)\S+"), rf"\g<1>{_MASK}"),
    # Authorization: Bearer token (header idiom, quoted or not)
    (
        re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE),
        rf"\g<1>{_MASK}",
    ),
    # AWS secret access key assignments (cli/env/ini idioms)
    (
        re.compile(
            r"(aws_secret_access_key(?:\s*=\s*|\s+))\S+", re.IGNORECASE
        ),
        rf"\g<1>{_MASK}",
    ),
    # AWS access key ids are identifiers, not secrets per se — but they
    # pair-identify the account and the operator has pasted key material
    # in command contexts before. Mask the id shape outright.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED-AWS-KEY-ID>"),
    # Bare provider tokens, anchored to high-confidence prefixes and length
    # floors. Keep provider-specific forms before the generic OpenAI `sk-`.
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"), _MASK),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), _MASK),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), _MASK),
    # GitHub tokens (personal-access, OAuth, server/user-to-server) as bare
    # values — ghp_/gho_/ghs_/ghu_/ghr_ + fine-grained github_pat_.
    (re.compile(r"\bgh[posur]_[A-Za-z0-9]{36,}\b"), _MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), _MASK),
)


def redact_secrets(text: str) -> str:
    """Return `text` with known secret shapes masked. Idempotent; returns
    the input unchanged when nothing matches."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_payload(obj: Any) -> Any:
    """Return a recursively redacted copy of a JSON-serializable payload.

    String values are redacted; mapping keys and non-string scalar values are
    left intact. The input object is never mutated.
    """
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, dict):
        return {key: redact_payload(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [redact_payload(value) for value in obj]
    return obj
