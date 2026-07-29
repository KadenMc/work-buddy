---
name: Co-work Action Snapshot Get
kind: capability
description: Read the exact frozen document version and focus attached to a targeted Co-work Chat turn.
capability_name: cowork_action_snapshot_get
category: cowork
op: op.wb.cowork_action_snapshot_get
schema_version: wb-capability/v1
parameters:
  store_id:
    type: string
    description: Bound Co-work Truth store ID
    required: true
  document_id:
    type: string
    description: Bound Co-work document ID
    required: true
  action_snapshot_id:
    type: string
    description: Exact action_snapshot_id returned in the received user turn
    required: true
  message_id:
    type: string
    description: Exact targeted user message ID returned by conversation_receive
    required: true
mutates_state: true
retry_policy: replay
tags:
- cowork
- document
- chat
- action snapshot
- frozen context
aliases:
- read targeted chat context
- get frozen document context
- read action snapshot
parents:
- cowork
---

Returns the complete immutable Markdown projection plus the exact target text,
selector, hashes, and allowed-change boundary captured for one targeted Chat
turn. A hosted document agent is generation-fenced to its bound store,
document, conversation, consumer generation, and received message. Use the
exact `action_snapshot_id` and `message_id` delivered by
`conversation_receive`; never substitute the current document or infer a focus
from transcript text. Every terminal fetch outcome durably returns a
`consumption_receipt_id`. The targeted reply and acknowledgement must echo
that receipt; merely echoing an unfetched action snapshot is rejected.

If the named frozen view is missing or fails integrity validation, the
capability returns `ok=false`, `status=action_snapshot_unavailable`, and a
generation-bound `consumption_receipt_id` whose `fetch_outcome` is
`unavailable`. That receipt permits only the normal receipt-bound reply and
acknowledgement path: explain truthfully that the exact context could not be
opened and make no document proposal or comment. This prevents a corrupt
snapshot from trapping the durable user turn while preserving its exact
message, action, consumer, and generation audit trail.

After a consumer restart, call the capability again under the new generation.
If the prior generation already committed the stable reply but did not
acknowledge the turn, the conversation store permits replay only when the old
and new receipts prove the same conversation, user message, action snapshot,
and transcript-visible target or Co-think context. It preserves the first
reply, binds the new receipt to it, and accepts the acknowledgement; changed
semantics are rejected.
