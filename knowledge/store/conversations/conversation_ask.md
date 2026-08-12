---
name: Conversation Ask
kind: capability
description: Ask a question in a conversation and optionally wait for the user's response.
capability_name: conversation_ask
category: conversations
op: op.wb.conversation_ask
schema_version: wb-capability/v1
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
  question:
    type: string
    description: Question text
    required: true
  response_type:
    type: string
    description: freeform (default), boolean, or choice
  choices:
    type: array
    description: 'For choice type: [{key, label}] or [str]'
  timeout_seconds:
    type: integer
    description: Block and wait for response (max 110s)
  consumer:
    type: string
    description: Optional persisted conversation consumer identity. Supply together with generation to fence a background driver's question to its live lease.
  generation:
    type: string
    description: Optional live lease generation. Supply together with consumer; a stale or revoked generation returns lease_lost without asking.
  message_id:
    type: string
    description: Caller-stable idempotency key; required for Co-work document-agent questions so their output-manifest binding can be replayed safely.
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- conversations
- conversation
- ask
aliases:
- chat question
- ask user
- conversation question
- follow up question
parents:
- conversations
---

`consumer` and `generation` form an optional lease fence for long-lived
conversation drivers. They must be supplied together. The question and lease
check share one transaction, so a stopped, rotated, or closed generation cannot
ask a late question.

Co-work document-agent questions also require ``message_id``. The stable key
binds the persisted assistant turn to the exact ordered Sources/Agent Execution
input manifest and prevents an ambiguous retry from creating another question.
