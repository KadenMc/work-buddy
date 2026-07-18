---
name: Cowork Doc Propose Edit
kind: capability
description: Propose one or more quote-anchored tracked edits on a cowork doc as an open proposal. Normal consent weight, no decision authority.
capability_name: cowork_doc_propose_edit
category: cowork
op: op.wb.cowork_doc_propose_edit
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
  hunks:
    type: list
    description: "One or more edits, each a mapping of {quote_anchor:{exact,prefix,suffix}, replacement, node_id_hint?}. The quote anchor is resolved by the kernel anchor resolver, never by node id."
    required: true
  rationale:
    type: str
    description: Full reviewer-facing reason for the edit.
    required: true
  tldr:
    type: str
    description: One-line summary shown on the margin card.
    required: true
  producer_model:
    type: str
    description: Required model claim for the authoring agent. It must match a non-placeholder session-manifest model, otherwise it is durably labeled caller_asserted.
    required: true
  base_doc_sha256:
    type: str
    description: Doc snapshot hash the proposal was drafted against. A proposal against a stale base cannot mint or refresh anything. Defaults to the current document content when omitted.
    required: false
  claim_refs:
    type: list
    description: "Optional. A list of {claim, role} where claim is a local claim id or a wb-truth URI and role is one of quote, paraphrase, summary, or instantiation, defaulting to instantiation. Accepting mints one expression per ref carrying that ref's role."
    required: false
  meta:
    type: dict
    description: Optional metadata accepted for forward compatibility. Producer identity cannot be overridden.
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
- propose
- edit
aliases:
- propose cowork edit
- suggest document edit
- tracked change proposal
- draft an edit on a cowork doc
parents:
- cowork
---
