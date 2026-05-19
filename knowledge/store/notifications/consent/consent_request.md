---
name: Consent Request
kind: capability
description: 'One-call consent flow: create a consent request, deliver to all available surfaces, and optionally poll + auto-resolve. The modal shows Allow always / Allow for N min / Allow once / Deny options. Without timeout_seconds: non-blocking (returns request_id for later polling via request_poll + consent_request_resolve). With timeout_seconds: blocks until user responds, then auto-resolves (writes the grant on approval, returns denial on deny). Max recommended timeout: 110s to stay within MCP limits.'
capability_name: consent_request
category: consent
op: op.wb.consent_request
schema_version: wb-capability/v1
parameters:
  operation:
    type: str
    description: Operation identifier (same as @requires_consent keys)
    required: true
  reason:
    type: str
    description: Human-readable explanation
    required: true
  risk:
    type: str
    description: low, moderate, or high
    required: false
  default_ttl:
    type: int
    description: Default TTL in minutes for temporary grants
    required: false
  requester:
    type: str
    description: Who is requesting (e.g., sidecar:cron_cleanup)
    required: false
  context:
    type: dict
    description: Optional metadata shown in the modal
    required: false
  callback:
    type: dict
    description: 'Dispatch on approval: {capability, params}'
    required: false
  callback_session_id:
    type: str
    description: Resume this session on approval
    required: false
  timeout_seconds:
    type: int
    description: 'Poll timeout. Omit for non-blocking. Max recommended: 110s'
    required: false
  interval_seconds:
    type: int
    description: 'Seconds between polls (default: 3)'
    required: false
  surfaces:
    type: list[str]
    description: 'Target surface names (e.g., [''dashboard'']). Default: all available'
    required: false
mutates_state: true
retry_policy: manual
tags:
- consent
- request
aliases:
- consent
- permission
- approve operation
- ask consent
- request consent
- consent modal
parents:
- notifications/consent
---
