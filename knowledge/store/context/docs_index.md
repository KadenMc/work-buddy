---
name: Docs Index
kind: skill
description: '[Legacy] Build IR index. Use agent_docs_rebuild instead.'
category: context
op: op.wb.docs_index
schema_version: wb-skill/v1
parameters:
  force:
    type: bool
    required: false
invokes:
- agent_docs_rebuild
skill_name: docs_index
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
