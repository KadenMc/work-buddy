---
name: Request Send
kind: capability
description: 'Create a request, deliver to all available surfaces, and optionally poll for the user''s response. Supports choice, boolean, freeform, and range response types. Without timeout_seconds: non-blocking (returns immediately, use request_poll later). With timeout_seconds: blocks until response or timeout (max recommended: 110s to stay within MCP call limits).'
capability_name: request_send
category: notifications
parameters:
  title:
    type: str
    description: Request title
    required: true
  body:
    type: str
    description: Request body/explanation
    required: false
  response_type:
    type: str
    description: choice, boolean, freeform, range, custom
    required: false
  choices:
    type: list
    description: 'For choice type: [{key, label, description}]'
    required: false
  number_range:
    type: dict
    description: 'For range type: {min, max, step}'
    required: false
  custom_template:
    type: dict
    description: 'For custom type: surface-specific rendering data (e.g., {type: ''triage_clarify'', presentation: ...})'
    required: false
  source:
    type: str
    description: Who is sending
    required: false
  source_type:
    type: str
    description: agent or programmatic
    required: false
  priority:
    type: str
    description: low, normal, high, urgent
    required: false
  callback:
    type: dict
    description: 'Dispatch on response: {capability, params}'
    required: false
  callback_session_id:
    type: str
    description: Resume this session on response
    required: false
  tags:
    type: list
    description: Tags for filtering
    required: false
  surfaces:
    type: list
    description: 'Target surfaces (e.g. [''telegram'']). Default: all available.'
    required: false
  timeout_seconds:
    type: int
    description: 'Poll timeout. Omit for non-blocking. Max recommended: 110s'
    required: false
  interval_seconds:
    type: int
    description: 'Seconds between polls (default: 3)'
    required: false
  expandable:
    type: bool
    description: None=auto-detect, True=rich dashboard view, False=toast-only.
    required: false
mutates_state: true
retry_policy: manual
tags:
- notifications
- request
- send
aliases:
- ask user
- prompt user
- request response
- user input
- show modal
parents:
- notifications
- notifications
---
