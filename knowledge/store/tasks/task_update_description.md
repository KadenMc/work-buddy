---
name: Task Update Description
kind: capability
description: 'Rewrite the description text on a task line. Preserves checkbox, #todo, #projects/*, namespace tags, wikilinks, 🆔 + ID, plugin emojis (📅, ✅, urgency). Updates the store''s description column in lockstep. Use this instead of filesystem-direct edits — it routes through the same consent-aware, retry-aware path as the other mutations and avoids the read-modify-write race on the master task list.'
capability_name: task_update_description
category: tasks
op: op.wb.task_update_description
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
  new_description:
    type: str
    description: New description text. Single line; whitespace is collapsed.
    required: true
  file_path:
    type: str
    description: 'Vault-relative path. Default: tasks/master-task-list.md'
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.update_task
- obsidian.write_file
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
- rewrite task line
parents:
- tasks
requires:
- obsidian
---
