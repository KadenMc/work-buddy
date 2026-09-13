---
name: Conversation Observability Uncommitted
kind: skill
description: Return the legacy session_uncommitted report from the DB-backed attribution layer. Refreshes first; see also context/session_uncommitted (the thin compat wrapper).
category: conversation_observability
op: op.wb.conversation_observability_uncommitted
schema_version: wb-skill/v1
parameters:
  days:
    type: int
    description: Recency window (default 7).
    required: false
mutates_state: true
retry_policy: manual
skill_name: conversation_observability_uncommitted
tags:
- conversation_observability
- conversation
- observability
- uncommitted
aliases:
- uncommitted sessions report
- which sessions left dirty files
- claude code uncommitted work
parents:
- conversation_observability
---
