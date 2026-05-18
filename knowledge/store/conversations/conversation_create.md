---
name: Conversation Create
kind: capability
description: Create a new conversation with the user. Opens a chat sidebar on the dashboard.
capability_name: conversation_create
category: conversations
parameters:
  title:
    type: string
    description: Conversation title
    required: true
  message:
    type: string
    description: Optional initial agent message
  source:
    type: string
    description: Source identifier (auto-detected if omitted)
tags:
- conversations
- conversation
- create
aliases:
- chat
- conversation
- follow up
- multi-turn
- side chat
parents:
- conversations
- conversations
---
