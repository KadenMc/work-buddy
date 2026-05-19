---
name: Task Scattered
kind: capability
description: Find open tasks scattered across the vault outside the master task list. Groups by file with counts. Uses Datacore structural queries.
capability_name: task_scattered
category: tasks
op: op.wb.task_scattered
schema_version: wb-capability/v1
parameters:
  limit:
    type: int
    description: Max tasks to scan (default 100)
    required: false
tags:
- tasks
- task
- scattered
aliases:
- scattered tasks
- orphan tasks
- tasks outside master list
- forgotten tasks
- tasks in journal
- tasks in docs
parents:
- tasks
requires:
- obsidian
---
