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
  action_snapshot_id:
    type: string
    description: Deprecated redundant echo of the targeted turn action_snapshot_id
  consumption_receipt_id:
    type: string
    description: Exact receipt returned by cowork_action_snapshot_get; required for a targeted turn and omitted otherwise
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
oldest turn. Out-of-order acknowledgements do not skip messages. When the
delivered message context names an `action_snapshot_id`, the driver must first
fetch it and echo the resulting `consumption_receipt_id` in
`conversation_ack`; a missing, mismatched, or reply-less receipt does not
advance the cursor. A driver should acknowledge only after its receipt-bound
reply and any requested proposal/comment have succeeded.

After a generation restart, a predecessor receipt cannot authorize the
successor's acknowledgement. The successor fetches the same exact turn,
replays the deterministic reply with its newly minted receipt, and
acknowledges with that receipt. This preserves generation authorization while
reusing the already-durable reply only for an identical target and user turn.
