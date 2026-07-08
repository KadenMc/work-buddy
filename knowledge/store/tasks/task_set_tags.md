---
name: Task Set Tags
kind: capability
description: 'Replace the user-modifiable tags on an existing task''s master-list line. Manages free-form namespace tags (e.g. #admin/uhn), project tags (#projects/<slug>/...), and opt-in prefixes (#ns/..., #task/...). Pass the complete desired list; anything missing is removed. Preserved: #todo, #tasker/* (mirrors store-owned state — mutate via task_change_state), #wb/todo, #wb/done, wikilinks, 🆔, plugin emojis. Project slugs are validated against the project registry — unknown slugs raise ValueError.'
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
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.update_task
- obsidian.write_file
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
requires:
- obsidian
---
