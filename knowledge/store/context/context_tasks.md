---
name: Context Tasks
kind: skill
description: 'Native task summary: outstanding tasks plus recent lifecycle changes (last 48h by default)'
category: context
op: op.wb.context_tasks
schema_version: wb-skill/v1
parameters:
  journal_days:
    type: int
    description: Days of journal entries to scan (default 7)
    required: false
  event_hours:
    type: int
    description: Hours of task state history to include (default 48, from config). Pass 0 to suppress.
    required: false
skill_name: context_tasks
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
---

After native activation this skill queries `TaskStore`; it never scans the
frozen master list or task-note Markdown.
