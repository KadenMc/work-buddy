---
name: Consent Grant
kind: capability
description: 'LOW-LEVEL: Direct consent grant for deferred resolution ONLY. Do NOT use this to bypass the consent flow — use consent_request instead, which notifies the user and waits for their approval. This capability exists for: (1) manual resolution after a consent_request timeout when the user later approves out-of-band, (2) programmatic grants from surface callbacks. All grants are session-scoped. Modes: ''always'' (24h), ''temporary'' (TTL-based), ''once'' (single-use).'
capability_name: consent_grant
category: consent
op: op.wb.consent_grant
schema_version: wb-capability/v1
parameters:
  operation:
    type: str
    description: Operation identifier from the consent_required response
    required: true
  mode:
    type: str
    description: 'Grant mode: ''always'', ''temporary'', or ''once'''
    required: true
  ttl_minutes:
    type: int
    description: TTL in minutes (required for 'temporary' mode)
    required: false
mutates_state: true
retry_policy: manual
tags:
- consent
- grant
aliases:
- consent
- permission
- approve
- allow
- grant consent
- give permission
parents:
- notifications/consent
---
