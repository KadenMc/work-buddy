---
schedule: "*/15 * * * *"  # task-note cadence: small, low-volume corpus
recurring: true
jitter_seconds: 90  # distinct offset across consolidated-index refresh jobs
type: capability
capability: index_rebuild
params:
  partition: task_note
  force: false
---
Keep the index's **`task_note` partition** current so task-linked Co-work documents stay
searchable. The source discovers document heads and uses content fingerprints to skip unchanged
documents, so recurring builds are incremental. The `index_rebuild` op self-skips while any index
build is running because all partitions share one single-writer database.

**One job per partition by design.** This job is a sibling of the other
`index-<partition>-refresh` jobs and is not folded into a shared `build_all` cron.
