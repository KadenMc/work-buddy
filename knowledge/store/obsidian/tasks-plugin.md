---
name: Obsidian Tasks Plugin Integration
kind: integration
description: Runtime integration with Tasks plugin (v7.23.1) -- cache API, ownership split, mutation pipeline, tag behavior, emoji-aware sync, soft-delete contract
tags:
- obsidian
- tasks
- plugin
- cache
- mutation
- ownership
- eval_js
- emoji-sync
- soft-delete
aliases:
- obsidian tasks
- tasks plugin
- task cache
- task mutation
parents:
- obsidian
- obsidian
---

Runtime integration with Obsidian Tasks plugin (v7.23.1) via eval_js bridge.

## Architecture

Python wrappers in env.py execute JS snippets from _js/ via bridge.eval_js(). All data comes from plugin cache (plugin.cache.tasks), not disk parsing.

## Plugin API (apiV1)

executeToggleTaskDoneCommand(line, path) -> programmatic toggle, returns new line text. Handles recurring tasks, done dates, and status transitions. createTaskLineModal() and editTaskLineModal() open UI modals (not programmatic).

## Plugin-Native Mutation Pipeline

1. Find Task in cache by ID/description
2. Call task.handleNewStatus(newStatus) -> returns new Task[] (handles recurrence, dates, pure function)
3. Call result[0].toFileLineString() -> correctly formatted line string
4. Write string to file via bridge.write_file() or vault.process()
5. Cache auto-reindexes from file change via vault subscription

## Ownership Split

Tasks plugin owns: checkbox state, done/cancelled dates, recurrence, priority emojis, due/scheduled/start dates.
work-buddy owns: state (inbox/mit/focused/snoozed/done/deleted), urgency, complexity, contract link, snooze-until, state change history, task ID (t-<hex>), soft-delete tombstone (deleted_at).
Shared: #projects/* tags.

Rule: Never fight the plugin's output. Let it compute its part, then add/update ours.

## Emoji-aware sync

When the master list is reconciled against the store, `task_sync` parses Tasks-plugin emoji metadata and drift-checks against the store:

- deadline emoji + YYYY-MM-DD -> `task_metadata.deadline_date` + `has_deadline=True`
- urgency emojis (high / medium / low) -> `task_metadata.urgency`
- done emoji + YYYY-MM-DD -> `task_metadata.completed_at`

See `tasks/task_sync` for the full reconcile pipeline and the no-orphan-cleanup decision.

## Soft-delete contract

The store side enforces a no-DELETE discipline across three call sites:

- `store.delete(task_id)` -> `UPDATE task_metadata SET deleted_at = now`. Restorable via `store.restore`. See `tasks/task_delete`.
- `store.set_task_tags(task_id, tags)` -> diff-and-update. Never DELETEs from `task_tags`. See `tasks/task_set_tags`.
- `action_items.delete(item_id)` -> `UPDATE task_action_items SET deleted_at = now`. See `tasks/action-items`.

Foreign keys on `task_action_items` / `task_tags` have no `ON DELETE` action (`_m009_drop_cascade`) so even raw SQL can't accidentally cascade. The off-machine backup system (`architecture/backups`) is the last line of defense.

## Tag Behavior

New tasks: clean lines with only #todo and #projects/* inline. State/urgency/complexity in SQLite store.
Legacy tasks: may still have #tasker/state/* and #tasker/urgency/* inline. Manager reads store first, falls back to inline tags.

## Priority Mapping

1=high (mapped from emoji), 2=medium (mapped from emoji), 3=none/default (no emoji).

## Task Object Key Properties

description, originalMarkdown, statusCharacter, status.type (TODO/DONE), priority (1-3), tags, _dueDate/_doneDate/_createdDate (Moment objects), recurrence, children, parent, taskLocation._tasksFile._path, taskLocation._lineNumber.

## Limitations

- Tasks plugin cache is read-only for programmatic use. Creation uses raw markdown approach.
- Cache includes all vault files -- filter by file_path for canonical list.
- Moment.js dates must be formatted before serialization.
- Task objects have circular parent/children refs preventing naive JSON.stringify.
