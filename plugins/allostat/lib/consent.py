"""S3 Leg 2 — read the machine-global Allostat consent / update preference.

The installer (`install.ps1`, `Save-AllostatConsent`) writes
`~/.allostat/consent.json` at install time:

    {"consented": true, "consented_at": "...", "consent_version": "1",
     "auto_update": true}

Two consumers, with deliberately different defaults-when-absent:

- **Update notifications (Leg 3a)** — low risk (just a banner line). Default ON
  when there is no consent file, so installs that predate consent capture still
  see update notices. Governed by the `auto_update` flag.
- **Auto-fetch of updates (Leg 3b, gated)** — high risk (downloads + stages a
  new bundle over the auth path). Requires a DISTINCT explicit opt-in
  (`auto_fetch`) that the current consent flow does NOT set, so it is OFF until
  Leg 3b ships with its own consent. The notify flag does not enable auto-fetch.

- **Privacy-disclosure migration (rem-consent, 2026-07-22; two-step contract,
  round 6 2026-07-23)** — the installer bumped the consent COPY 1 -> 2 on
  2026-07-20 (the retired copy wrongly implied nothing you write ever leaves
  the machine; the shipped wire actually sends a prompt excerpt + each tool
  call's command/file path, held in memory and never stored). The installer's
  headless carry-forward is correct — auto-update must not re-prompt — but
  that left a customer on v1 transmitting under the retired disclosure with no
  corrected notice. `consent_version` used to be write-only; the functions at
  the bottom of this module READ it so the wrapper can (a) surface the
  corrected notice non-blockingly at session start, (b) withhold the
  content-bearing fields from the wire until acknowledged, and (c) record
  PRESENTATION and ACCEPTANCE as two SEPARATE facts, never one write: a
  successful emit of the notice at session start records only that it was
  presented (`record_disclosure_presented` / `presentation_confirmed`);
  `consent_version` only advances in `acknowledge_disclosure`, called from
  user-prompt-submit on the customer's next prompt after a CONFIRMED
  presentation — the informed continued use the notice itself defines as
  acknowledgment. A customer can never be recorded as having accepted a
  notice nobody actually saw. See wrapper/hooks/session-start.py and the
  three dispatch hooks.

Best-effort: any read/parse failure returns the safe default, never raises.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_CONSENT_RELPATH = (".allostat", "consent.json")


def _consent_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base.joinpath(*_CONSENT_RELPATH)


def read_consent(home: Path | None = None) -> dict:
    """Return the consent dict, or {} if absent/unreadable."""
    try:
        data = json.loads(_consent_path(home).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_notifications_enabled(home: Path | None = None) -> bool:
    """Whether to surface 'update available' notices. Default ON when no
    consent file exists (legacy installs); otherwise honors the `auto_update`
    flag the user chose at install time."""
    val = read_consent(home).get("auto_update")
    if val is None:
        return True
    return bool(val)


def auto_fetch_enabled(home: Path | None = None) -> bool:
    """Whether the high-risk auto-fetch path (Leg 3b) may run. Requires an
    explicit `auto_fetch: true` that the current consent flow does not set, so
    this is OFF until Leg 3b ships its own opt-in. Conservative by design."""
    return read_consent(home).get("auto_fetch") is True


# ---------------------------------------------------------------------------
# Existing-customer privacy-disclosure migration (rem-consent, 2026-07-22).
# ---------------------------------------------------------------------------

# The consent-copy version this build of the wrapper expects a customer to have
# seen. Must track the installer's $script:AllostatConsentVersionCurrent (bumped
# 1 -> 2 on 2026-07-20 when the privacy paragraph was corrected to match the
# shipped wire payload). When the installer bumps the copy again, bump this.
CURRENT_DISCLOSURE_VERSION = "2"

# The wrapper's transmitted-prompt cap (mirrors PROMPT_CAPTURE_CHARS in
# wrapper/hooks/user-prompt-submit.py). Stated in the notice so the disclosure
# names the exact cap the code enforces. If the cap moves there, move it here.
_PROMPT_EXCERPT_CAP = 500

# The corrected, honest notice surfaced on the session-start banner. States both
# halves the retired copy got wrong: a prompt excerpt + each tool call's command
# / file path IS sent to the server, AND that it is used only in memory and never
# stored. Kept here as the single source of truth so the hook and the tests agree.
_DISCLOSURE_NOTICE = (
    "Allostat privacy notice (updated): to decide what to remind you of, the "
    "Allostat server is sent a short excerpt of your prompt (up to "
    f"{_PROMPT_EXCERPT_CAP} characters) and the command or file path of each "
    "tool call. Those are used in memory to make that one decision and then "
    "released -- never written to disk, never logged, never stored on Allostat's "
    "servers. Your memory tree, handoffs, files, and the resolved content of any "
    "correction stay only on this computer. If your prompts routinely contain "
    "patient data or other regulated content, weigh that. Continued use "
    "acknowledges this notice."
)


def _first_run_marker_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / ".allostat" / ".first_run_complete"


def _version_int(value) -> int:
    """Parse a stored consent_version to an int; a non-numeric / missing value
    reads as 0 so it compares as OLDER than the current version (fail-safe)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def disclosure_migration_needed(home: Path | None = None) -> bool:
    """True when this install has NOT yet seen the current privacy disclosure.

    - consent.json present with a `consent_version` < current  -> True
      (the load-bearing case: a v1 customer on the retired copy).
    - consent.json present but with NO `consent_version`        -> True
      (a record that predates version tagging; older than the corrected copy).
    - consent.json absent BUT a completed first-run marker exists -> True
      (design call: a legacy pre-consent-capture install that never saw ANY
      disclosure — the "legacy absent-file" sibling case).
    - consent.json absent and no install marker                 -> False
      (a bare / never-installed home is not a consented install; fail OPEN so
      transmission is not withheld from something that never consented).
    """
    from runtime_mode import is_local
    if is_local():
        # Local evaluation sends nothing to Allostat. Do not solicit or
        # record acceptance of a hosted-service disclosure on this path.
        # Preserve the old record so hosted rollback still enforces it.
        return False
    record = read_consent(home)
    if record:
        stored = record.get("consent_version")
        if stored is None:
            return True
        return _version_int(stored) < _version_int(CURRENT_DISCLOSURE_VERSION)
    # consent.json absent/unreadable -> {}.
    return _first_run_marker_path(home).is_file()


