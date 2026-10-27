---
name: Walkthrough mode — appendix to working preferences
description: Detailed rules for real-time walkthrough situations. Loaded by Allostat when walkthrough/step-by-step/high-stakes-filing language fires.
type: operator_template
scope: user
shipped_via: Allostat install scaffolder (Bucket B template — empty, customize for high-stakes situations)
---

# Walkthrough mode — appendix to working preferences

This file extends `working_preferences.md` with detailed rules for high-stakes situations where you want real-time step-by-step walkthrough rather than batch delivery.

## When to invoke walkthrough mode

TODO: describe situations where you want walkthrough mode (e.g., "LLC formation, EIN application, IRS filings, business bank account opening, payroll setup, complex Stripe/cloud migrations").

## What walkthrough mode requires

TODO: describe Claude's behavior in walkthrough mode (e.g., "explain each step in plain English before executing; pause after each step for my confirmation; surface alternatives at decision points").

## Pre-walkthrough checklist

TODO: describe what should happen before the walkthrough starts (e.g., "list every step that will happen; estimate total time; confirm I have credentials/access needed").

## Reversibility flag

TODO: describe how you want irreversible steps flagged (e.g., "any step that creates legal/financial state must be explicitly flagged before execution").

## Capture conventions

TODO: describe how you want the session captured (e.g., "log every step + confirmation; save to a dated session folder for future reference").
