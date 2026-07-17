---
name: Truth Span Mark
kind: capability
description: Resolve a quote selector against captured evidence and append one immutable evidence span for claim support.
capability_name: truth_span_mark
category: truth
op: op.wb.truth_span_mark
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  evidence_id:
    type: str
    description: Evidence record whose text contains the span.
    required: true
  selector:
    type: dict
    description: Composite selector with exact and optional prefix, suffix, start, and end fields.
    required: true
  producer_model:
    type: str
    description: Required model claim for the agent authoring this write. It must match a non-placeholder session-manifest model; otherwise it is treated as caller_asserted, not authenticated.
    required: true
  snapshot_text:
    type: str
    description: Matching source text for hash-only evidence.
    required: false
  author_kind:
    type: str
    description: Explicit human, agent_run, or unknown authorship for mixed evidence.
    required: false
  author_ref:
    type: str
    description: Durable author reference when author_kind needs one.
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
- evidence
- span
- quote
aliases:
- mark evidence span
- anchor source quote
- create claim receipt
- select supporting text
- add quote anchor
parents:
- truth
---
