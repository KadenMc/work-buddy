---
name: Task Delete
kind: capability
description: 'Permanently delete a task: remove line, note file, and store record. Consent-gated.'
capability_name: task_delete
category: tasks
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
mutates_state: true
retry_policy: manual
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
- tasks
requires:
- obsidian
---
