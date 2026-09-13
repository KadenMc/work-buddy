---
name: Read Message
kind: skill
description: Fetch a single message with full body content
category: messaging
op: op.wb.read_message
schema_version: wb-skill/v1
parameters:
  msg_id:
    type: str
    description: Message ID
    required: true
  session:
    type: str
    description: Session ID for read-tracking
    required: false
skill_name: read_message
tags:
- messaging
- read
- message
aliases:
- open a message
- read message contents
- full message body
- view specific message
- fetch one message
- show message body
parents:
- messaging
---
