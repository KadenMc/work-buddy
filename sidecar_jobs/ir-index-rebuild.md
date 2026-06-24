---
schedule: "*/5 * * * *"  # minute 0 of each 5-min cycle — the anchor that sibling index crons offset off
recurring: true
jitter_seconds: 45  # intra-minute de-sync only; kept under 60s so it stays within minute 0 (sibling crons own minutes 1-4)
type: capability
capability: ir_index
params:
  action: build
  source: conversation
  days: 30  # index conversations from the last 30 days
---
Rebuild the conversation search index so that recent sessions are searchable
via `context_search`. Runs every 5 minutes — fast when nothing has changed.
