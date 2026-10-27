---
project: example
description: Example appendix file demonstrating the YAML frontmatter convention. Allostat's appendix system loads this on topic match. Customer replaces or adds project-specific appendix files in their own appendix folder.
topics: [example, demo, appendix-mechanism]
confidence_threshold: 0.7
eager_fallback: false
---

# Example appendix — Allostat appendix mechanism demo

This is an example appendix file. Allostat ships it as a demonstration of how the appendix-loading mechanism works on a fresh install. It serves as both documentation AND a working example.

## How the appendix system works

The appendix system lets you ship project-specific deep context that loads ONLY when relevant — keeping your session start tight while making detail available on demand.

**The flow:**

1. Your `~/.claude/allostat/projects/<project>_appendix/` folder holds appendix `.md` files
2. Each file has YAML frontmatter declaring its `topics` + `confidence_threshold` + `eager_fallback` setting
3. When you submit a prompt, Allostat's UserPromptSubmit hook scans the prompt for topic keywords
4. Matching appendix files load into Claude's context via `additionalContext`
5. Non-matching files stay on disk, costing zero tokens

## Frontmatter fields

- **project**: project this appendix belongs to (matches the folder name minus `_appendix` suffix)
- **description**: short description for `/allostat-appendix-list` or similar discovery tooling
- **topics**: list of trigger keywords. Word-boundary match by default.
- **confidence_threshold**: minimum match confidence (0.0-1.0) before this file loads. Higher = stricter.
- **eager_fallback**: if `true`, use substring match (not word-boundary). Useful for plurals/inflections.

## Size caps

- Per-file: ≤20 KB
- Total per appendix folder: ≤100 KB
- Enforced by `lib/appendix_system.py:audit_seed_dir()` at bundle build time

## How to add your own appendices

1. Create `~/.claude/allostat/projects/<your_project>_appendix/` if absent
2. Author a `.md` file with the frontmatter above
3. Topics should be specific enough not to false-fire on unrelated prompts
4. Test by submitting a prompt containing your topic keyword and verifying the appendix loads (look for `tier_loaded` observation in your `.allostat/observations.jsonl`)

## What ships with Allostat

Only this one example file ships in the install bundle. Allostat is mechanism, not content — your appendix folder fills from your own project documentation as you discover what your work needs at hand.
