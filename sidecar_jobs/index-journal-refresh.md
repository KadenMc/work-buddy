---
schedule: "*/5 * * * *"
recurring: true
jitter_seconds: 30
type: capability
capability: index_rebuild
params:
  partition: journal
  force: false
---
Keep the native **`journal` partition** current. Each run snapshots the Journal
transactional search outbox, completes the locked incremental build, and only then
acknowledges that snapshot. A failed or interrupted delivery remains pending and is
replayed by the next run. The first post-restore run is an implicit incremental
backfill; operators may use `force:true` for a deliberate full partition backfill.

The capability is a no-op while `index.enabled` is false and self-skips while another
partition holds the consolidated index writer gate.
