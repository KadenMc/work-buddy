---
name: Session Tasks Get
kind: capability
description: List the tasks a session was assigned to (the reverse of task→sessions), each enriched with its current text + state. Bridge-independent — reads the SQLite task store, so it works even when Obsidian isn't running. Read-only.
capability_name: session_tasks_get
category: tasks
op: op.wb.session_tasks_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
tags:
- tasks
- session
- assignment
- provenance
- reverse
- get
aliases:
- tasks for session
- which tasks did this session work on
- session task assignments
- reverse task lookup
parents:
- tasks
---

Return the tasks a session was assigned to — the reverse of the forward `task → sessions` linkage (`task_assign` writes a `task_sessions` row; this reads it back by `session_id`).

Each task is enriched with its current `task_text` (description) and `state` straight from the SQLite task store, so this capability is **bridge-independent**: it stays callable when the Obsidian bridge is down (unlike a `task_read`-based enrichment).

Returns:

```
{
  "tasks": [
    {
      "task_id": str,
      "assigned_at": str,       # ISO-8601 of the assignment
      "task_text": str | None,  # current description from the store
      "state": str | None       # inbox | mit | focused | snoozed | done
    },
    ...
  ]
}
```

Oldest-first by `assigned_at`. An empty list means no session→task assignment was ever recorded for that session.

## Related

- `task_assign` writes the assignment (forward direction).
- For "which sessions worked on this task" (forward), the task read payload already embeds `assigned_sessions`.
- For a session's PR activity, see `session_prs_get`.
