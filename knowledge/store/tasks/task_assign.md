---
name: Task Assign
kind: capability
description: Claim a native task for the current session and return structured task plus current Co-work knowledge context.
capability_name: task_assign
category: tasks
op: op.wb.task_assign
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
retry_policy: verify_first
tags:
- tasks
- task
- assign
aliases:
- assign task
- claim task
- work on task
- start task
parents:
- tasks
requires: []
---

Returns native task/document context and a mutation receipt. It never returns a
Markdown note path or task line.
