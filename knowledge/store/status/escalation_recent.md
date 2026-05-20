---
name: Escalation Recent
kind: capability
description: Recent LLM-escalation observability records. Each record is one logical job (one LLMRunner.call OR one adapter-level escalation chain across multiple calls) with its full per-tier attempt list, final outcome, and trace correlation.
capability_name: escalation_recent
category: llm
op: op.wb.escalation_recent
schema_version: wb-capability/v1
parameters:
  limit:
    type: int
    description: Max records to return (newest first). Default 50.
    required: false
  trace_id:
    type: str
    description: Filter by exact trace_id correlation token.
    required: false
  final_outcome:
    type: str
    description: Filter by final_outcome ('success', 'backend_error', 'validation_failed', 'empty_content', 'exhausted').
    required: false
  source:
    type: str
    description: Filter by source ('llm_runner', 'journal_segmenter', 'verdict_call').
    required: false
  summary:
    type: bool
    description: When true, return aggregate counts (by source / outcome / final_tier, plus 'escalated_past_first') instead of records.
    required: false
tags:
- llm
- escalation
- recent
aliases:
- tier escalation
- escalation log
- what tier answered
- did this escalate
- fallback chain
- model fallback
- validation failed
- tier observability
parents:
- status
---
