---
name: Task Update Description Directions
kind: directions
description: How to rewrite a task's description text safely - preferred over filesystem-direct edits, atomic against concurrent user edits
summary: Use task_update_description, NOT filesystem-direct Edit. It routes through the atomic app.vault.process() write path, so concurrent user typing in Obsidian doesn't get clobbered.
trigger: agent or user wants to change the description text of an existing task line
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
    "new_description": "Rewritten task text"
})
```

Returns:
  - `success`, `task_id`, `old_description`, `new_description`
  - `old_line` / `new_line` - the full markdown line
  - `store_updated` - whether the SQLite description column was kept in sync
  - `atomic` - True when the write went through the atomic `app.vault.process()` path; False / absent when the legacy fallback ran (bridge unreachable)
  - `conflict_resolved` - True when a concurrent-edit conflict was detected and the retry succeeded

## What it preserves

Checkbox state, the `#todo` marker, all hashtags (`#projects/*`, namespace tags), wikilinks (including the task-note link `[[uuid|📓]]`), the 🆔 marker and ID, plugin emojis (📅 due date, ✅ done date, 🔼/⏫ urgency).

## What it does NOT do

- It will not change the task's state, urgency, due date, or completion. Use `task_change_state` / `task_toggle` for those.
- It will not add or remove tags. Use `task_set_tags` for tag rewrites.
- It rejects multi-line descriptions. Task lines are single-line by construction; put detail in the `[[uuid|📓]]`-linked note.

## Why NOT to use filesystem-direct Edit

The master-task-list.md has a documented read-modify-write race. Historically the only path for description rewrites was `Edit` on `master-task-list.md`, which can clobber concurrent user typing in Obsidian. The atomic write path (`app.vault.process()`) closes that race, but only if you go through `task_update_description`. Filesystem-direct edits bypass it.

If `task_update_description` returns `success=False, conflict=True`, the user is actively editing the task line in Obsidian. Surface the conflict to the user; don't keep retrying.
