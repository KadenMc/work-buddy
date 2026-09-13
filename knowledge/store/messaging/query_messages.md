---
name: Query Messages
kind: skill
description: Query messages by recipient, sender, status, or limit
category: messaging
op: op.wb.query_messages
schema_version: wb-skill/v1
parameters:
  recipient:
    type: str
    description: Filter by recipient
    required: false
  sender:
    type: str
    description: Filter by sender
    required: false
  status:
    type: str
    description: Filter by status (e.g., pending)
    required: false
  limit:
    type: int
    description: Max messages to return (default 50)
    required: false
skill_name: query_messages
tags:
- messaging
- query
- messages
aliases:
- list messages
- find messages
- show inter-agent messages
- recent messages
- what messages have arrived
- search messaging log
- filter messages
parents:
- messaging
---
