---
name: Memory Reflect
kind: capability
description: 'LLM-powered reasoning over memories. CONSENT-GATED: triggers a server-side LLM call against your Anthropic API key (~1-3K tokens per call). Use memory_read for free retrieval first.'
capability_name: memory_reflect
category: memory
op: op.wb.memory_reflect
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Question to reason about using memory
    required: true
  budget:
    type: str
    description: 'Retrieval depth: low (default), mid, high'
    required: false
consent_operations:
- memory_reflect
tags:
- memory
- reflect
aliases:
- reason about memories
- analyze my memories
- LLM reflection on memory
- think about memories
- memory synthesis
- synthesize from memory
parents:
- memory
requires:
- hindsight
---
