---
name: Journal Prompt Generation Context
kind: capability
description: Retrieve the frozen seed and prompt for one lease-bound Journal generation worker. This internal capability is disclosure-accounted and rejects every other agent session.
capability_name: journal_prompt_generation_context
category: journal
op: op.wb.journal_prompt_generation_context
schema_version: wb-capability/v1
parameters:
  request_id:
    type: str
    description: Exact generation request ID from the scoped worker brief.
    required: true
  lease_token:
    type: str
    description: Exact one-time lease capability from the scoped worker brief.
    required: true
mutates_state: true
retry_policy: verify_first
tags:
- journal
- internal
- generation
parents:
- journal
requires: []
---

This capability is only for the detached Journal prompt worker launched by an
explicit Generate action. It returns the immutable human seed separately from
the prompt definition and records the exact disclosure before returning it.
The gateway injects the execution session; callers cannot select it.
