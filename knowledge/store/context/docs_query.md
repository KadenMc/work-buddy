---
name: Docs Query
kind: capability
description: '[Legacy] Search knowledge units. Use agent_docs instead.'
capability_name: docs_query
category: context
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
- context
---
