---
schedule: "7 * * * *"  # hourly at :07, off the top-of-hour pile-up
recurring: true
type: capability
capability: run_source_pipeline
params:
  source: journal_backlog
enabled: false
---
Retired Markdown Running Notes triage.

Native Running Notes are versioned and acted on in the React Journal. The old
pipeline reads daily Markdown, proposes file actions, and ends with a section
rewrite; it must never run after the Journal database fence. Its implementation
remains only for explicit migration inspection during the archive grace period.
