---
name: Consent Request Resolve
kind: capability
description: Approve or deny a pending consent request. If approved, writes the grant and dispatches the callback (session resume or messaging).
capability_name: consent_request_resolve
category: consent
parameters:
  request_id:
    type: str
    description: The request ID to resolve
    required: true
  approved:
    type: bool
    description: True to approve, False to deny
    required: true
  mode:
    type: str
    description: 'Grant mode if approved: ''always'', ''temporary'', or ''once'''
    required: false
  ttl_minutes:
    type: int
    description: TTL in minutes (for 'temporary' mode)
    required: false
mutates_state: true
retry_policy: manual
tags:
- consent
- request
- resolve
aliases:
- approve consent
- deny consent
- resolve request
- handle pending consent
- grant or deny operation
- respond to consent request
- decide on permission
parents:
- notifications/consent
- notifications/consent
---
