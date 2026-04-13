---
schedule: "*/5 * * * *"  # every 5 minutes
recurring: true
type: capability
capability: ir_index
params:
  action: build
  source: conversation
  days: 30  # index conversations from the last 30 days
---
Rebuild the conversation search index so that recent sessions are searchable
via `context_search`. Runs every 5 minutes — fast when nothing has changed.
