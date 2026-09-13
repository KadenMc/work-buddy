---
name: Send Message
kind: skill
description: Send a message to another agent or project
category: messaging
op: op.wb.send_message
schema_version: wb-skill/v1
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
skill_name: send_message
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
---
