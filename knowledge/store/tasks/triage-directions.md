---
name: Task Triage Directions
kind: directions
description: How to run interactive task triage — presentation rules, actions, summary format
summary: 'Present tasks in batches of 5. Actions: mit, focus, snooze, done, kill, skip. Always ask user — don''t decide for them. Report breakdown after applying.'
trigger: user wants to triage or review pending inbox tasks
command: wb-task-triage
workflow: tasks/task-triage
capabilities:
- tasks/task_review_inbox
- tasks/task_stale_check
- tasks/task_change_state
tags:
- tasks
- triage
- inbox
- interactive
- directions
aliases:
- triage tasks
- review inbox tasks
- batch task decisions
- inbox review
parents:
- tasks
---

Start the task triage workflow via mcp__work-buddy__wb_run("task-triage"). Follow the conductor.

## Presentation rules

Present tasks in batches of 5 at a time. For each task show:
- The task description (trimmed, no raw tags)
- The suggested action and reason
- Ask user to confirm, override, or skip

Available actions:
- mit: select as Most Important Task for this week
- focus: promote to actively working on now
- snooze: defer (set snooze_until date if possible)
- done: mark as completed
- kill: archive/remove (for stale tasks beyond saving)
- skip: leave as-is for now

State changes are stored in the SQLite metadata store (not inline tags).

## Summary format

After applying decisions, report:
- How many tasks were triaged
- Breakdown by action taken
- Current state after triage (focused count, inbox count)

## Don'ts

- Don't present all tasks at once -- batches of 5 maximum
- Don't make decisions for the user -- always ask
- Don't recommend system reorganization, new tags, or process changes
- Don't explain the task management philosophy
- Don't add overhead -- this should take under 5 minutes

Be efficient. Don't explain the system. Just present tasks and collect decisions.
