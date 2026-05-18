---
name: Task Toggle
kind: capability
description: Mark a task complete, incomplete, or toggle. Handles checkbox, done date, and store state atomically. Use done=true to complete, done=false to reopen, omit to toggle. Consent-gated.
capability_name: task_toggle
category: tasks
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
  done:
    type: bool
    description: True=complete, False=incomplete, omit=toggle
    required: false
mutates_state: true
retry_policy: verify_first
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
- tasks
requires:
- obsidian
---
