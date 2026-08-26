---
name: Conversation Poll
kind: capability
description: "Check or await an exact conversation question, defaulting to the current question."
capability_name: conversation_poll
category: conversations
op: op.wb.conversation_poll
schema_version: wb-capability/v1
parameters:
  conversation_id:
    type: "string"
    description: "Conversation ID"
    required: true
  timeout_seconds:
    type: "integer"
    description: "Block and wait (max 110s)"
  message_id:
    type: "string"
    description: "Exact question ID to inspect; a later question cannot replace this target"
  consumer:
    type: "string"
    description: "Persisted conversation consumer; required together with generation for scoped hosted agents"
  generation:
    type: "string"
    description: "Live lease generation; a stopped or rotated driver cannot read question content or responses"
tags:
- conversations
- conversation
- poll
aliases:
- check conversation
- conversation response
- poll chat
- has user answered
- conversation answered
- check for reply
parents:
- conversations
---
