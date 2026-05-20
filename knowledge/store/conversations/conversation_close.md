---
name: Conversation Close
kind: capability
description: Close a conversation.
capability_name: conversation_close
category: conversations
op: op.wb.conversation_close
schema_version: wb-capability/v1
parameters:
  conversation_id:
    type: string
    description: Conversation ID
    required: true
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
