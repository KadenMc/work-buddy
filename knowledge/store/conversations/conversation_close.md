---
name: Conversation Close
kind: skill
description: Close a conversation.
category: conversations
op: op.wb.conversation_close
schema_version: wb-skill/v1
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
skill_name: conversation_close
tags:
- conversations
- conversation
- close
aliases:
- end conversation
- close chat
- finish conversation
- wrap up conversation
- close dashboard chat
parents:
- conversations
---
