---
name: Conversation Observability List
kind: capability
description: 'List observed agent sessions (Claude Code, Codex, …) by conversation time, newest first. Recency is measured by when the conversation happened, not when the row was last refreshed, so a resumed old session does not resurface as recent. Filters: since/until (explicit window, ISO or relative shorthand) or days (recency), and project.'
capability_name: conversation_observability_list
category: conversation_observability
op: op.wb.conversation_observability_list
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Recency by conversation time — sessions whose activity falls within the last N days.
    required: false
  project:
    type: str
    description: Filter by project_name.
    required: false
  since:
    type: str
    description: 'Explicit window start: ISO datetime or relative shorthand (e.g. "2d", "18h"). Sessions are kept when their [start, end] overlaps the window.'
    required: false
  until:
    type: str
    description: 'Explicit window end: ISO datetime or relative shorthand. Defaults to now.'
    required: false
tags:
- conversation_observability
- conversation
- observability
- list
aliases:
- list observed sessions
- recent agent sessions
- session observability list
parents:
- conversation_observability
---
