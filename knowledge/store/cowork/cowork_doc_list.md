---
name: Cowork Doc List
kind: capability
description: List registered cowork docs in a scope with drift and open-proposal counts.
capability_name: cowork_doc_list
category: cowork
op: op.wb.cowork_doc_list
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  profile:
    type: str
    description: Optional profile filter. One store carries one profile, so a value that does not match the scope yields an empty list.
    required: false
mutates_state: false
retry_policy: manual
auto_retry: false
tags:
- cowork
- doc
- list
aliases:
- list cowork docs
- list documents
- cowork document list
- open documents in scope
parents:
- cowork
---
