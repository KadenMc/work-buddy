---
name: Llm Submit
kind: capability
description: Asynchronously submit an llm_call for background execution. Returns immediately with an operation_id; the sidecar's retry sweep invokes llm_call with your params and messages the originating session on completion. Use when local inference latency (tens of seconds) would block the caller unnecessarily. For synchronous bounded calls use llm_call. Cloud tier calls are already fast — no point submitting them; profile is therefore required.
capability_name: llm_submit
category: llm
op: op.wb.llm_submit
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
  profile:
    type: str
    description: Named local/remote profile (e.g. 'local_general'). Required — submits are for local profiles only.
    required: true
  output_schema:
    type: dict|str
    description: JSON Schema for structured output. Pass a dict for inline schemas, or a string name to load from work_buddy/llm/schemas/<name>.json. Omit for freeform.
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
    description: "Local-inference admission priority for the broker: 'interactive', 'workflow' (default), or 'background'. Background submits should pass 'background' so they yield to interactive work on the same LM Studio profile."
    required: false
auto_retry: false
tags:
- llm
- submit
aliases:
- async llm
- background llm
- queue llm call
- submit llm
- defer llm
- fire and forget
- autodream
- background inference
parents:
- status
---
