---
name: Workflow Update
kind: capability
description: Update an existing workflow unit. Only provided fields change; omitted fields preserved. 'steps' and 'step_instructions' replace/merge rather than patch individual entries — read the current value, mutate, and pass the whole structure back.
capability_name: workflow_update
category: context
op: op.wb.workflow_update
schema_version: wb-capability/v1
parameters:
  path:
    type: str
    description: Path of workflow to update
    required: true
  name:
    type: str
    description: New human-readable name
    required: false
  description:
    type: str
    description: New one-line summary
    required: false
  workflow_name:
    type: str
    description: New registry slug
    required: false
  steps:
    type: str
    description: JSON array replacing the DAG. Callers should read the current value via agent_docs, mutate, and pass back.
    required: false
  step_instructions:
    type: str
    description: JSON object merged into step_instructions. Keys present in the new dict overwrite; keys absent are preserved. Pass the whole dict to replace cleanly.
    required: false
  execution:
    type: str
    description: New default execution policy
    required: false
  allow_override:
    type: bool
    description: New allow_override flag
    required: false
  content_full:
    type: str
    description: New workflow-level content
    required: false
  content_summary:
    type: str
    description: New summary
    required: false
  command:
    type: str
    description: New slash command name
    required: false
  parents:
    type: str
    description: New comma-separated parent paths (replaces)
    required: false
  tags:
    type: str
    description: New comma-separated tags (replaces)
    required: false
  aliases:
    type: str
    description: New comma-separated aliases (replaces)
    required: false
  dev_notes:
    type: str
    description: New dev-mode-only notes. Pass an empty string to clear.
    required: false
  params_schema:
    type: str
    description: New params schema (JSON object). Replaces existing schema entirely; pass an empty dict to clear.
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- workflow
- update
aliases:
- update workflow
- edit workflow DAG
- modify workflow steps
- change workflow
- patch workflow
- edit step instructions
parents:
- context
---
