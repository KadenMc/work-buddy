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
