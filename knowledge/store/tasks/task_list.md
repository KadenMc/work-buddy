---
name: Task List
kind: capability
description: List tasks from the SQLite store (bridge-independent — works even when Obsidian isn't running). With no arguments returns every live, non-archived, OPEN task, oldest-created first. The enumeration complement to task_search, which matches by text — use task_list to pull the whole open backlog (e.g. for a full-list review) rather than a single lookup.
capability_name: task_list
category: tasks
op: op.wb.task_list
schema_version: wb-capability/v1
parameters:
  state:
    type: str
    description: Exact state filter ('inbox' / 'focused' / 'done'). Omit for no state filter (subject to include_done). An explicit state overrides include_done.
    required: false
  include_done:
    type: bool
    description: When false (default) and no state is given, exclude completed tasks.
    required: false
  include_archived:
    type: bool
    description: Include archived tasks (default False).
    required: false
  limit:
    type: int
    description: Max results (default 500 — high so a full-list sweep is not silently truncated).
    required: false
tags:
- tasks
- task
- list
- enumerate
aliases:
- list open tasks
- list all tasks
- all open tasks
- enumerate tasks
- open task backlog
- every open task
- task list
parents:
- tasks
---
