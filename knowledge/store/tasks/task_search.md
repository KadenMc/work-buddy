---
name: Task Search
kind: capability
description: Search native task descriptions in TaskStore. For full-text knowledge search, use the task-note IR source, which indexes current Co-work document heads rather than Markdown files.
capability_name: task_search
category: tasks
op: op.wb.task_search
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: Substring to search for in task description text. Empty string returns nothing.
    required: true
  limit:
    type: int
    description: Max results (default 50)
    required: false
  include_archived:
    type: bool
    description: Include archived tasks (default False)
    required: false
  include_done:
    type: bool
    description: Include completed tasks (default True)
    required: false
tags:
- tasks
- task
- search
aliases:
- find task
- search tasks
- find a task by name
- look up task
- task by description
- tasks containing
- search task descriptions
parents:
- tasks
---
