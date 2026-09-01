---
schedule: "*/15 * * * *"
recurring: true
jitter_seconds: 75
type: capability
capability: index_rebuild
params:
  partition: contracts
  force: false
---
Keep the native **`contracts` partition** current. Before the Contracts authority seal
the adapter publishes nothing. After seal, each run snapshots pending revision events,
builds the current SQLite projection, and then records delivery. A crash leaves the
events pending for replay; `force:true` performs a full partition backfill.

The capability is a no-op while `index.enabled` is false and self-skips while another
partition holds the consolidated index writer gate.
