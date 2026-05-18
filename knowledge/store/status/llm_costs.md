---
name: Llm Costs
kind: capability
description: Check LLM token usage, costs, and breakdown for this session. Shows per-task costs, per-model costs, cache hit rates, and top callers.
capability_name: llm_costs
category: status
parameters:
  breakdown:
    type: bool
    description: 'Show per-task and per-model breakdown (default: false)'
    required: false
tags:
- status
- llm
- costs
aliases:
- llm costs
- token usage
- api costs
- how much has haiku cost
- llm spending
parents:
- status
- status
---
