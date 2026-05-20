---
name: Run Source Pipeline
kind: capability
description: 'Run an end-to-end source pipeline: collect raw items, annotate with tags + summary, algorithmically cluster, LLM-refine cluster boundaries + per-cluster action proposals (local-first tier_chain), and spawn a group umbrella thread + group sub-threads with the items as ContextItems. Replaces the per-source journal/chrome/email scan entry points.'
capability_name: run_source_pipeline
category: threads
op: op.wb.run_source_pipeline
schema_version: wb-capability/v1
parameters:
  source:
    type: str
    description: Registered pipeline name (one of 'chrome_triage', 'email_triage', 'journal_backlog')
    required: true
  journal_date:
    type: str
    description: 'Journal pipeline: YYYY-MM-DD; defaults to today'
    required: false
  profile:
    type: str
    description: 'Journal pipeline: override segmentation tier profile'
    required: false
  engagement_window:
    type: str
    description: 'Chrome pipeline: engagement lookback (e.g. ''12h'', ''24h'')'
    required: false
  include_summaries:
    type: bool
    description: 'Chrome pipeline: attach cached Haiku summaries (default True)'
    required: false
  summary:
    type: str
    description: 'Chrome pipeline: human-readable scrape summary for the umbrella title'
    required: false
  scrape_id:
    type: str
    description: 'Chrome pipeline: per-scrape id surfaced in audit metadata'
    required: false
  days_back:
    type: int
    description: 'Email pipeline: how far back to scan in days (default 2)'
    required: false
  max_messages:
    type: int
    description: 'Email pipeline: hard cap on candidates per run (default 50)'
    required: false
  unread_only:
    type: bool
    description: 'Email pipeline: only consider unread messages (default true)'
    required: false
  folder_path:
    type: str
    description: 'Email pipeline: limit to a specific folder URI'
    required: false
  account_id:
    type: str
    description: 'Email pipeline: limit to a specific account'
    required: false
  include_body_chars:
    type: int
    description: 'Email pipeline: fetch body up to N chars per message (default 0 = headers only)'
    required: false
mutates_state: true
retry_policy: manual
tags:
- threads
- run
- source
- pipeline
aliases:
- run pipeline
- process backlog
- scan journal
- triage chrome
- triage email
- scan inbox
- run source pipeline
- spawn group threads
parents:
- threads
---
