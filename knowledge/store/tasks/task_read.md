---
name: Task Read
kind: capability
description: Read a task's full context (text, note, metadata) without claiming it for the current session
capability_name: task_read
category: tasks
parameters:
  task_id:
    type: str
    description: Task ID (e.g., 't-xxxxxxxx')
    required: true
op: op.wb.task_read
schema_version: wb-capability/v1
tags:
- tasks
- task
- read
aliases:
- read task
- view task
- show task
- inspect task
- look at task
parents:
- tasks
requires:
- obsidian
---
