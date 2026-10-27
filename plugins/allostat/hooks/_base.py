"""Shared hook scaffolding for the allostat-mcp wrapper plugin.

Common patterns:
  - parse stdin JSON payload
  - resolve the project root + state dir
  - opt-out via ALLOSTAT_DISABLED=1 (bench harness flag)
  - opt-out via missing harness-scoped MCP authority (silent degrade if no auth)
  - format additionalContext output for UserPromptSubmit etc.
  - log to stderr on errors (visible in operator's session if hook
    troubleshooting is needed) but never raise — hooks must not break
    the operator's session
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# v0.2.0 audit-hardening (W3/W4/W38/W47): force stdout + stderr to UTF-8
# regardless of OS default encoding (Windows defaults to cp1252 which
# corrupts the box-drawing characters in handoff-checkpoint red boxes
# and the ✅/⚠/❌ markers in the install-state banner). reconfigure()
# only exists on Python 3.7+ TextIOWrapper; guard for older runtimes
# even though we require 3.12+.
#
# H-24 (2026-07-11): stdin MUST be reconfigured too. It defaults to the OS
# locale codepage (cp1252 on Windows), so a piped hook JSON payload carrying
# a non-cp1252 byte (non-English username/path, certain smart punctuation)
# fails to decode inside read_payload() — the bare except there swallows it
# to {}, discarding cwd/session_id/transcript_path for EVERY hook fire.
# Test stdins (StringIO) have no .reconfigure; the AttributeError guard skips
# them harmlessly.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# Add the wrapper's lib/ to sys.path so hooks can import local_state,
# mcp_client, client_state_writer, etc.
_HOOK_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _HOOK_DIR.parent
_LIB_DIR = _PLUGIN_ROOT / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def disabled() -> bool:
    """ALLOSTAT_DISABLED=1 turns the regulator off (bench harness opt-out)."""
    return os.environ.get("ALLOSTAT_DISABLED") == "1"


def has_mcp_token() -> bool:
    """Without a Bearer token the wrapper cannot reach the MCP server.

    On missing token, hooks degrade silently (return 0, no additionalContext)
    so an unconfigured plugin install doesn't break the operator's session.

    Audit #6 (2026-07-06): this preflight MUST mirror the same token sources
    MCPClientConfig.from_env() consults — scoped OS env and installed stores.
    Before this fix the gate checked only os.environ, so a bearer installed
    (or rotated) into the plugin .env — which the client would happily use —
    still failed the gate closed, silently suppressing every hook's dispatch
    and all server-side regulation. Use the client resolver directly so this
    preflight cannot choose a different harness/account authority.
    """
    try:
        from mcp_client import MCPClientConfig  # noqa: E402
        config = MCPClientConfig.from_env(harness=detect_harness())
        if config.runtime == "local":
            from local_runtime import require_ready
            require_ready()
            return True
        return bool(config.bearer_token)
    except Exception:  # noqa: BLE001 — never let a preflight import break the hook
        return False


def should_inject(state_dir: Path | None) -> bool:
    """S2 Block 3a (2026-05-27) — deactivation lifecycle guard.

    Returns True iff the wrapper should inject rules + run regulation this
    hook fire. Reads the subscription_state cache populated at SessionStart
    by `subscription_gate.refresh_subscription_state`.

    Fail-CLOSED semantics (2026-07-20 — see the note below):
      - state_dir is None → **False**. Nothing to read entitlement from.
      - Cache absent / malformed / unreadable → False (F-04, 2026-07-10: per
        `subscription_gate.is_active_subscription`, fail-CLOSED — a
        missing/corrupt cache no longer grants entitlement; SessionStart writes
        a real state before any hook reads it).
      - Cached state in {'active', 'trial'} → True.
      - Cached state in {'lapsed', 'canceled'} → False.
      - **Any exception → False.** Import failure, read failure, anything.

    The guard is read-only — populating the cache is SessionStart's job.
    Downstream hooks (UserPromptSubmit, PreToolUse, PostToolUse, Stop)
    consult this and early-noop when False.

    Operator's locked directive 2026-05-27 PM: silent removal on lapse.
    "A message would be intrusive — Allostat's presence on their machine
    after they manually went into their account and unsubscribed."

    WHY BOTH BRANCHES FLIPPED (2026-07-20). This function is the gate every
    downstream hook consults, and it used to answer True on `state_dir is None`
    and True on ANY exception. So the F-04 hardening below it — which made
    `is_active_subscription` fail closed on a missing or corrupt cache — was
    only ever consulted when the import and the read both SUCCEEDED. A broken
    plugin update, a missing module, a syntax error in `subscription_gate.py`:
    all of them landed in the `except` and resolved to *entitled*.

    That is why the same directive was issued five times between 2026-05-27 and
    2026-07-20 without the behaviour changing. Each round hardened the predicate
    under inspection while this caller kept answering True. The operator's
    words: "There is no world where allowing any part of allostat to run
    without the regulator is safe."

    `state_dir is None` was justified as "don't degrade sessions where state
    can't be resolved". But `resolve_state_dir()` falls back to `Path.cwd()`
    and never returns None, so the branch is defensive-only — and if it ever
    does fire, there is by definition no cache to read, which is precisely the
    case that must deny.

    The cost of this direction is real and accepted: if entitlement cannot be
    read, regulation stops. That is the *pro-customer* outcome. A customer whose
    Allostat goes inert still has a fully working Claude Code and recovers on
    the next SessionStart. A customer whose Allostat runs unregulated gets
    unmetered memory injection — the 75k-tokens-on-a-3-word-prompt failure.
    """
    if state_dir is None:
        return False
    try:
        import subscription_gate  # noqa: E402
        active = subscription_gate.is_active_subscription(state_dir)
        if not active:
            _record_injection_suppressed(state_dir)
        return active
    except Exception as e:  # noqa: BLE001
        # Fail CLOSED. "I could not determine entitlement" must never resolve
        # to "entitled" — that equivalence is the whole defect.
        try:
            emit_stderr(f"should_inject lookup failed (regulation OFF): {e}")
        except Exception:  # noqa: BLE001  # H3 audit fix: stderr-print itself raised
            pass
        return False


def _record_injection_suppressed(state_dir: Path) -> None:
    """Log that the gate turned regulation off, and on what evidence.

    Silence toward the OPERATOR is the locked directive and stays. Silence in
    the RECORD is a different thing and was a defect: when a rate limit pushed
    331 of 676 benchmark sessions into a silent gate, nothing was written at
    all — not even `session_start` — so the failure presented as random
    injection loss with no way to reproduce it. A support ticket cannot be
    answered from an empty log.

    Best-effort and never raises: a gate that fails to log still gates.
    """
    try:
        import subscription_gate  # noqa: E402
        from local_state import append_observation  # noqa: E402

        cache = subscription_gate.read_cached_state(state_dir) or {}
        append_observation(state_dir, "injection_suppressed", details={
            "subscription_state": cache.get("subscription_state"),
            "source": cache.get("source"),
            "error": cache.get("error"),
            "validated_at": cache.get("validated_at"),
        })
    except Exception:  # noqa: BLE001
        pass


def is_terminally_inactive(state_dir: Path | None) -> bool:
    """Decouple SAFETY from entitlement (item #5, 2026-07-14).

    Returns True ONLY when entitlement is a server-CONFIRMED terminal-inactive
    state (`lapsed`/`canceled`) — the sole case where the deactivation guard
    fully silences the wrapper INCLUDING its local safety rules (the operator's
    locked silent-removal-on-lapse: after they unsubscribed, Allostat's presence
    on their machine — safety refusals included — goes quiet).

    An INDETERMINATE state returns False so the SAFETY floor still runs:
      - `"unknown"` — the fail-closed sentinel a 300s control-plane outage
        produces for a still-PAYING user (this is the blocker being closed:
        an outage must NOT strip the 12 destructive-command/sandbox guards).
      - missing / malformed cache — can't confirm a terminal state → protect.

    state_dir None → False (can't confirm terminal state; run safety).
    Any lookup error → False (fail-safe: keep safety on).

    Distinct from `should_inject` (active/trial → value-add regulation). Safety
    runs whenever this is False; value-add runs only when should_inject is True.
    """
    if state_dir is None:
        return False
    try:
        import subscription_gate  # noqa: E402
        return subscription_gate.is_terminally_inactive(state_dir)
    except Exception as e:  # noqa: BLE001
        try:
            emit_stderr(
                f"is_terminally_inactive lookup failed (fail-safe: safety on): {e}"
            )
        except Exception:  # noqa: BLE001
            pass
        return False


def read_payload() -> dict[str, Any]:
    """Read the hook's stdin JSON payload. Returns {} on parse failure."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"prompt": raw}  # UserPromptSubmit fallback
    return payload if isinstance(payload, dict) else {}


# F01 (2026-08-03) — cap on directory entries examined while enumerating
# decode candidates. Real paths finish in a few hundred; the cap bounds a
# pathological tree. Exhausting it means uniqueness could not be PROVEN,
# which is treated exactly like ambiguity: degrade, never guess.
_DECODE_PROBE_BUDGET = 50_000


def _decode_harness_dir_name(name: str) -> Path | None:
    """Decode a harness project-directory name back to the real path it
    encodes — accepted only when that path is the UNIQUE inverse
    (D1 2026-08-03; uniqueness added for F01, same date).

    The sanitizer (`session_handoff.sanitize_cwd_for_harness`) maps ':', '\\',
    '/', and ' ' all to '-', so the encoding is lossy AND many-to-one:
    `C--dev-a-b` is the image of `C:\\dev\\a\\b`, `C:\\dev\\a-b`, and
    `C:\\dev\\a b` alike.

    A round-trip (`sanitize(decoded) == name`) proves a candidate is *a*
    valid inverse. It does NOT prove it is the *only* one — that was F01:
    the walk returned its first longest match, round-tripped it, and handed
    back `decoded_harness_transcript` provenance for what was really a coin
    flip between sibling projects. A resuming session could then be given
    another project's memory and instructed to trust it.

    So: enumerate every existing candidate, and return one only when there
    is exactly one. Two or more (or an unprovable search, see
    `_DECODE_PROBE_BUDGET`) returns None, and the caller degrades to
    `harness_transcript_guess` — which refuses to inject a resume core as
    authoritative. A declared guess is recoverable; a confident wrong answer
    is not. Enumeration stops as soon as a second match appears: the caller
    only needs "exactly one or not", never the full list.

    Candidates come from READING THE DIRECTORY, not from re-joining tokens
    with '-'. That distinction is load-bearing. A token-joining walk can
    only ever synthesize names containing '-', so it is structurally blind
    to `alpha beta` — it would find `alpha-beta`, see a single match, and
    report a unique decode while a real collider sat next to it on disk.
    Listing each directory and sanitizing the names actually present finds
    every collider regardless of which character collapsed.
    """
    import re

    m = re.match(r"^([A-Za-z])--(.+)$", name)
    if m:
        root = Path(f"{m.group(1)}:\\")
        rest = m.group(2)
    elif name.startswith("-") and len(name) > 1:
        root = Path("/")
        rest = name[1:]
    else:
        return None  # bare names (`ExampleProject`) encode nothing recoverable
    # Case folding follows the FILESYSTEM, via os.path.normcase: a no-op on
    # POSIX (where `Alpha` and `alpha` are genuinely different directories and
    # folding would invent collisions), lowercase on Windows/macOS. A cwd
    # string does not always carry the on-disk casing — `c:\work\example`
    # typed at a prompt encodes with a lowercase drive while the directory is
    # `C:\work\example` — so an exact byte compare against real directory
    # names silently refuses paths that plainly exist (measured: this rejected
    # `C--Windows-system32`, whose directory is `System32`). Where casing
    # cannot distinguish two directories, folding cannot manufacture a
    # collision either.
    folded_name = os.path.normcase(name)
    tokens = [t for t in os.path.normcase(rest).split("-")]
    if not tokens or len(tokens) > 32:
        return None

    from session_handoff import sanitize_cwd_for_harness  # noqa: E402

    matches: list[Path] = []
    probes = 0
    exhausted = False

    def _walk(base: Path, idx: int) -> None:
        nonlocal probes, exhausted
        if len(matches) > 1 or exhausted:
            return
        if idx == len(tokens):
            # A complete consumption of the tokens. It counts only if
            # re-sanitizing reproduces the name (modulo filesystem casing).
            if os.path.normcase(sanitize_cwd_for_harness(base)) == folded_name:
                matches.append(base)
            return
        remaining = len(tokens) - idx
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if len(matches) > 1 or exhausted:
                        return
                    if probes >= _DECODE_PROBE_BUDGET:
                        exhausted = True
                        return
                    probes += 1
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        # Best-effort: an entry that cannot be stat'd (vanished
                        # mid-scan, permission denied) contributes no candidate.
                        # Skipping can only ever shrink the match set, so it can
                        # hide a collider and cause a DEGRADE — never a wrong
                        # confident answer, which is the direction that matters.
                        continue
                    # How many tokens would this real directory name have
                    # produced? Whatever collapsed inside it — a space, a
                    # literal dash — sanitizing reproduces the encoder's own
                    # output for this component.
                    child_tokens = os.path.normcase(
                        sanitize_cwd_for_harness(entry.name)
                    ).split("-")
                    if len(child_tokens) > remaining:
                        continue
                    if tokens[idx:idx + len(child_tokens)] != child_tokens:
                        continue
                    _walk(Path(entry.path), idx + len(child_tokens))
        except OSError:
            # Unreadable directory (permissions, vanished): it contributes
            # no candidates. Uniqueness among the readable ones still holds
            # for the tree we can see.
            return

    try:
        _walk(root, 0)
    except RecursionError:
        return None
    if exhausted:
        return None  # uniqueness unproven — same answer as ambiguous
    if len(matches) == 1:
        return matches[0]
    return None  # zero candidates, or a collision: never adopt a guess


def resolve_project_root_with_provenance(
    payload: dict[str, Any],
) -> tuple[Path | None, str]:
    """Resolve the project root AND say how it was resolved (D1, 2026-08-03).

    Provenance values:
      "cwd"                       — the payload said so; authoritative.
      "decoded_harness_transcript"— no cwd, but the transcript's harness
                                    directory name decoded to a real path and
                                    round-tripped exactly; equally authoritative.
      "harness_transcript_guess"  — no cwd, undecodable transcript dir; the
                                    harness directory itself stands in. Memory
                                    resolved from this is a PARALLEL tree, not
                                    the project's — callers must say so rather
                                    than present its contents as current state
                                    (the ExampleProject banner read a 5-week-old
                                    orphan as fact this way).
      "cwd_fallback"              — no payload hints at all; Path.cwd().

    The un-provenanced `resolve_project_root_from_payload` below delegates
    here, so the two can never diverge.
    """
    cwd_hint = payload.get("cwd") or payload.get("workspaceRoot")
    if cwd_hint:
        try:
            return Path(cwd_hint).resolve(), "cwd"
        except Exception:
            pass
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if transcript:
        try:
            tp = Path(transcript)
            if tp.parent.exists():
                decoded = _decode_harness_dir_name(tp.parent.name)
                if decoded is not None:
                    return decoded, "decoded_harness_transcript"
                # The transcript folder lives at .claude/projects/<encoded>/.
                # Fall back to that as a project_root proxy when no .allostat/.
                return tp.parent, "harness_transcript_guess"
        except Exception:
            pass
    return Path.cwd(), "cwd_fallback"


def resolve_project_root_from_payload(payload: dict[str, Any]) -> Path | None:
    """Use the harness-provided transcript path / cwd to pick a project
    root. Falls back to cwd. Provenance-free view of
    `resolve_project_root_with_provenance` — same resolution, always."""
    root, _provenance = resolve_project_root_with_provenance(payload)
    return root


_pending_codex_context: list[str] = []
_codex_flush_registered = False


def wrap_pending_context(prefix: str, suffix: str) -> None:
    """Bracket everything buffered for the single Codex flush.

    Codex wall-of-text fix (2026-07-05): all SessionStart payloads travel in
    ONE combined object under Codex, so session-start wraps them in an
    <allostat_internal_context> envelope before flushing (data, not display
    text). No-op when nothing is buffered (Claude never buffers).
    """
    if _pending_codex_context:
        _pending_codex_context.insert(0, prefix)
        _pending_codex_context.append(suffix)


_post_flush_callbacks: list[tuple[object, object | None]] = []


def run_after_physical_flush(callback, *, on_failure=None) -> None:
    """Register a callback to run ONLY after buffered Codex context has
    physically reached stdout. Round-7 inversion (advisor 2026-07-23): a
    successful return from emit_additional_context proves only that text
    entered a list; presentation state may be committed exclusively on proof
    of physical output. Callbacks run at most once, in registration order,
    and never at all if the flush fails. A transient flush failure retains the
    buffer and both callbacks for the atexit retry. An optional ``on_failure``
    callback runs only when the associated buffer is explicitly discarded."""
    _post_flush_callbacks.append((callback, on_failure))


def _discard_pending_context() -> None:
    """Abandon buffered output and release its two-phase reservations."""
    global _post_flush_callbacks
    _pending_codex_context.clear()
    callbacks, _post_flush_callbacks = _post_flush_callbacks, []
    for _callback, on_failure in callbacks:
        if on_failure is None:
            continue
        try:
            on_failure()
        except Exception as callback_error:
            emit_stderr(f"post-flush failure callback failed: {callback_error}")


def flush_pending_context(hook_event: str = "SessionStart") -> bool:
    """Codex accepts only ONE JSON object per hook. Emit all buffered
    additionalContext as a single combined object.

    Returns True only when the payload PHYSICALLY reached stdout (print +
    flush both succeeded) — or when nothing was buffered. Round-7: this
    result is the only failure signal the Codex path has; it is returned,
    not swallowed. On failure the buffer is KEPT so the atexit-registered
    retry still has the payload and its success/failure callbacks. A later
    successful retry therefore commits the same physical-delivery state."""
    global _post_flush_callbacks
    if not _pending_codex_context:
        if _post_flush_callbacks:
            _discard_pending_context()
        return True
    combined = "\n\n".join(_pending_codex_context)
    try:
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": hook_event, "additionalContext": combined}
        }))
        sys.stdout.flush()
    except Exception as e:
        emit_stderr(f"flush_pending_context failed — buffered context NOT delivered: {e}")
        return False
    _pending_codex_context.clear()
    callbacks, _post_flush_callbacks = _post_flush_callbacks, []
    for cb, _on_failure in callbacks:
        try:
            cb()
        except Exception as e:
            emit_stderr(f"post-flush callback failed: {e}")
    return True


