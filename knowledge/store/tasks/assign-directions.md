---
name: Task Assign Directions
kind: directions
description: How to assign a task — presentation format, completion tracking, state change protocol
summary: 'Use MCP tool directly, never Python. task_assign = claim + read; use task_read when you only want to inspect. Present: task text/state/urgency, contract, note preview with path, session count. State NOT changed by assignment. After completing deliverable, proactively ask to mark done — always confirm first.'
trigger: user runs /wb-task-assign or asks to assign a task to the current session
command: wb-task-assign
capabilities:
- tasks/task_assign
- tasks/task_toggle
tags:
- tasks
- assign
- session
- completion
- directions
aliases:
- assign task
- claim task
- pick up task
parents:
- tasks
---

Assign via mcp__work-buddy__wb_run("task_assign", {"task_id": "<id>"}). Do NOT use Python code.

If no argument provided, ask for the task ID.

## Assign vs read

- **task_assign** = read the task AND record the current session against it (claim). Use when the user is starting work.
- **task_read** = pure read, no session write. Use when you only need to inspect a task (e.g. user is browsing, or an upstream flow wants the payload without implying a claim).

Both return the same read payload. Internally, task_assign composes task_read with a session-tracker write, so agents can swap call sites without reshaping downstream code.

The assignment is queryable in both directions: the read payload embeds `assigned_sessions` (task → sessions), and `session_tasks_get(session_id)` returns the reverse (session → tasks, with each task's text + state).

## Presentation after assignment

1. Task: text and current state/urgency
2. Contract: which contract it serves (if any)
3. Note: preview of note content (if exists) + file path
4. Sessions: how many sessions have worked on this task

## State

Task state is NOT changed by assignment. To mark focused, use task_change_state separately.

## Completion tracking

After committing code (or completing the deliverable), proactively ask: "Task <id> looks complete -- should I mark it done?"

On approval: mcp__work-buddy__wb_run("task_toggle", {"task_id": "<id>", "done": true})

Do not silently mark the task done -- always confirm first.
