---
name: Summarization Worker Tick
kind: capability
description: 'Drain the summarization queue once (PRD §6 O2). Picks eligible (cooldown-passed) entries FIFO, bounded by `worker_tick_limit` and the daily cost budget. Used by the sidecar cron and inline-trigger from `/wb-journal-update` and `/wb-morning`. Pass `bypass_cooldown=true` for explicit user-triggered refresh; `bypass_budget=true` to override the daily ceiling.'
capability_name: summarization_worker_tick
category: summarization
op: op.wb.summarization_worker_tick
schema_version: wb-capability/v1
parameters:
  bypass_cooldown:
    type: bool
    description: Skip the per-session cooldown (default 30 min). Use for `/wb-summarize-now`-style explicit triggers.
    required: false
  bypass_budget:
    type: bool
    description: Skip the daily-budget circuit-breaker. Use sparingly — for user-confirmed expensive sweeps only.
    required: false
  limit:
    type: int
    description: Max queue entries to process this tick. Default from config (`conversation_observability.summaries.worker_tick_limit`, default 20).
    required: false
mutates_state: true
retry_policy: manual
tags:
- summarization
- worker
- queue
- v2
- conversation_observability
aliases:
- drain summarization queue
- run v2 summarization worker
- summarize queued sessions
parents:
- summarization
---
