---
name: Session Search
kind: capability
description: Hybrid search within a single session. Uses IR (keyword/semantic/substring) scoped to the session, then resolves chunk hits to message-level results via the span map.
capability_name: session_search
category: context
parameters:
  session_id:
    type: str
    description: Full or partial session UUID
    required: true
  query:
    type: str
    description: Search query
    required: true
  method:
    type: str
    description: 'Search method: ''substring'', ''keyword'', ''semantic'', or ''keyword,semantic'' (default)'
    required: false
  top_k:
    type: int
    description: Max chunk hits to resolve (default 5)
    required: false
tags:
- context
- session
- search
aliases:
- search in session
- find in conversation
- session query
- semantic session search
parents:
- context
- context
---
