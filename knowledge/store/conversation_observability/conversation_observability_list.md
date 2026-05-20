---
name: Conversation Observability List
kind: capability
description: 'List observed Claude Code sessions, sorted by recency. Optional filters: days (recency window), project (one project''s sessions only).'
capability_name: conversation_observability_list
category: conversation_observability
op: op.wb.conversation_observability_list
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Filter to sessions observed in the last N days.
    required: false
  project:
    type: str
    description: Filter by project_name.
    required: false
tags:
- conversation_observability
- conversation
- observability
- list
aliases:
- list observed sessions
- recent claude sessions
- session observability list
parents:
- conversation_observability
---
