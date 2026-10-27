# PATCH-204 (proposed, awaiting operator approval) — /allostat-ship-install-code slash command

**Status:** documented, NOT applied. The classifier blocked creating new slash-command config during autonomous run as a self-modification surface requiring explicit authorization. Operator can apply by accepting the file content below into `D:/dev/allostat-wrapper/commands/allostat-ship-install-code.md` (next ship picks it up automatically).

## Why

The v1.4.32 ship surfaced a confident-inference-without-source-verification failure: dev wrote `install-v3.ps1` URL from memory pattern instead of reading the canonical `access_and_backup.md`. Operator caught it. Root cause: pattern memory ≠ canonical source.

This slash command makes canonical-source-read mandatory for the install snippet surface — eliminates the hallucination class for this specific operator-facing artifact.

## Apply

Create `D:/dev/allostat-wrapper/commands/allostat-ship-install-code.md` with the content below. The next wrapper ship's bundle will include it; operator installs as normal.

## File content (paste verbatim)

```markdown
---
name: allostat-ship-install-code
description: Render the canonical operator-facing install snippet for a freshly-minted install code. PATCH-204 (2026-05-28) closes the hallucinated-install-snippet failure mode.
allowed-tools: Read
---

# /allostat-ship-install-code

When a ship has just landed and the install code has been minted, this command surfaces the canonical PowerShell snippet (verbatim from `D:/dev/allostat/access_and_backup.md`) with the install code substituted in.

## Usage

\`\`\`
/allostat-ship-install-code <INSTALL-CODE>
\`\`\`

Example:
\`\`\`
/allostat-ship-install-code MPZ5KBCETTXKNI3Y
\`\`\`

## What the command does

1. Read `D:/dev/allostat/access_and_backup.md` (canonical access doc)
2. Extract the "Standard customer install command (PowerShell)" code block
3. Substitute `<install-code>` placeholder with the provided code
4. Return the ready-to-paste PowerShell snippet + reminder to fully exit Claude Code after install

## Acceptance criteria

- The snippet ALWAYS reads from `access_and_backup.md` — never from memory
- If `access_and_backup.md` is missing or unreadable, surface the failure honestly (do NOT fall back to inferred snippet)
- The install code parameter is required; if absent, refuse with "Usage: /allostat-ship-install-code <CODE>"

## Implementation

When this slash command fires, Claude reads `D:/dev/allostat/access_and_backup.md`, finds the section "Standard customer install command (PowerShell)", substitutes the install code into the `<install-code>` placeholder, and emits the snippet + a `Then fully restart Claude Code (close ALL windows; closing one window is not enough on Windows due to env-var inheritance).` reminder.

If the file is missing, Claude responds: "access_and_backup.md not found at the canonical path; install snippet cannot be safely rendered. Either restore the file or get the snippet from `D:/dev/allostat-wrapper/docs/recommended_settings_dev.md`."
```

## Sibling rule (Phase 9 candidate)

This is the canonical source-of-truth mechanism for ONE surface. The broader rule — "verify against canonical source before asserting cause, format, or path" — is captured separately (per `from-allostat/20260528_dev_v1_4_32_ship_friction_assessment.md` §"Underlying pattern"). Phase 9 standing-rule candidate.
