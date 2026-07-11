---
name: Summarization Worker Tick
kind: capability
description: 'Drain the active summarization queue once. Runs by default, respects the Session Summaries preference, remains dormant without a plausible backend, rotates failures for fairness, and excludes visible dead letters.'
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
  bypass_inactive:
    type: bool
    description: Drain even though summaries are opted out or the backend pre-gate reports no plausible backend. For explicit one-off user requests only; also the escape hatch if the plausibility check misjudges a working setup.
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

One tick first evaluates activation and backend plausibility without making a
network call (`bypass_inactive=true` skips both gates for an explicit one-off
run). It then drains cooldown-eligible, non-dead-letter rows under the
tick and daily-cost bounds. Environmental failures rotate to the queue tail
without consuming attempts; item-intrinsic failures consume the configured
attempt budget and remain visible after dead-lettering.
