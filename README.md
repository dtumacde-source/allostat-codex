# Allostat local edition

Durable project memory for Codex.
Free for personal, commercial and internal organizational use under the bundled
proprietary license. Distribution of first-party Allostat is reserved to its owner.
This is not an open-source release. Third-party components retain their own licenses.

## Install on Windows

This package supports Windows x64 with trusted CPython 3.12 and its `py` launcher.
The host CLI and Git must already be installed. macOS, Linux and Windows ARM are
not supported by this wheel bundle. No Allostat account, subscription, activation
code, hosted service, or provider API key is needed for setup. Your chosen AI host
still needs its own subscription or provider access for normal AI use.

Add the official marketplace, then install:

```powershell
codex plugin marketplace add dtumacde-source/allostat-codex
codex plugin add allostat@allostat
```

Ask your agent: "Set up the installed Allostat local plugin." The bundled
local-setup skill resolves the installed plugin and builds its runtime offline
from the included wheels. Allow the host's hook trust prompt, then restart/reload
the host. Initial MCP startup may report setup required until that step completes.
See the [host installation documentation](https://developers.openai.com/learn/developers-codex-plugin).

## Choose your guard preferences once

For a new user, the recommended release defaults enable destructive-command
protection (innate-02) and disable optional Allostat guards. Tell the setup agent
"I am a new user; apply Allostat's release guard defaults." It will explicitly run
the installed entry with `run guards reset`, then read back `run guards status`.
Reset replaces ALL saved Allostat guard preferences and invalidates earlier
managed confirmations; it is never part of an automatic install or update.

Existing users keep their saved preferences. An existing installation without a
settings file keeps its earlier all-enabled behavior. If your installation history
is unclear, choose whether to keep that behavior or explicitly adopt the new
defaults. Setup and status still work while you decide. Malformed settings fail
safely to all-enabled until you explicitly repair/reset them.

Owners can enable or disable individual Allostat guards with the guards command.
When destructive protection is enabled, it pauses matched destructive actions
until its exact pending-action confirmation is satisfied. Host permissions,
package checks, memory consistency and sandbox boundaries remain separate.
The retired send guard remains absent. Guards are best-effort, not a security
sandbox or a substitute for checking an agent's actions.

## Check memory and update

Ask for an Allostat health check, then verify automatic hook delivery in a fresh
session and a memory save/read you authorize. Runtime validation alone is not
proof that the host has reloaded its hooks. Memory stays in your local project;
retrieved context may be sent to your chosen AI provider by the host.

Use the host's marketplace update controls. Each changed package identity needs
its bundled local-setup before use. Keep saved settings and memory; do not reset
preferences on update. Restart/reload after changing versions. Do not use retired
activation scripts or edit cached plugin files. A rollback selects a known prior
local package, not the retired hosted edition. Runtime repair builds a fresh
attempt without deleting memory, preferences or prior attempts.

## License and release identity

See [LICENSE](LICENSE) for the free-use proprietary terms and [THIRD_PARTY_NOTICES](plugins/allostat/THIRD_PARTY_NOTICES/) for exact
dependency notices and source-form material. This 1.5.0 distribution includes the
reviewed local engine derived from the 1.4.96 line; internal engine metadata can
retain that base version. The native plugin version includes a content digest.
Only Windows x64 / CPython 3.12 is supported by this distribution.
