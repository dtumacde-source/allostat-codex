---
name: local-setup
description: Set up or check the bundled Windows local Allostat runtime after plugin installation.
---

# Set up local Allostat

This package needs Windows x64 and an installed trusted CPython 3.12 with its `py` launcher. It needs no Allostat account, activation code, subscription or server.

Resolve `../../bootstrap/local_entry.py` from this installed skill file, not the project directory. For a status request run only `py -3.12 -I -B "<resolved-entry>" status`. When the user asks to set up Allostat, run `py -3.12 -I -B "<resolved-entry>" setup`. For an explicit runtime repair request, use the same entry with `repair`; it builds a new validated attempt without deleting the old one. Setup writes only a versioned runtime under the user's `.allostat/plugin-runtimes/v1`, using bundled hash-pinned wheels without downloading. It does not change PATH, native registrations, guard settings or project memory. Missing Python or integrity failures stop with an explanation; do not invoke old activation or download an alternative package.

After successful setup, ask the user to restart/reload the host and approve its hook trust prompt. Then check local MCP initialization, automatic hook delivery and a user-approved memory save/read separately. Runtime integrity alone is not full plugin health. No model/API call is required for setup.

## First-setup preferences

Read `run guards status` after runtime setup. Never reset a valid saved policy
automatically. Missing settings do not establish that this is a new user.
If the user explicitly identifies as a new user and asks for the release defaults,
explain that optional Allostat guards will be off and destructive innate-02 on;
run this installed entry with `run guards reset`, then read `run guards status`.
Reset replaces all saved preferences and invalidates managed confirmations.
For an existing user without settings, keep the earlier all-enabled behavior
unless they explicitly choose the new defaults. If history/choice is unknown,
ask once whether to preserve the current behavior or adopt the release defaults;
do not block setup/status while awaiting the answer. Malformed settings remain
strict until an explicit repair/reset. Never change host permissions.
