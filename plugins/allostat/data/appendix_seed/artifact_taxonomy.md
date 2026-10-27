---
project: allostat
description: Four-category split for where files belong. Continuity-code / continuity-session-state / communication / deliverables. Resolves the holdover pattern where dev-internal continuity artifacts accidentally land in operator-facing Downloads. Loaded ALWAYS-ON for Allostat-managed cwds (post-A1 revert 2026-05-24).
topics: [artifact, deliverable, file-routing, downloads, taxonomy]
always_on: true
confidence_threshold: 0.7
eager_fallback: false
---

## Artifact taxonomy — where files belong

Every file the agent writes belongs to one of four categories. Routing is determined by **audience** and **versioning lifetime** — not by file format.

### The four categories

| Category | Lives in | Audience | Versioned with |
|---|---|---|---|
| **Continuity (code contract)** | `<project>/` (in the source tree) | Next agent session, indirectly | Code release cycles |
| **Continuity (session state)** | `<project>/memory/` (project-rooted, zip-portable) | Next agent session, directly | Operator's machine |
| **Communication** | `<project>-channel/` folders | Other agent (advisor, sibling dev) | Communication thread |
| **Deliverables** | `~/Downloads/` (or operator-preferred location) | Operator, for review and routing | Stakeholder review cycle |

### Examples per category

**Continuity (code contract):** STATE.md, dev_patch_log.md, deploy contracts, README.md, contract docs, ADRs. These are part of the code's contract — the deploy scripts implement them, the next session reads them to understand the system's intent. They version on release cycles alongside the code.

**Continuity (session state):** Rolling session handoffs (`<project>/memory/handoffs/<session_id>.md`, one per session, overwritten in place), observation logs (`.allostat/observations.jsonl`), pillar nudge histories (`.allostat/nudge_history.jsonl`), session_state files. These evolve per-operator-session, not on release cycles. They're how THIS operator's NEXT session picks up where the prior one left off.

**Communication:** Channel briefs between agents (`from-channel/...`, `to-channel/...`), advisor-channel inbox/outbox, multi-agent routing artifacts. The audience is another agent, not the operator. The operator may read these but isn't the intended audience.

**Deliverables:** Polished PDFs, presentations, formal reports, mockups, HTML pages, generated images, standalone scripts. Anything the operator would brief another human with. Audience is the operator (for review) and downstream stakeholders (after review).

### Decision rule when unsure

Ask two questions in this order:

1. **Who's the primary reader?**
   - Operator (for stakeholder routing) → **Deliverable** → operator's Downloads
   - Operator (for dev forensics they may or may not read) → still dev-internal; ask Q2
   - Another agent (advisor, sibling dev, next session) → **Continuity** or **Communication**; ask Q2

2. **Does it version with code or with operator's machine state?**
   - With code (the deploy scripts implement it, you'd want it in git) → **Continuity (code contract)** → project source tree
   - With operator's machine (per-session, evolves with operator's workflow) → **Continuity (session state)** → memory tree
   - With a thread of communication → **Communication** → channel folder

### Common mistakes this rule prevents

**Session-end handoffs in Downloads.** Operator doesn't read them; the next agent session does. Routing to Downloads accumulates dev-internal artifacts in operator's review-and-route folder, where they don't belong.

**Code audits as polished PDFs.** Code audits / debugging logs are dev forensics (continuity, even if operator may glance at them), NOT deliverables. Keep them as markdown.

**Strategy memos as plain markdown.** Inverse mistake. Stakeholder-facing strategy work IS a deliverable. Polish it.
