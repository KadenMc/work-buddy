---
name: Task Read Directions
kind: directions
description: How to read a task without claiming it — inspection-only path that does not write a session assignment
summary: Pure-read path. Use task_read when the user wants to inspect a task without claiming it for the current session. No session-tracker write, no state change. Use task_assign instead when the user is starting work.
trigger: user runs /wb-task-read or asks to view a task without claiming it for the current session
command: wb-task-read
capabilities:
- tasks/task_read
tags:
- tasks
- read
- inspect
- directions
aliases:
- read task
- view task
- inspect task
- show task
- look at task
parents:
- tasks
---

Read via mcp__work-buddy__wb_run("task_read", {"task_id": "<id>"}). Do NOT use Python code.

If no argument provided, ask for the task ID.

## When to use task_read vs task_assign

- **task_read** — pure inspection. No session is recorded against the task. Use when the user is browsing, auditing, or you only need the payload for an upstream flow.
- **task_assign** — inspection PLUS claims the task for the current session. Use when the user is starting work on it.

Both return the same read payload (task text, metadata, note content, file + line, existing session list). If you assigned a task earlier in the conversation and only want to re-inspect it now, task_read is fine — assigning again just writes an idempotent duplicate session row.

## Presentation

Same as task_assign:

1. Task: text and current state/urgency
2. Contract: which contract it serves (if any)
3. Note: preview of note content (if exists) + file path
4. Sessions: how many sessions have worked on this task (read-only view)

## Do NOT

- Do not propose marking the task done from a read. Completion tracking belongs to the assign flow — a user who only asked to inspect has not committed to finishing it.
