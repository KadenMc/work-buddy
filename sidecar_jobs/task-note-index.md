---
schedule: "*/5 * * * *"  # every 5 minutes
recurring: true
type: capability
capability: ir_index
params:
  action: build
  source: task_note
  days: 30  # ignored by the task_note source (notes don't expire)
---
Rebuild the task-note IR index so that hybrid (BM25 + dense) search over
note bodies stays fresh. The source adapter discovers every task whose
`note_uuid` column is non-null and whose markdown file exists on disk;
the engine skips unchanged files via mtime, so this is fast when nothing
has changed.

Searched via `context_search` with `source="task_note"`.
