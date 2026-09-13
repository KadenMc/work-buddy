---
name: Docs Query
kind: skill
description: '[Legacy] Search knowledge units. Use agent_docs instead.'
category: context
op: op.wb.docs_query
schema_version: wb-skill/v1
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
skill_name: docs_query
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
