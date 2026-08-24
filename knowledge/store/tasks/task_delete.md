---
name: Task Delete
kind: capability
description: Soft-delete a native task into Trash. Task history and Co-work knowledge remain recoverable.
capability_name: task_delete
category: tasks
op: op.wb.task_delete
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
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
retry_policy: manual
consent_operations:
- tasks.delete_task
tags:
- tasks
- task
- delete
aliases:
- remove task
- delete todo
- destroy task
- permanently delete task
- get rid of task
- erase todo
- drop task
parents:
- tasks
requires: []
---

Delete is reversible. Restore keeps retired document history and creates a new
active Co-work binding when the prior binding was retired.
