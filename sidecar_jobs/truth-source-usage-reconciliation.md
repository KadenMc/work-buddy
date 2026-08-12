---
schedule: "2-59/5 * * * *"
recurring: true
jitter_seconds: 45
type: capability
capability: truth_source_usage_reconcile
params:
  limit_per_store: 100
---

Recover committed Truth-to-Sources usage acknowledgements left incomplete by a
process stop. The operation is bounded and idempotent; redaction races remain
explicit maintenance state rather than being reported as clean.
