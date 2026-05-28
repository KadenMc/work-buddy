---
name: Conversation Observability Summarize
kind: capability
description: DEPRECATED — legacy v1 entry. Generates LLM topic summaries for stale Claude Code sessions in batches. No-ops when `conversation_observability.summaries.use_incremental` is true (the v2 queue worker handles refresh on the 5-min cadence; see `summarization_worker_tick`). Preserved for rollback compatibility and as an MCP-callable v1 path; new callers should use `summarization_worker_tick` or wait for the natural cron drain.
capability_name: conversation_observability_summarize
category: conversation_observability
op: op.wb.conversation_observability_summarize
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Recency window for candidates (default 7).
    required: false
  max_sessions:
    type: int
    description: Cap per-call summarizations. Default 3 — keep small; each session is one LLM round trip.
    required: false
  force:
    type: bool
    description: Re-summarize every candidate regardless of freshness. Use after a prompt or schema bump.
    required: false
mutates_state: true
retry_policy: manual
tags:
- conversation_observability
- conversation
- observability
- summarize
aliases:
- summarize claude sessions
- generate session tldr
- topic summaries refresh
- session llm summary
parents:
- conversation_observability
---
