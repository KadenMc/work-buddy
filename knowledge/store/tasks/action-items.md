---
name: Task action items
kind: reference
description: 'Per-action-item rows attached to a parent task. Each item carries its own risk profile + required contexts + definition_of_done. Safety rule: items with authorship=''agent_unapproved'' cannot be executed by the agent -- is_executable enforces this.'
entry_points:
- work_buddy.obsidian.tasks.action_items.create
- work_buddy.obsidian.tasks.action_items.list_for_task
- work_buddy.obsidian.tasks.action_items.update
- work_buddy.obsidian.tasks.action_items.approve
- work_buddy.obsidian.tasks.action_items.set_current
- work_buddy.obsidian.tasks.action_items.is_executable
- work_buddy.obsidian.tasks.action_items.position_in_task
tags:
- tasks
- action-items
- density
- develop-at-pickup
- safety-rule
- authorship
aliases:
- action items
- task action items
- task_action_items
- is_executable
- approve action item
- current step
- step N of M
- authorship
parents:
- tasks
dev_notes: The `authorship` enum supersedes the older `user_authored INTEGER` + `approved_at TEXT` pair; same three states, one canonical column. Markdown round-trip for action items is deferred (the master-list `step N of M` frontend and `/wb-task-develop` slash command depend on Resolution Surface integration). `set_current` uses raw SQL because `store.update` does not yet take the `current_action_item_id` kwarg -- when other callers need to write that column, add the kwarg to `store.update` like the context-array fields.
---

Per-action-item rows attached to a parent task. Module: `work_buddy/obsidian/tasks/action_items.py`. Schema: `task_action_items` table + `task_metadata.current_action_item_id` column.

## Schema

Per-item: `id`, `task_id` (FK), `sequence` (UNIQUE per task), `description`, `state` (`pending` | `in_progress` | `done` | `skipped`), `risk_profile_json`, `agent_required_contexts`, `user_required_contexts`, `definition_of_done`, `authorship`, `completed_at`, `handoff_package_path`, `deleted_at`, `created_at`, `updated_at`.

The `authorship` column is a three-valued enum (`work_buddy.threads.enums.Authorship`):

| Value | Meaning |
|---|---|
| `USER` | User wrote the item directly. Executable. |
| `AGENT_APPROVED` | Agent proposed it; user clicked Approve. Executable. |
| `AGENT_UNAPPROVED` | Agent proposed it; user has not yet approved. NOT executable. |

## CRUD entry points

- `create(task_id, *, description, sequence=None, ...)` -- auto-assigns next sequence when `None`.
- `get(item_id)`, `list_for_task(task_id, *, include_done=True)`, `update(item_id, **fields)` (sentinel discipline like `store.update`), `delete(item_id)` (soft -- sets `deleted_at`; see `restore` to undo).
- `approve(item_id)` -- flips `authorship` from `agent_unapproved` to `agent_approved` (the canonical way for the user to admit an agent-proposed item to execution).
- `set_current(task_id, item_id)` -- points `task_metadata.current_action_item_id` (`None` to clear).

## Safety rule

`is_executable(item)` returns `True` iff:

- `authorship` is `USER` or `AGENT_APPROVED`, AND
- `state` is not in `{done, skipped}` (terminal exclusion), AND
- `deleted_at` is `NULL`.

Every executor / Resolution Surface accept-action button / develop-at-pickup write step MUST call `is_executable` before running an item.

## position_in_task

`position_in_task(item) -> (1-based-index, total)` powers the master-list "step N of M" badge.

## See also

- `tasks/task_delete` -- the soft-delete pattern action_items.delete shares with task deletion.
- `architecture/migrations` -- how this table is created and rolled forward.
