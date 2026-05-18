---
name: Notification Send
kind: capability
description: Send a fire-and-forget notification to the user via all available surfaces (Obsidian, Telegram if enabled). No response expected. Optionally target specific surfaces.
capability_name: notification_send
category: notifications
parameters:
  title:
    type: str
    description: Notification title
    required: true
  body:
    type: str
    description: Notification body
    required: false
  priority:
    type: str
    description: low, normal, high, urgent
    required: false
  source:
    type: str
    description: Who is sending
    required: false
  tags:
    type: list
    description: Tags for filtering
    required: false
  surfaces:
    type: list
    description: 'Target surfaces (e.g. [''telegram'']). Default: all available.'
    required: false
  expandable:
    type: bool
    description: None=auto-detect, True=rich dashboard view, False=toast-only.
    required: false
mutates_state: true
retry_policy: manual
tags:
- notifications
- notification
- send
aliases:
- notify
- alert
- message user
- send notification
parents:
- notifications
- notifications
---
