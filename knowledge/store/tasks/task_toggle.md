---
name: Task Toggle
kind: capability
description: Complete, reopen, or toggle a native task atomically. Preserves an optional historical done date and the prior attention state for an idempotent reopen.
capability_name: task_toggle
category: tasks
op: op.wb.task_toggle
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
    required: true
  done:
    type: bool
    description: True=complete, False=incomplete, omit=toggle
    required: false
  done_date:
    type: str
    description: ISO YYYY-MM-DD to stamp as the completion date when marking done. Defaults to today. Use for retroactive completion (e.g. the landing-commit date a completeness check uncovered). Ignored when reopening.
    required: false
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
- tasks.toggle_task
tags:
- tasks
- task
- toggle
aliases:
- finish task
- done task
- complete todo
- mark done
- uncomplete task
- reopen task
parents:
- tasks
requires: []
---
