---
name: Truth Claim Supersede
kind: capability
description: Create or select a successor claim and atomically attach its support, derivation, and typed supersession link.
capability_name: truth_claim_supersede
category: truth
op: op.wb.truth_claim_supersede
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  predecessor_claim_id:
    type: str
    description: Existing claim the successor changes or replaces.
    required: true
  reason:
    type: str
    description: Typed reason such as updated, corrected, refined, valid_time_closed, source_retracted, or preference_changed.
    required: true
  producer_model:
    type: str
    description: Required model claim for the agent authoring this change. It must match a non-placeholder session-manifest model; otherwise newly written claim metadata is durably labeled caller_asserted, not authenticated.
    required: true
  successor_claim_id:
    type: str
    description: Existing successor id. Omit to propose a successor from the payload fields.
    required: false
  proposition:
    type: str
    description: New successor proposition when successor_claim_id is omitted.
    required: false
  claim_kind:
    type: str
    description: New successor claim kind when successor_claim_id is omitted.
    required: false
  structured:
    type: dict
    description: Optional structured successor fields.
    required: false
  scope:
    type: str
    description: Successor scope. Default store.
    required: false
  valid_from:
    type: str
    description: Optional successor valid-time start.
    required: false
  valid_to:
    type: str
    description: Optional successor valid-time end.
    required: false
  confidence_extraction:
    type: float
    description: Optional extraction confidence from 0 through 1.
    required: false
  meta:
    type: dict
    description: Optional successor metadata. Producer identity cannot be overridden.
    required: false
  support_span_ids:
    type: list
    description: Evidence span ids to attach to the successor.
    required: false
  derivation:
    type: dict
    description: Optional method, premises, confidence, and rationale mapping.
    required: false
  note:
    type: str
    description: Optional explanation stored on the supersession relation.
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
- supersede
- revision
aliases:
- supersede truth claim
- replace confirmed fact
- revise stored claim
- create claim successor
- correct scoped truth
parents:
- truth
---
