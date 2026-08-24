---
name: Task Read
kind: capability
description: Read native structured task context plus the current projection-free Co-work knowledge-document head without claiming it.
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
requires: []
---

The document content includes the current structured head and uncompacted Yjs
updates. No Markdown task line, note path, or Obsidian bridge is involved.
