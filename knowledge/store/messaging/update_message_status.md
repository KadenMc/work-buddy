---
name: Update Message Status
kind: skill
description: Update a message's status (e.g., pending → resolved)
category: messaging
op: op.wb.update_message_status
schema_version: wb-skill/v1
parameters:
  msg_id:
    type: str
    description: Message ID
    required: true
  new_status:
    type: str
    description: New status value
    required: true
skill_name: update_message_status
tags:
- messaging
- update
- message
- status
aliases:
- mark message read
- change message status
- resolve a message
- update message state
- close out a message
- message status change
- clear a blocking message
- stop a message blocking my turn
parents:
- messaging
---
