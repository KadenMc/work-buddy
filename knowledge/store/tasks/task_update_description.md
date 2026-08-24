---
name: Task Update Description
kind: capability
description: Update a native task description with revision checking and an idempotent mutation receipt.
capability_name: task_update_description
category: tasks
op: op.wb.task_update_description
schema_version: wb-capability/v1
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
requires: []
---
