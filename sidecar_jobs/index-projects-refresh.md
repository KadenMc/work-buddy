---
schedule: "*/15 * * * *"
recurring: true
jitter_seconds: 45
type: capability
capability: index_rebuild
params:
  partition: projects
  force: false
---
Keep the SQLite-backed **`projects` partition** current. Project revision events are
acknowledged only after the locked index build completes; an interrupted run replays
the still-pending batch idempotently. `force:true` performs a full partition backfill.

The capability is a no-op while `index.enabled` is false and self-skips while another
partition holds the consolidated index writer gate.
