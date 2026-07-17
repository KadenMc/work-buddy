---
name: Truth Claim Reject
kind: capability
description: Ask the human to reject one exact claim as plain noise, false content, or a preference correction.
capability_name: truth_claim_reject
category: truth
op: op.wb.truth_claim_reject
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  claim_id:
    type: str
    description: Proposed claim to render and reject.
    required: true
  reason_class:
    type: str
    description: reject_plain, reject_as_false, or reject_as_preference.
    required: true
  result_proposition:
    type: str
    description: Preference proposition offered for reject_as_preference.
    required: false
  result_structured:
    type: dict
    description: Optional structured fields for the offered preference claim.
    required: false
  support_span_ids:
    type: list
    description: Evidence spans supporting a false or preference result claim.
    required: false
  derivation:
    type: dict
    description: Optional derivation for the false or preference result claim.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
consent_operations:
- truth.claim_reject
tags:
- truth
- claim
- reject
- consent
aliases:
- reject truth claim
- mark claim false
- decline proposed fact
- record preference rejection
- dismiss claim proposal
parents:
- truth
---
