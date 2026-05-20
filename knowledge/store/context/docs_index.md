---
name: Docs Index
kind: capability
description: '[Legacy] Build IR index. Use agent_docs_rebuild instead.'
capability_name: docs_index
category: context
op: op.wb.docs_index
schema_version: wb-capability/v1
parameters:
  force:
    type: bool
    required: false
invokes:
- agent_docs_rebuild
tags:
- context
- docs
- index
aliases:
- legacy build index
- old index rebuild
- deprecated docs index
- legacy docs indexing
- old knowledge rebuild
parents:
- context
---
