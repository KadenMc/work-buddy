---
name: "Assisted Draft Context Get"
kind: capability
description: "Consume the exact explicitly disclosed initial or authored-turn form snapshot for one hosted assistance worker."
capability_name: "assisted_draft_context_get"
category: "assistance"
op: "op.wb.assisted_draft_context_get"
schema_version: "wb-capability/v1"
parameters:
  assistant_session_id:
    type: "string"
    description: "Exact assistant session from the worker's server-authored binding."
    required: true
  conversation_id:
    type: "string"
    description: "Bound conversation ID."
    required: true
  consumer:
    type: "string"
    description: "Bound assistance inbox consumer."
    required: true
  generation:
    type: "string"
    description: "Exact live lease generation."
    required: true
  message_id:
    type: "string"
    description: "Initial snapshot ID from the launch brief, or exact user-message ID first returned by conversation_receive."
    required: true
mutates_state: true
retry_policy: "manual"
auto_retry: false
parents:
- "services/dashboard/react/assisted-drafts"
tags:
- "dashboard"
- "assistance"
- "conversations"
---

Returns only the exact disclosed initial or received-turn form snapshot, purpose/schema, bounded conversation context and host receipts. Checks exact worker binding and policy, records Sources disclosure before returning content, and issues a generation-bound consumption receipt. An ambiguous release is not automatically replayable. No arbitrary draft or live-widget read is permitted.
