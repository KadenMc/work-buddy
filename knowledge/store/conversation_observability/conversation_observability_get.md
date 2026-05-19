---
name: Conversation Observability Get
kind: capability
description: Look up a single observed-session row by session_id, including its metadata (start/end, message_count, span count, tool usage counts). Returns None when the session has not been observed yet.
capability_name: conversation_observability_get
category: conversation_observability
op: op.wb.conversation_observability_get
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
- get
aliases:
- session observability lookup
- session metadata get
- claude session details
parents:
- conversation_observability
---
