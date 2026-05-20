---
name: Conversation Observability Summary Get
kind: capability
description: Look up the cached tldr + topic summaries for one session_id. Returns None when nothing has been summarized yet.
capability_name: conversation_observability_summary_get
category: conversation_observability
op: op.wb.conversation_observability_summary_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
tags:
- conversation_observability
- conversation
- observability
- summary
- get
aliases:
- session summary lookup
- get session tldr
- topic summary for session
parents:
- conversation_observability
---
