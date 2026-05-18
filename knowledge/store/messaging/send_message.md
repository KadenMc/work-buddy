---
name: Send Message
kind: capability
description: Send a message to another agent or project
capability_name: send_message
category: messaging
parameters:
  sender:
    type: str
    description: Sender project name
    required: true
  recipient:
    type: str
    description: Recipient project name
    required: true
  type:
    type: str
    description: 'Message type: status-update, question, result, escalation'
    required: true
  subject:
    type: str
    description: Message subject
    required: true
  body:
    type: str
    description: Message body text
    required: false
  thread_id:
    type: str
    description: Thread ID to continue a conversation
    required: false
  priority:
    type: str
    description: 'Priority: low, normal, high, urgent'
    required: false
tags:
- messaging
- send
- message
aliases:
- send a message
- message another agent
- contact another session
- ping another claude
- notify another project
- write to another session
- inter-agent message
parents:
- messaging
- messaging
---
