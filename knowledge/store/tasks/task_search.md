---
name: Task Search
kind: capability
description: Search tasks by description text via the SQLite store. Bridge-independent — works even when Obsidian isn't running. Returns task records (full task_metadata rows) ordered most-recently-updated first. For full-text search over task NOTE bodies (the [[uuid|📓]]-linked detail files), use context_search(source='task_note') instead — that's hybrid retrieval over note content; this is exact-text search over the line description.
capability_name: task_search
category: tasks
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
- tasks
---
