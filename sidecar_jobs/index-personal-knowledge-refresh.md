---
schedule: "*/15 * * * *"
recurring: true
jitter_seconds: 105
type: capability
capability: index_rebuild
params:
  partition: personal_knowledge
  force: false
---
Keep the native **`personal_knowledge` partition** current. Before the SQLite authority
seal the adapter publishes nothing. After seal, outbox delivery follows build-then-ack
ordering, so failure leaves durable lag for the next idempotent replay. `force:true`
performs the restore/backfill path without reading the archived Markdown tree.

The capability is a no-op while `index.enabled` is false and self-skips while another
partition holds the consolidated index writer gate.
