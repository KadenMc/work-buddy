---
name: Cowork Doc Comment
kind: capability
description: Raise a quote-anchored flag (a proposal with no replacement) on a cowork doc.
capability_name: cowork_doc_comment
category: cowork
op: op.wb.cowork_doc_comment
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
  quote_anchor:
    type: dict
    description: "A mapping of {exact, prefix, suffix} resolved by the kernel anchor resolver."
    required: true
  body:
    type: str
    description: The flag text, the problem being raised.
    required: true
  tldr:
    type: str
    description: One-line summary for the margin card.
    required: true
  producer_model:
    type: str
    description: Required model claim for the authoring agent. It must match a non-placeholder session-manifest model, otherwise it is durably labeled caller_asserted.
    required: true
  base_doc_sha256:
    type: str
    description: Doc hash the flag was raised against. Defaults to the current document content when omitted.
    required: false
  claim_refs:
    type: list
    description: "Optional. A list of {claim, role} where claim is a local claim id or a wb-truth URI and role is one of quote, paraphrase, summary, or instantiation, defaulting to instantiation."
    required: false
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
- comment
- flag
aliases:
- raise a cowork flag
- comment on a document
- flag a passage
- annotate a cowork doc
parents:
- cowork
---
