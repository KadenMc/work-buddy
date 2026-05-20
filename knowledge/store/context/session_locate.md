---
name: Session Locate
kind: capability
description: Jump from a context_search hit to the relevant conversation page. Takes a span_index from search result metadata and returns messages centered on that chunk.
capability_name: session_locate
category: context
op: op.wb.session_locate
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or partial (8-char) session UUID
    required: true
  span_index:
    type: int
    description: IR span index from context_search result metadata
    required: true
tags:
- context
- session
- locate
aliases:
- find chunk in session
- locate search hit
- span to messages
- search result context
parents:
- context
---
