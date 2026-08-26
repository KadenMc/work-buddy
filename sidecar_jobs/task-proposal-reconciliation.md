---
schedule: "3-59/5 * * * *"
recurring: true
enabled: true
jitter_seconds: 45
type: capability
capability: task_proposals_reconcile
params:
  limit: 50
---

Resume bounded, already-human-approved task execution intents after a process
stop, then synchronize durable Journal proposal ingress and realization links.
This deterministic maintenance pass never infers or accepts a proposal and
never calls a model. Read-only and restore fences remain authoritative.
