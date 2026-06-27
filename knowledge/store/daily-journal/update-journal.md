---
name: Update Journal
kind: workflow
description: Append activity-detected Log entries to an Obsidian journal file.
workflow_name: update-journal
execution: main
params_schema:
  target:
    type: str
    description: Which day's journal to update — "today", "yesterday", or "YYYY-MM-DD". Omit to auto-detect (today, or ask near midnight).
    required: false
steps:
- id: read-journal
  name: Read today's journal and determine activity window
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.journal.read_journal_state
    kwargs:
      create_on_read: true
    input_map:
      target: __params__.target
    timeout: 90
  invokes: []
- id: collect
  name: Collect fresh context (scoped to activity window)
  step_type: code
  depends_on:
  - read-journal
  auto_run:
    callable: work_buddy.journal.collect_scoped_context
    input_map:
      journal_state: read-journal
    timeout: 300
  visibility:
    mode: summary
    include_keys:
    - bundle_path
    - collect_since
    - collect_until
  invokes: []
- id: synthesize
  name: Synthesize activity into journal-style Log entry
  step_type: reasoning
  depends_on:
  - collect
  invokes: []
- id: write
  name: Present summary, get consent, append to journal
  step_type: reasoning
  depends_on:
  - synthesize
  requires_individual_consent: true
  invokes: []
tags:
- daily-journal
- update
- journal
parents:
- daily-journal
- daily-journal
dev_notes: The `collect` step calls `collect_scoped_context` which fans `collect_bundle` across ~10 sources. With cold caches and a multi-hour window, total wall time routinely lands in the 100-200s range (multi-repo GitSource across 16 repos adds ~5-15s on top of the chat/chrome/obsidian scans). Timeout raised from 180s -> 300s on 2026-04-23 after a real timeout in session 985e3e38 even with a 1-hour window. When `t-a373609f` lands (cache `max_age_seconds` plumbing), the caller can pass a floor so re-runs within a minute or two are instant and the timeout becomes largely cosmetic.
---
