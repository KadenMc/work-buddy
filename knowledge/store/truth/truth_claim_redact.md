---
name: Truth Claim Redact
kind: capability
description: Ask the human to destroy readable content from one exact claim while retaining identity, hashes, links, status, and audit history.
capability_name: truth_claim_redact
category: truth
op: op.wb.truth_claim_redact
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  claim_id:
    type: str
    description: Claim whose readable content will be destroyed.
    required: true
  reason:
    type: str
    description: Redaction reason. Default privacy.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
consent_operations:
- truth.claim_redact
tags:
- truth
- claim
- redact
- privacy
aliases:
- redact truth claim
- remove claim text
- erase sensitive proposition
- tombstone claim content
- privacy redact fact
parents:
- truth
---
