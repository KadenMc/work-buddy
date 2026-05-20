---
name: Context Tasks
kind: capability
description: 'Obsidian task summary: outstanding tasks + recent state changes (last 48h by default)'
capability_name: context_tasks
category: context
op: op.wb.context_tasks
schema_version: wb-capability/v1
parameters:
  journal_days:
    type: int
    description: Days of journal entries to scan (default 7)
    required: false
  event_hours:
    type: int
    description: Hours of task state history to include (default 48, from config). Pass 0 to suppress.
    required: false
tags:
- context
- tasks
aliases:
- outstanding tasks
- task list
- what needs doing
- todo items
- task events
- task history
- task changes
parents:
- context
requires:
- obsidian
---
