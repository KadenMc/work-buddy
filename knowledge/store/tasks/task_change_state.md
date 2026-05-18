---
name: Task Change State
kind: capability
description: 'Update task metadata: state (not completion), urgency, due date. Cannot set state=''done'' — use task_toggle for completion.'
capability_name: task_change_state
category: tasks
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: false
  description_match:
    type: str
    description: Description substring (fallback)
    required: false
  state:
    type: str
    description: 'New state: inbox, mit, focused, snoozed. NOT done — use task_toggle.'
    required: false
  urgency:
    type: str
    description: 'New urgency: low, medium, high'
    required: false
  due_date:
    type: str
    description: Due date as YYYY-MM-DD
    required: false
mutates_state: true
retry_policy: verify_first
tags:
- tasks
- task
- change
- state
aliases:
- change task state
- mit this task
- focus a task
- snooze a task
- update task urgency
- change due date
- promote task to MIT
- move task to inbox
parents:
- tasks
- tasks
requires:
- obsidian
---
