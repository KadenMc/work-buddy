---
name: Task Review Inbox
kind: skill
description: Get inbox tasks with suggested actions (mit, snooze, kill, needs_date)
category: tasks
op: op.wb.task_review_inbox
schema_version: wb-skill/v1
skill_name: task_review_inbox
tags:
- tasks
- task
- review
- inbox
aliases:
- review new tasks
- inbox triage
- undecided tasks
- new task review
- inbox items
- what's in my inbox
- decide on new tasks
parents:
- tasks
---

Returns native inbox tasks with attention-state recommendations. The React
Tasks view provides the interactive five-item triage surface.
