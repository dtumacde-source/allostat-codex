"""Allostat per-class silo modules.

Ported from the v2.3 plugin's lib/silos/ into the v2.4 wrapper as part
of PATCH-167 silo_inscribe port (Path beta — drift first, then voice /
customization / workflow / confidence_recovery).

Each module owns one signal class:
  drift.py               - stale-fact-in-context cases
  voice.py               - off-voice phrasing instances
  customization.py       - decision_class -> option mappings
  workflow.py            - per-workflow gotchas
  confidence_recovery.py - over/under-confidence corrections

All build on silo_base (fingerprint + entry primitives) and store at
`<state_dir>/silos/<class>.jsonl`. Privacy moat: entries stay
operator-local; only abstract fingerprint hashes cross to server during
recall.
"""
