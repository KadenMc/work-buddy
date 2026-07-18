---
name: Cowork Doc Expression Mark
kind: capability
description: Propose linking an existing passage to an existing claim (an expression row), propose-weight and read-mostly in v1.
capability_name: cowork_doc_expression_mark
category: cowork
op: op.wb.cowork_doc_expression_mark
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  document_id:
    type: str
    description: Target cowork doc id.
    required: true
  span:
    type: dict
    description: "A mapping of {exact, prefix, suffix, node_id_hint?} for the passage."
    required: true
  claim_ref:
    type: str
    description: A wb-truth URI or a local claim id the passage expresses. It must resolve in this store.
    required: true
  role:
    type: str
    description: Required. One of quote, paraphrase, summary, or instantiation, stating how the passage expresses the claim.
    required: true
  producer_model:
    type: str
    description: Required model claim for the authoring agent. It must match a non-placeholder session-manifest model, otherwise it is durably labeled caller_asserted.
    required: true
  producer_call_id:
    type: str
    description: Optional durable model call identifier.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- cowork
- doc
- expression
- mark
aliases:
- mark a cowork expression
- link passage to claim
- record an expression
- say this passage expresses this claim
parents:
- cowork
---
