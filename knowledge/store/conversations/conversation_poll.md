---
name: Conversation Poll
kind: capability
description: Check if the latest question in a conversation has been answered.
capability_name: conversation_poll
category: conversations
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
  timeout_seconds:
    type: integer
    description: Block and wait (max 110s)
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
- conversations
---
