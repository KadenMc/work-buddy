---
name: Journal Prompt Generation Context
kind: skill
description: Retrieve the frozen seed and prompt for one lease-bound Journal generation worker. This internal skill is disclosure-accounted and rejects every other agent session.
category: journal
op: op.wb.journal_prompt_generation_context
schema_version: wb-skill/v1
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
skill_name: journal_prompt_generation_context
tags:
- journal
- internal
- generation
parents:
- journal
---

This skill is only for the detached Journal prompt worker launched by an
explicit Generate action. It returns the immutable human seed separately from
the prompt definition and records the exact disclosure before returning it.
The gateway injects the execution session; callers cannot select it.
