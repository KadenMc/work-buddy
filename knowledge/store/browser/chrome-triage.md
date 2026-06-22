---
name: Chrome Triage
kind: workflow
description: 'Triage currently-open Chrome tabs through the unified source pipeline: collect tabs, attach cached Haiku summaries + tag signals, embedding-fused cluster (Louvain over embedding+tag+window-gated proximity), Sonnet-refine cluster boundaries + propose a per-cluster action (close all tabs / group in Chrome / route to tasks / etc.), and spawn a group umbrella thread + group sub-threads with the tabs as ContextItems. The user reviews and approves via the dashboard column grid + per-column action chip.'
workflow_name: chrome-triage
execution: main
allow_override: false
steps:
- id: run-pipeline
  name: Run Chrome triage source pipeline
  step_type: code
  depends_on: []
  execution: main
  auto_run:
    callable: work_buddy.pipelines.run_source_pipeline
    kwargs:
      source: chrome_triage
      engagement_window: 24h
      include_summaries: true
    timeout: 240
    # The source pipeline drives outbound effects (summary persistence,
    # downstream task creation) whose idempotency under partial completion
    # is not audited — disable retry until that audit lands.
    retry_on_timeout: false
  visibility:
    mode: summary
  invokes: []
tags:
- browser
- chrome
- triage
parents:
- browser
- browser
---

## run-pipeline

Auto-run. Call ``work_buddy.pipelines.run_source_pipeline(source='chrome_triage', engagement_window='24h', include_summaries=True)``. The pipeline runs collect → annotate → precluster → refine → spawn end-to-end and returns the umbrella thread id + child group thread ids + per-cluster action proposals. The umbrella becomes visible on the dashboard's Threads tab; the user reviews + drag-drops tabs between groups + picks per-group actions via the column-header action chip; approving the umbrella runs each child's chosen action through the standard FSM dispatch path (calls ``chrome_tab_close`` / ``chrome_tab_group`` / ``chrome_route_to_tasks`` / etc.).
