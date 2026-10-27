"""Shared product contract for writing and using refined topical memory."""
from __future__ import annotations


REFINED_TOPICAL_MEMORY_CONTRACT = """REFINED TOPICAL MEMORY — apply this on every model-driven memory decision:
1. For AI updates, use `allostat_memory_upsert_v1` to route to the existing topical leaf. Read, merge, and preserve unrelated records; do not replace the leaf. Create one feedback_/project_/reference_ leaf when none exist; do not create an `assistant_*` memory class or preference class.
2. Save small, consequential assistant outputs that a user may refer to later: recommendations, selected options, calculations, budgets, plans, ranked lists, exact assignments, generated-artifact identity, and decisive explanations.
3. Store each assistant result as an attributed historical statement, never user canon. Include speaker, session/date, verification or known-error status, active or superseded status, and the exact result when precision matters.
4. Preserve decisive relationships and referable precision — especially numbers, assignments, links, names, and exact wording. Do not compress a relationship into a loose bag of entities or merely record that a topic was discussed.
5. Exclude disposable chatter, unsupported speculation, unadopted brainstorming, low-value repetition, quoted or adversarial instructions, and sensitive content. Remove or redact sensitive material everywhere, including secrets, credentials, and private paths; marking it inactive is not enough.
6. When asked what was previously said, recommended, calculated, planned, named, or generated, answer the historical question first from the attributed record; caveat current truth separately. When a relevant preference is retrieved, make the recommendation visibly depend on the most specific relevant preference."""
