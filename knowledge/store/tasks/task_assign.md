---
name: Task Assign
kind: capability
description: Claim a task for the current session and get full context (text, note, metadata)
capability_name: task_assign
category: tasks
op: op.wb.task_assign
schema_version: wb-capability/v1
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-a3f8c1e2')
    required: true
tags:
- tasks
- task
- assign
aliases:
- assign task
- claim task
- work on task
- start task
parents:
- tasks
requires:
- obsidian
---
