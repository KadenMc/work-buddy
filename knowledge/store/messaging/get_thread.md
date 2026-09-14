---
name: Get Thread
kind: skill
description: Get all messages in a conversation thread
category: messaging
op: op.wb.get_thread
schema_version: wb-skill/v1
parameters:
  thread_id:
    type: str
    description: Thread ID
    required: true
skill_name: get_thread
tags:
- messaging
- get
- thread
aliases:
- view conversation thread
- message thread history
- all messages in thread
- read threaded messages
- conversation history
- thread transcript
parents:
- messaging
---
