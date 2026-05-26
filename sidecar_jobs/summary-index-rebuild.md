---
schedule: "*/5 * * * *"  # every 5 minutes
recurring: true
jitter_seconds: 105  # spread 5-minute pile-ups; lands off the other index crons
type: capability
capability: ir_index
params:
  action: build
  source: summary
  days: 30
---
Rebuild the IR `summary` index so summarization-framework summaries stay
searchable via `context_search(source="summary", ...)` and `summary_search`.
The source adapter (`work_buddy/ir/sources/summary.py`) discovers
`summary_items` rows with `status='ok'` and emits one Document per
`SummaryNode` row; the engine skips items whose `generated_at` matches
the last-indexed mtime, so this is fast when nothing has changed.

Pairs with `conversation-observability-summarize.md` (every 2h): the
producer cron writes new summaries, the index cron picks them up within
five minutes.
