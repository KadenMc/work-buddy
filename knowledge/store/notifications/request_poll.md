---
name: Request Poll
kind: skill
description: 'Check/wait for a response to a previously delivered request. Without timeout_seconds: single immediate check. With timeout_seconds: blocks until response or timeout (max recommended: 110s). Response is cleared from Obsidian after reading (one-shot).'
category: notifications
op: op.wb.request_poll
schema_version: wb-skill/v1
parameters:
  notification_id:
    type: str
    description: The request ID to poll
    required: true
  timeout_seconds:
    type: int
    description: 'Poll timeout. Omit for immediate check. Max recommended: 110s'
    required: false
  interval_seconds:
    type: int
    description: 'Seconds between polls (default: 3)'
    required: false
skill_name: request_poll
tags:
- notifications
- request
- poll
aliases:
- check response
- poll modal
- check obsidian
- wait for response
parents:
- notifications
---
