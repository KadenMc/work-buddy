---
name: Task Scattered
kind: capability
description: Retired legacy vault-task discovery surface; unavailable under native task authority.
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
requires: []
---

Do not use this as a task source after native cutover. Vault checkboxes may be
captured through explicit intake workflows, but they are never peer task truth.
