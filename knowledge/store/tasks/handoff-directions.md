---
name: Task Handoff Directions
kind: directions
description: How to write a structured session handoff prompt and package it as native task knowledge.
summary: 'Write from what you already know. Use the exact section structure: Task, Context, What I Already Know, Key Files, System Notes, Suggested Approach. Create via task_create with initial Co-work knowledge in summary.'
trigger: user wants to hand off in-progress work to a new agent session via a task
command: wb-task-handoff
capabilities:
- tasks/task_create
tags:
- tasks
- handoff
- session
- continuity
- directions
aliases:
- handoff task
- session handoff
- pass off work
- create handoff note
parents:
- tasks
---

Create a native task with a comprehensive handoff prompt in its Co-work knowledge document.

## Write the handoff prompt

Consolidate into these exact sections:

> This is a handoff prompt from session {WORK_BUDDY_SESSION_ID}.

## Task -- What needs to be done. One concrete paragraph.
## Context -- Why this exists. Motivation without needing to ask.
## What I Already Know -- Decisions, approaches tried/abandoned, edge cases, constraints. Highest-value section.
## Key Files -- Absolute paths with brief annotations.
## System Notes -- Non-obvious environmental knowledge. Skip if nothing applies.
## Suggested Approach -- If you have a clear idea. Omit if not.

Be specific. File paths absolute. Decisions include rationale.

## Create the task

mcp__work-buddy__wb_run("task_create", {"task_text": "<concise summary>", "summary": "<full handoff prompt>"})

The `summary` parameter provisions the Co-work document. `task_create` carries a
stable mutation ID, so a response-loss retry through the gateway replays the
same native task instead of creating a duplicate. Do not use `obsidian_retry`.

## Confirm

Show: task ID, brief summary, remind about /wb-task-assign <id>.
