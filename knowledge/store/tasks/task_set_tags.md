---
name: Task Set Tags
kind: capability
description: Replace a native task's complete structured tag and namespace set. Project slugs are validated against the project registry.
capability_name: task_set_tags
category: tasks
op: op.wb.task_set_tags
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
    required: true
  namespace_tags:
    type: list[str]
    description: Replacement list of tags (no leading '#'). Includes project tags (e.g. 'projects/work-buddy/systems/task-system') and free-form namespace tags. An empty list strips all user-modifiable tags.
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
- set
- tags
aliases:
- tag task
- retag task
- add namespace to task
- remove tag from task
- set task namespace
- set task project
- assign project to task
- retroactively tag with project
parents:
- tasks
requires: []
---
