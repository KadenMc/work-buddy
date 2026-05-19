---
name: Conversation Observability Refresh
kind: capability
description: 'Refresh the conversation_observability DB: observed sessions metadata, session-attributed commits, and session-attributed file writes (with dirty-state snapshot). Stale-only by default; pass stale_only=false to force every recent session to re-load.'
capability_name: conversation_observability_refresh
category: conversation_observability
op: op.wb.conversation_observability_refresh
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: How far back to scan (default 7).
    required: false
  max_sessions:
    type: int
    description: Cap per-call work — observed_sessions refresh stops after N. Useful for time-sensitive callers.
    required: false
  stale_only:
    type: bool
    description: Skip files whose per-concern mtime ledger matches the on-disk mtime. Default True.
    required: false
mutates_state: true
retry_policy: manual
tags:
- conversation_observability
- conversation
- observability
- refresh
aliases:
- refresh conversation observability
- rebuild claude session attribution
- update session commits cache
- scan recent claude code sessions
parents:
- conversation_observability
---
