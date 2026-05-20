---
name: Email Close
kind: capability
description: 'Mark an email cluster as not actionable — newsletters, automated notifications, etc. Advisory only: dismisses the Thread without touching the underlying mailbox (Thunderbird bridge is read-first in v1).'
capability_name: email_close
category: email
op: op.wb.email_close
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Email-cluster sub-thread to close
    required: true
  reason:
    type: str
    description: Optional dismissal reason for the audit log
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- email
- close
aliases:
- close email cluster
- dismiss emails
- drop email group
- skip email cluster
parents:
- email
---
