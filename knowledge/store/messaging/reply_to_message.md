---
name: Reply To Message
kind: capability
description: Reply to an existing message
capability_name: reply_to_message
category: messaging
parameters:
  msg_id:
    type: str
    description: ID of message to reply to
    required: true
  sender:
    type: str
    description: Sender project name
    required: true
  body:
    type: str
    description: Reply body text
    required: true
  type:
    type: str
    description: 'Reply type (default: ack)'
    required: false
tags:
- messaging
- reply
- to
- message
aliases:
- respond to message
- answer a message
- reply to another agent
- continue conversation
- message reply
- acknowledge message
parents:
- messaging
- messaging
---
