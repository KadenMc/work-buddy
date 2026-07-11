---
name: Summarization Backfill
kind: capability
description: Observe historical conversation sessions and enqueue every missing or stale summary without making LLM calls.
capability_name: summarization_backfill
category: summarization
op: op.wb.summarization_backfill
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Historical discovery window in days. Defaults to 3650.
    required: false
  observe:
    type: bool
    description: Refresh the observed-session ledger before reconciliation. Defaults to true.
    required: false
  max_sessions:
    type: int
    description: Optional bound for the observation pass.
    required: false
mutates_state: true
retry_policy: safe
tags:
- summarization
- backfill
- queue
- conversation_observability
aliases:
- backfill conversation summaries
- revive summary dead letters
parents:
- summarization
---

Widens local session observation, then reconciles missing or stale v2 summaries
into the durable queue. Re-enqueueing resets the attempt/error state, so running
this after correcting an item-intrinsic failure also revives dead letters. The
operation is enqueue-only; the background worker still enforces cooldown and
daily budget limits.
