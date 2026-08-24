---
name: Task Update Description Directions
kind: directions
description: How to update a native task description safely with revision checking and idempotent replay.
summary: Use task_update_description with the current revision; a stale write returns structured conflict state rather than overwriting newer work.
trigger: agent or user wants to change an existing task description
capabilities:
- tasks/task_update_description
tags:
- tasks
- update
- description
- atomic
- directions
aliases:
- rewrite task
- rename task
- edit task description
- change task wording
- rephrase task
parents:
- tasks
---

## When to use

Whenever a task's description text needs to change - typo fix, scope sharpening, mid-flight rephrase. Examples:
  - User asks "can you rename that task to X"
  - You're consolidating two scope-shifted tasks and the description needs to reflect the new scope
  - You spot a typo and want to correct it without deleting + recreating the task (preserves task_id, history, sessions)

## Call

```
mcp__work-buddy__wb_run("task_update_description", {
    "task_id": "t-...",
    "new_description": "Rewritten task text",
    "expected_revision": 7
})
```

Returns the native task, its revision, collection revision, and durable mutation
receipt. The gateway supplies `expected_revision` and a stable
`client_mutation_id` when an ordinary caller omits them.

## What it preserves

Task identity, structured tags, state, dates, history, action items, and the
Co-work document binding remain unchanged.

## What it does NOT do

- It will not change the task's state, urgency, due date, or completion. Use `task_change_state` / `task_toggle` for those.
- It will not add or remove tags. Use `task_set_tags` for tag rewrites.
- It rejects multi-line descriptions. Put detail in the task's Co-work knowledge document.

## Conflicts

Never edit frozen task Markdown. If the capability returns a revision conflict,
surface the live task/revision, reconcile the user's change, and submit a new
semantic mutation. Do not blindly retry a stale request.