def emit_additional_context(
    text: str,
    *,
    hook_event: str = "UserPromptSubmit",
) -> None:
    """Print the JSON envelope Claude Code expects for hook
    additionalContext output.

    Claude Code's hook protocol requires `hookEventName` to match the
    actual event slot the hook is registered for. Each hook script must
    pass its event name explicitly via `hook_event` — defaults to
    `UserPromptSubmit` for back-compat, but callers should always set it.

    Codex accepts only ONE JSON object on a hook's stdout — so under Codex
    we buffer emissions and flush a single combined object at process exit
    (via `flush_pending_context`, registered with `atexit` on first use).
    The Claude path is unchanged: immediate one-object-per-call.
    """
    if not text:
        return
    if detect_harness() == "codex":
        global _codex_flush_registered
        _pending_codex_context.append(text)
        if not _codex_flush_registered:
            atexit.register(flush_pending_context, hook_event)
            _codex_flush_registered = True
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": text,
        }
    }
    print(json.dumps(output))
    # Round-7: physical output is the proof callers key presentation state on.
    # Flush now so a closed/dead stdout raises HERE, to the caller, instead of
    # sitting in libc's buffer while the caller records "shown".
    sys.stdout.flush()


def emit_stderr(msg: str) -> None:
    sys.stderr.write(f"[allostat-mcp] {msg}\n")


