---
name: Conversation Observability Summarize
kind: capability
description: Generate (or refresh) LLM topic summaries for up to `max_sessions` stale Claude Code sessions. Each session produces one tldr + a bounded topic list, persisted with model + prompt-version provenance for stale detection. Disabled by default; call deliberately.
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
