---
name: Truth Claim Propose
kind: capability
description: Propose a profile-valid claim and atomically attach supporting spans and one optional derivation.
capability_name: truth_claim_propose
category: truth
op: op.wb.truth_claim_propose
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  proposition:
    type: str
    description: Exact human-readable proposition being proposed.
    required: true
  claim_kind:
    type: str
    description: Profile-allowed claim kind.
    required: true
  producer_model:
    type: str
    description: Required model claim for the agent authoring this proposal. It must match a non-placeholder session-manifest model; otherwise it is durably labeled caller_asserted, not authenticated.
    required: true
  structured:
    type: dict
    description: Optional structured claim fields validated by the profile.
    required: false
  scope:
    type: str
    description: Claim scope. Default store.
    required: false
  valid_from:
    type: str
    description: Optional ISO 8601 valid-time start.
    required: false
  valid_to:
    type: str
    description: Optional ISO 8601 valid-time end.
    required: false
  confidence_extraction:
    type: float
    description: Optional extraction confidence from 0 through 1.
    required: false
  meta:
    type: dict
    description: Optional claim metadata. Producer identity cannot be overridden.
    required: false
  support_span_ids:
    type: list
    description: Evidence span ids to attach as active support.
    required: false
  derivation:
    type: dict
    description: Optional method, premises, confidence, and rationale mapping.
    required: false
  producer_call_id:
    type: str
    description: Optional durable model call identifier.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- claim
- propose
- derivation
aliases:
- propose truth claim
- add candidate fact
- submit evidence claim
- record unconfirmed claim
- suggest scoped truth
parents:
- truth
---
