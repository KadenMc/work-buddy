---
name: Session Get
kind: capability
description: Browse messages in a Claude Code session. Paginated with role/type filtering. Use after context_search finds a session.
capability_name: session_get
category: context
op: op.wb.session_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or partial (8-char) session UUID
    required: true
  offset:
    type: int
    description: Start index (default 0)
    required: false
  limit:
    type: int
    description: Max messages to return (default 10)
    required: false
  roles:
    type: str
    description: 'Comma-separated: ''user'', ''assistant'''
    required: false
  message_types:
    type: str
    description: 'Comma-separated: ''text'' (has text content), ''tool_use'' (has tool calls)'
    required: false
  query:
    type: str
    description: Substring filter on message text (case-insensitive)
    required: false
tags:
- context
- session
- get
aliases:
- inspect session
- browse conversation
- session messages
- read session
- drill into session
parents:
- context
---
