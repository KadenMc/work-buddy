---
name: Memory Read
kind: capability
description: 'Read from personal memory (Hindsight). No LLM cost. Modes: ''search'' (default) — semantic + keyword recall, use descriptive topic phrases with specific entity names for best results; ''model'' — fetch a mental model by ID; ''recent'' — list latest memories.'
capability_name: memory_read
category: memory
op: op.wb.memory_read
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Descriptive topic phrase for search mode. Use specific terminology and entity names (e.g. named work-pattern vocabulary) rather than generic labels like 'blindspots'. Ignored for model/recent modes.
    required: false
  mode:
    type: str
    description: search (default) — semantic + keyword recall. model — fetch a mental model (self-profile, work-patterns, blindspots, preferences, current-constraints). recent — list N most recent memories.
    required: false
  model_id:
    type: str
    description: 'Mental model ID for mode=model (default: self-profile)'
    required: false
  limit:
    type: int
    description: Max memories for mode=recent (default 20)
    required: false
  budget:
    type: str
    description: 'Retrieval depth for mode=search: low (fast, default), mid, high'
    required: false
tags:
- memory
- read
aliases:
- what do I remember
- recall memory
- search hindsight
- retrieve memories
- personal memory search
- remember what I told you
- recall my preferences
- search personal memory
parents:
- memory
requires:
- hindsight
---
