---
name: File locations — your save-path conventions
description: Where Claude should write files on your machine. Allostat loads this at session start so Claude defaults to your preferred locations.
type: operator_template
scope: user
shipped_via: Allostat install scaffolder (Bucket B template — empty, customize for your machine)
---

# File locations

This file declares your save-path conventions. Allostat loads it at every session start so Claude defaults to writing files where you actually want them.

## How to use this template

Each section is a path convention. Fill in the paths your workflow actually uses on your machine. Different machines may have different paths.

## Default save location for deliverables

TODO: describe where one-off deliverables (PDFs, HTML, scripts, exports) should go. Examples: `~/Downloads/`, `~/Desktop/`, or a custom path you've created.

## Default source code location

TODO: describe where your source code projects live. Examples: `~/dev/`, `~/code/`, or a custom location.

## Session folder convention

TODO: when a task produces multiple related deliverables, describe how you want them grouped. Examples: by project name, or by date-stamped session folder.

## In-place exceptions

TODO: describe situations where files should NOT go to the default location (e.g., "in-place edits to files that already exist in a project stay in the project").

## Machine-specific overrides

TODO: if you work across multiple machines and want different defaults per machine, describe them here.
