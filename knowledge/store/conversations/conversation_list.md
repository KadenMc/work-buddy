---
name: Conversation List
kind: capability
description: List conversations.
capability_name: conversation_list
category: conversations
op: op.wb.conversation_list
schema_version: wb-capability/v1
parameters:
  status:
    type: string
    description: 'Filter: ''open'' (default), ''closed'', or ''all'''
tags:
- conversations
- conversation
- list
aliases:
- list chats
- active conversations
- open conversations
- what conversations are active
- recent conversations
- conversation directory
parents:
- conversations
---
