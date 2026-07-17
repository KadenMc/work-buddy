---
name: Truth Query
kind: capability
description: Query confirmed claims by current or historical belief time, or inspect active conflicts and review findings.
capability_name: truth_query
category: truth
op: op.wb.truth_query
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  view:
    type: str
    description: current, as-of, conflicts, or needs-review. Default current.
    required: false
  belief_at:
    type: str
    description: Historical belief-time cutoff. Required for as-of.
    required: false
  valid_at:
    type: str
    description: Optional domain valid-time filter.
    required: false
  scope:
    type: str
    description: Optional claim scope filter.
    required: false
  claim_kind:
    type: str
    description: Optional claim kind filter.
    required: false
  include_needs_review:
    type: bool
    description: Include confirmed claims with an active review overlay.
    required: false
  claim_id:
    type: str
    description: Optional claim filter for the conflicts view.
    required: false
tags:
- truth
- query
- belief-time
- review
aliases:
- query truth claims
- list confirmed facts
- inspect claim history
- find truth conflicts
- show claims needing review
parents:
- truth
---
