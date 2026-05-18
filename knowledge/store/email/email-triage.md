---
name: Email Triage
kind: workflow
description: 'Triage recent unread email through the unified source pipeline: fetch via the Thunderbird bridge, synthesise tags from sender/folder/labels, embedding-fused cluster, refine cluster boundaries + propose a per-cluster action (close / create one task per email / create umbrella task), and spawn a group umbrella thread + group sub-threads with the emails as ContextItems. The user reviews and approves via the dashboard''s Threads tab.'
workflow_name: email-triage
execution: main
allow_override: false
steps:
- id: run-pipeline
  name: Run email triage source pipeline
  step_type: code
  depends_on: []
  execution: main
  auto_run:
    callable: work_buddy.pipelines.run_source_pipeline
    kwargs:
      source: email_triage
      days_back: 2
      max_messages: 50
      unread_only: true
    timeout: 240
  visibility:
    mode: summary
  invokes: []
tags:
- email
- thunderbird
- triage
parents:
- email
- email
---

## run-pipeline

Auto-run. Call ``work_buddy.pipelines.run_source_pipeline(source='email_triage', days_back=2, max_messages=50, unread_only=True)``. The pipeline runs collect → annotate → precluster → refine → spawn end-to-end. Per-cluster actions resolve through the email action library (``email_close``, ``email_create_tasks``, ``email_create_umbrella_task``). ``email_close`` is advisory: dismisses the Thread without touching the underlying mailbox (Thunderbird bridge is read-first in v1). The umbrella + children become visible on the dashboard's Threads tab; approving the umbrella runs each child's chosen action through the standard FSM dispatch path.
