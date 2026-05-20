---
name: Request Poll
kind: capability
description: 'Check/wait for a response to a previously delivered request. Without timeout_seconds: single immediate check. With timeout_seconds: blocks until response or timeout (max recommended: 110s). Response is cleared from Obsidian after reading (one-shot).'
capability_name: request_poll
category: notifications
op: op.wb.request_poll
schema_version: wb-capability/v1
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
