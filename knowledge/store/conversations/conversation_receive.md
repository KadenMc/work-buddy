---
name: Conversation Receive
kind: capability
description: Receive the oldest unacknowledged user turn for a generation-leased conversation consumer.
capability_name: conversation_receive
category: conversations
op: op.wb.conversation_receive
schema_version: wb-capability/v1
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
  consumer:
    type: string
    description: Stable consumer name supplied by the owning workflow
    required: true
  generation:
    type: string
    description: Current lease generation supplied by the owning workflow
    required: true
  timeout_seconds:
    type: integer
    description: Block and wait for a user turn (max 110s)
mutates_state: true
retry_policy: replay
tags:
- conversations
- conversation
- receive
- durable inbox
aliases:
- receive conversation message
- wait for user turn
- conversation inbox
- consume chat message
parents:
- conversations
---

Returns the oldest user turn that this consumer has not acknowledged. Delivery
does not advance the cursor, so a process restart receives the same turn again.
Only the current generation lease can receive; a replaced driver gets
`lease_lost` and must stop.
