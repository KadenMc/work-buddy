---
name: Conversation Send
kind: capability
description: Send a message in an existing conversation (fire-and-forget, no response expected).
capability_name: conversation_send
category: conversations
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
  message:
    type: string
    description: Message content
    required: true
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
- conversations
---
