---
name: Cowork Doc Get
kind: capability
description: Read one cowork doc's content-meta, open proposals, expressions, hashes, and drift.
capability_name: cowork_doc_get
category: cowork
op: op.wb.cowork_doc_get
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
mutates_state: false
retry_policy: manual
auto_retry: false
tags:
- cowork
- doc
- get
aliases:
- read cowork doc
- open cowork document
- get document review layer
- inspect cowork doc
parents:
- cowork
---

`cowork_doc_get` is the read-only source shared by the Review rail and editor
annotation projection. It assembles document metadata, proposals, expressions,
claims, and provenance from one explicit SQLite read snapshot, so the response
cannot combine ledger states from different instants.

Proposal, expression, and provenance spans include complete `exact`, `prefix`,
and `suffix` quote anchors. For an expression whose claim reference resolves to
a local claim (including a canonical same-store `wb-truth:` URI), the expression
entry includes the claim's current `claim_status` and `claim_kind`. External,
malformed, or otherwise unresolvable references return `null` for that metadata
instead of promoting an unknown claim or failing the document read.

Provenance classification is conservative. `ai_confirmed` requires the durable
combination of agent-authored proposal provenance and a human-accepted
replacement span. Marking a passage that already exists does not infer that an
agent wrote the prose; absent stronger durable evidence, its authorship remains
unknown.
