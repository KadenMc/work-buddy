---
name: Conversation Send
kind: capability
description: Send a message in an existing conversation (fire-and-forget, no response expected).
capability_name: conversation_send
category: conversations
op: op.wb.conversation_send
schema_version: wb-capability/v1
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
  message:
    type: string
    description: Message content
    required: true
  message_id:
    type: string
    description: Optional caller-stable idempotency key; the first accepted agent message wins on replay
  consumer:
    type: string
    description: Optional persisted conversation consumer identity. Supply together with generation to fence a background driver's write to its live lease.
  generation:
    type: string
    description: Optional live lease generation. Supply together with consumer; a stale or revoked generation returns lease_lost without sending.
  consumption_receipt_id:
    type: string
    description: Exact receipt returned by cowork_action_snapshot_get; required while replying to a targeted turn
mutates_state: true
retry_policy: manual
tags:
- conversations
- conversation
- send
aliases:
- chat message
- conversation message
- send chat message
- post in conversation
- speak in conversation
parents:
- conversations
---

When `message_id` is supplied, it is scoped to this conversation and the agent
role. The first successful send fixes the stored content; a retry with the same
ID returns that existing message without overwriting it. Reusing the ID for a
different conversation or role is rejected.

`consumer` and `generation` form an optional lease fence for long-lived
conversation drivers. They must be supplied together. The send and lease check
share one transaction, so a stopped, rotated, or closed generation cannot emit
a late message.

When the consumer's oldest unacknowledged turn carries an action snapshot,
`conversation_send` requires the exact generation/message-bound
`consumption_receipt_id` minted by `cowork_action_snapshot_get`. The reply is
durably linked to that receipt and carries transcript-visible consumed-target
provenance. Supplying an action snapshot identifier without fetching it cannot
authorize a reply.

If a generation restarts after the deterministic reply committed but before
the user turn was acknowledged, the successor must fetch the exact delivered
turn again and replay the same stable `message_id` with its new receipt. The
existing reply is reused only when the complete action/discussion context and
exact user message are identical. Both generation-bound receipts are linked to
the one durable reply; a different target or turn is rejected.
