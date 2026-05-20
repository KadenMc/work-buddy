---
name: Consent Revoke
kind: capability
description: Revoke a previously granted consent for an operation.
capability_name: consent_revoke
category: consent
op: op.wb.consent_revoke
schema_version: wb-capability/v1
parameters:
  operation:
    type: str
    description: Operation identifier to revoke
    required: true
mutates_state: true
retry_policy: manual
tags:
- consent
- revoke
aliases:
- revoke
- deny
- remove consent
- block
parents:
- notifications/consent
---
