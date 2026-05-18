---
name: Ir Index
kind: capability
description: Build or check the IR search index. Run 'build' to (re)encode dense vectors for indexed documents; 'status' returns per-source counts including dense_eligible_docs (how many docs CAN be encoded) and pending_eligible (real backlog — NOT doc_count vs vector_count, which is misleading because sources like conversation intentionally leave dense_text empty for tool-only spans).
capability_name: ir_index
category: context
parameters:
  action:
    type: str
    description: '''build'' (default) or ''status'''
    required: false
  source:
    type: str
    description: 'Source to index: ''conversation'' (default)'
    required: false
  days:
    type: int
    description: Index sessions from last N days (default 30)
    required: false
  force:
    type: bool
    description: Rebuild from scratch (default False)
    required: false
tags:
- context
- ir
- index
aliases:
- build index
- index conversations
- conversation index
- search index status
- reindex
parents:
- context
- context
---
