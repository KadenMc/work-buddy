---
name: Journal Prompt Generation Complete
kind: skill
description: Retain one lease-bound Journal worker result as identified agent output and bind it as an unreviewed prompt variant.
category: journal
op: op.wb.journal_prompt_generation_complete
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
  result_text:
    type: str
    description: Exact plain-text generated result. The seed must not be repeated unless the prompt calls for it.
    required: true
mutates_state: true
retry_policy: verify_first
skill_name: journal_prompt_generation_complete
tags:
- journal
- internal
- generation
parents:
- journal
---

The output call causally acknowledges the worker's recorded input disclosure,
commits an `agent_output` Source under the injected execution identity, and
creates one unreviewed Journal variant. It never accepts a caller-selected
author or worker session.
