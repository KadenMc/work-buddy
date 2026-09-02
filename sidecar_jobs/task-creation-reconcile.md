---
schedule: "* * * * *"
recurring: true
jitter_seconds: 10
type: capability
capability: task_creation_reconcile
params:
  limit: 25
---

Boundedly resume hidden task-plus-document creation and existing-task attachment
intents through every cross-store crash boundary. The operation reuses the
original durable request, actor, idempotency key, participant receipts, and
coordinator decision; it never invents a replacement task or silently changes
the requested Truth policy.
