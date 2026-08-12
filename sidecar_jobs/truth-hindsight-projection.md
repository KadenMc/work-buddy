---
schedule: "4-59/5 * * * *"
recurring: true
jitter_seconds: 45
type: capability
capability: truth_hindsight_projection_tick
params:
  limit_per_store: 20
  reconcile: true
---

Reconcile current, policy-eligible confirmed Truth claims into Hindsight's
replaceable semantic-derivative projection. The worker is dormant while
`hindsight.truth_projection.enabled` is false. It uses the Truth-owned outbox,
Agent Execution disclosure receipts, Sources dependency usages, and destination
inspection to recover missed wake-ups and ambiguous acknowledgements without
making Hindsight authoritative.
