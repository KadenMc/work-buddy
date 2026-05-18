---
name: Process Backlog
kind: workflow
description: 'Process today''s Running Notes backlog through the unified source pipeline: collect line-range segments, annotate with Haiku-generated tags + summaries, embedding-fused cluster, Sonnet-refine cluster boundaries + propose a per-group action, and spawn a group umbrella thread + group sub-threads with the segments as ContextItems. The user reviews and refines via the dashboard column grid.'
workflow_name: process-backlog
execution: main
allow_override: false
steps:
- id: run-pipeline
  name: Run journal backlog source pipeline
  step_type: code
  depends_on: []
  execution: main
  auto_run:
    callable: work_buddy.pipelines.run_source_pipeline
    kwargs:
      source: journal_backlog
  visibility:
    mode: summary
  invokes: []
tags:
- daily-journal
- process
- backlog
parents:
- daily-journal
- daily-journal
---

## run-pipeline

Auto-run. Call ``work_buddy.pipelines.run_source_pipeline(source='journal_backlog', journal_date=...)``. The pipeline runs collect → annotate → precluster → refine → spawn end-to-end and returns the umbrella thread id + child group thread ids + per-cluster action proposals. The umbrella becomes visible on the dashboard's Threads tab; the user reviews + drag-drops items between groups + picks per-group actions via the column header action chip; approving the umbrella runs each child's chosen action through the standard FSM dispatch path.
