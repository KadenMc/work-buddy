---
name: Llm Call
kind: capability
description: Make a single LLM API call (Tier 2 execution). Cheaper than spawning a full agent session. Supports freeform text or structured JSON output via output_schema (inline dict or named schema from work_buddy/llm/schemas/). Routes to Claude via 'tier' or to a local/remote OpenAI-compatible server (LM Studio, vLLM, Ollama) via 'profile'. Handles caching and cost tracking automatically.
capability_name: llm_call
category: llm
op: op.wb.llm_call
schema_version: wb-capability/v1
parameters:
  system:
    type: str
    description: System prompt
    required: true
  user:
    type: str
    description: User message content
    required: true
  output_schema:
    type: dict|str
    description: JSON Schema for structured output. Pass a dict for inline schemas, or a string name to load from work_buddy/llm/schemas/<name>.json. Omit for freeform text.
    required: false
  tier:
    type: str
    description: 'Cloud model tier: ''haiku'' (default if no profile given), ''sonnet'', or ''opus''. Mutually exclusive with ''profile''.'
    required: false
  profile:
    type: str
    description: Named local/remote profile (e.g. 'local_general') declared under llm.profiles in config. Routes through the profile's backend instead of Anthropic. Mutually exclusive with 'tier'.
    required: false
  max_tokens:
    type: int
    description: 'Max response tokens (default: 1024)'
    required: false
  temperature:
    type: float
    description: 'Sampling temperature (default: 0.0)'
    required: false
  cache_ttl_minutes:
    type: int
    description: Cache TTL in minutes. None=config default, 0=no cache.
    required: false
  priority:
    type: str
    description: "Local-inference admission priority for the broker: 'interactive', 'workflow' (default), or 'background'. Only applies to the local 'profile' path; ignored for cloud 'tier' (Anthropic isn't brokered). Lets background work yield to interactive work on the same LM Studio profile."
    required: false
tags:
- llm
- call
aliases:
- api call
- llm call
- claude api
- structured output
- haiku call
- simple reasoning
- cheap llm
- classify
- analyze
- local llm
- lm studio
- qwen
parents:
- status
---
