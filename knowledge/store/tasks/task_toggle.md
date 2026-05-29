---
name: Task Toggle
kind: capability
description: Mark a task complete, incomplete, or toggle. Handles checkbox, done date, and store state atomically. Use done=true to complete, done=false to reopen, omit to toggle. Consent-gated.
capability_name: task_toggle
category: tasks
op: op.wb.task_toggle
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
  done:
    type: bool
    description: True=complete, False=incomplete, omit=toggle
    required: false
  done_date:
    type: str
    description: ISO YYYY-MM-DD to stamp as the completion date when marking done. Defaults to today. Use for retroactive completion (e.g. the landing-commit date a completeness check uncovered). Ignored when reopening.
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.toggle_task
- obsidian.write_file
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
requires:
- obsidian
---
