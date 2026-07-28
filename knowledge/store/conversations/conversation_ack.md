---
name: Conversation Acknowledge
kind: capability
description: Acknowledge exactly the oldest delivered user turn for a generation-leased conversation consumer.
capability_name: conversation_ack
category: conversations
op: op.wb.conversation_ack
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
  message_id:
    type: string
    description: Exact user message ID returned by conversation_receive
    required: true
mutates_state: true
retry_policy: replay
tags:
- conversations
- conversation
- acknowledge
- durable inbox
aliases:
- acknowledge conversation message
- ack user turn
- advance conversation inbox
parents:
- conversations
---

Advances the consumer cursor only when `message_id` is the currently delivered
oldest turn. Out-of-order acknowledgements do not skip messages. A driver should
acknowledge only after its reply and any requested proposal/comment have
succeeded.
