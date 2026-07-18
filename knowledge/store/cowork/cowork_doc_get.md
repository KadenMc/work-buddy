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
