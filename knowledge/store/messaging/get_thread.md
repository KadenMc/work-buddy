---
name: Get Thread
kind: capability
description: Get all messages in a conversation thread
capability_name: get_thread
category: messaging
op: op.wb.get_thread
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Thread ID
    required: true
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
