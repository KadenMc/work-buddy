---
name: Truth Claim Confirm
kind: capability
description: Ask the human to review one server-composed claim and its active receipts, then confirm only that exact content.
capability_name: truth_claim_confirm
category: truth
op: op.wb.truth_claim_confirm
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  claim_id:
    type: str
    description: Proposed claim to render and confirm.
    required: true
  gesture_kind:
    type: str
    description: Exact confirmation decision, normally confirm. Quarantined-only support requires confirm_quarantined_support.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
consent_operations:
- truth.claim_confirm
tags:
- truth
- claim
- confirm
- consent
aliases:
- confirm truth claim
- approve exact fact
- ratify claim receipt
- human claim review
- attest proposition
parents:
- truth
---