def refuse_tool_call(message: str, *, hook_event: str = "PreToolUse") -> int:
    """Harness-aware tool-call refusal. Returns the exit code the hook must
    return.

    Claude Code: red-box via additionalContext + exit 2 (today's behavior,
    unchanged — the caller adds its own stderr line where it always did).

    Codex (probe-proven 2026-07-06): exit 2 does NOT block — Codex denies on
    the JSON decision object `hookSpecificOutput.permissionDecision: "deny"`,
    surfacing the reason to the model verbatim (holds even under
    bypassPermissions). The red-box text rides as the reason; any buffered
    additionalContext is dropped so the deny is the SINGLE stdout object
    (Codex one-object contract).
    """
    if detect_harness() == "codex":
        _discard_pending_context()
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }))
        return 0
    emit_additional_context(message, hook_event=hook_event)
    return 2


@dataclass(frozen=True)
class HarnessProfile:
    """Per-harness injection tuning. Claude = today's behavior; Codex leaner
    because it compresses hard ~250k tokens (keep session-start inject small)."""
    name: str
    handoff_max_chars: int
    memory_max_chars: int | None  # None = uncapped
    # Slice 2 (write-loop): granular per-file mute of the always-on/universal
    # appendices, replacing slice 1's coarse all-or-nothing bool. Inject the
    # appendices MINUS any filename in this set. Both shipped profiles now
    # exclude nothing — slice 2 wired the write loop (handoff protocol
    # backed), slice 3 wired UserPromptSubmit's per-turn `now:` line
    # (livetime protocol backed). The mechanism stays for future harnesses
    # or per-file mutes.
    always_on_exclude: frozenset[str] = frozenset()


_PROFILES: dict[str, HarnessProfile] = {
    "claude": HarnessProfile(
        "claude", handoff_max_chars=8000, memory_max_chars=None,
        always_on_exclude=frozenset(),
    ),
    "codex": HarnessProfile(
        "codex", handoff_max_chars=3000, memory_max_chars=4000,
        always_on_exclude=frozenset(),
    ),
}


def detect_harness(argv: list[str] | None = None) -> str:
    """Explicit-only detection. Codex iff `--harness codex` in argv (CODEX_HOME
    is NOT in the hook env — probe 2026-07-05). Default → claude."""
    argv = sys.argv if argv is None else argv
    try:
        i = argv.index("--harness")
    except ValueError:
        return "claude"
    if i + 1 < len(argv):
        val = argv[i + 1].strip().lower()
        if val in _PROFILES:
            return val
    return "claude"


def get_profile(harness: str | None = None) -> HarnessProfile:
    if harness is None:
        harness = detect_harness()
    return _PROFILES.get(harness, _PROFILES["claude"])
