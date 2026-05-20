---
name: Docs Query
kind: capability
description: '[Legacy] Search knowledge units. Use agent_docs instead.'
capability_name: docs_query
category: context
op: op.wb.docs_query
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    required: false
  category:
    type: str
    required: false
  depth:
    type: str
    required: false
  top_n:
    type: int
    required: false
invokes:
- agent_docs
tags:
- context
- docs
- query
aliases:
- legacy knowledge query
- old docs query
- legacy search knowledge
- deprecated knowledge query
parents:
- context
---
