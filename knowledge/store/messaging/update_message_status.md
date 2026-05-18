---
name: Update Message Status
kind: capability
description: Update a message's status (e.g., pending → resolved)
capability_name: update_message_status
category: messaging
parameters:
  msg_id:
    type: str
    description: Message ID
    required: true
  new_status:
    type: str
    description: New status value
    required: true
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
parents:
- messaging
- messaging
---
