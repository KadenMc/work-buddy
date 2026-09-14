---
name: Task Update Description
kind: skill
description: Update a native task description with revision checking and an idempotent mutation receipt.
category: tasks
op: op.wb.task_update_description
schema_version: wb-skill/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
    required: true
  new_description:
    type: str
    description: New description text. Single line; whitespace is collapsed.
    required: true
  expected_revision:
    type: int
    description: Current task revision for compare-and-swap; the gateway pins it when omitted.
    required: false
  client_mutation_id:
    type: str
    description: Optional stable idempotency key.
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.update_task
skill_name: task_update_description
tags:
- tasks
- task
- update
- description
aliases:
- rename task
- rewrite task
- edit task description
- change task text
- update task wording
- rephrase task
- rewrite task text
parents:
- tasks
---
