---
schedule: "*/5 * * * *"  # every 5 minutes
recurring: true
jitter_seconds: 90  # spread 5-minute pile-ups; lands off ir-index-rebuild's offset
type: capability
capability: ir_index
params:
  action: build
  source: task_note
  days: 30  # ignored by the task_note source (notes don't expire)
---
Rebuild the task-note IR index so that hybrid (BM25 + dense) search over
note bodies stays fresh. Before native Task activation, the source adapter
uses the frozen legacy Markdown compatibility reader. After activation, it
discovers task-linked Co-work document heads directly and uses their content
fingerprints to skip unchanged documents. It does not require Markdown task
notes or Obsidian in native mode.

Searched via `context_search` with `source="task_note"`.