def disclosure_ok_to_transmit(home: Path | None = None) -> bool:
    """The shared gate the dispatch hooks consult before putting content-bearing
    fields (prompt excerpt / command / file path) on the wire. False while the
    corrected disclosure is still pending; the hooks suppress those fields until
    it flips True (session-start surfaces/presents the notice, then
    acknowledge_disclosure() advances consent_version on the customer's next
    qualifying prompt after a confirmed presentation)."""
    return not disclosure_migration_needed(home)


def disclosure_notice_line() -> str:
    """The corrected privacy copy surfaced on the session-start banner."""
    return _DISCLOSURE_NOTICE


def acknowledge_disclosure(home: Path | None = None) -> bool:
    """Best-effort: record that the corrected disclosure was surfaced.

    Bumps `consent_version` to the current version and stamps
    `disclosure_acknowledged_at`, preserving `auto_update` / `consented_at` /
    any other prior fields. This is the FIRST wrapper-side write of consent.json;
    it mirrors the best-effort, non-blocking self-ack pattern in
    lib/migration_provenance.py (auto_acknowledge). Never raises — a write
    failure returns False and leaves consent_version < current, so the transmit
    gate keeps withholding (fail-safe). Returns True on a successful write.
    """
    try:
        path = _consent_path(home)
        record = dict(read_consent(home))  # {} when absent
        record["consent_version"] = CURRENT_DISCLOSURE_VERSION
        record.setdefault("consented", True)
        record["disclosure_acknowledged_at"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def record_disclosure_presented(home: Path | None = None) -> bool:
    """Record that the corrected disclosure was ACTUALLY surfaced (the emit
    carrying it succeeded). Presentation is a separate fact from acceptance:
    this stamps `disclosure_presented_at`/`disclosure_presented_version` and
    NEVER touches consent_version — the version advances only in
    acknowledge_disclosure(), on the customer's next qualifying action after
    a confirmed presentation (round-6 P1-2: acknowledging an unseen notice
    is not consent). Never raises; returns True on a successful write.
    """
    try:
        path = _consent_path(home)
        record = dict(read_consent(home))  # {} when absent
        record["disclosure_presented_version"] = CURRENT_DISCLOSURE_VERSION
        record["disclosure_presented_at"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def presentation_confirmed(home: Path | None = None) -> bool:
    """True iff the CURRENT disclosure version was confirmed presented —
    the precondition for treating continued use as acknowledgment."""
    record = read_consent(home)
    return bool(
        record.get("disclosure_presented_at")
        and record.get("disclosure_presented_version")
        == CURRENT_DISCLOSURE_VERSION
    )
