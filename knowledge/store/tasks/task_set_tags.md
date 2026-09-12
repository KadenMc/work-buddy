---
name: Task Set Tags
kind: capability
description: Replace a native task's complete structured tag and namespace set. Namespace spelling is independent of registered project associations; this operation does not change project links.
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
    description: Complete replacement set, with every supplied entry stored as a namespace (no leading '#'). Existing ordinary tags are also replaced; use the dashboard's separate tag/namespace fields when that distinction must be preserved. A projects/ prefix has no special project-link meaning. An empty list strips tags while preserving project associations.
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
parents:
- tasks
requires: []
---
