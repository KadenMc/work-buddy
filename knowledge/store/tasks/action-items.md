---
name: Task action items
kind: reference
description: 'Per-action-item rows attached to a parent task. Each item carries its own risk profile + required contexts + definition_of_done. Safety rule: items with authorship=''agent_unapproved'' cannot be executed by the agent -- is_executable enforces this.'
entry_points:
- work_buddy.tasks.service.TaskService.create_action_item
- work_buddy.tasks.service.TaskService.update_action_item
- work_buddy.tasks.store.TaskStore
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
dev_notes: Native writes validate required-context arrays, definition_of_done, and risk_profile_json before opening a receipt transaction. A risk profile string must decode to a JSON object. Automatic sequence allocation uses the highest historical sequence including soft-deleted rows, so deletion never reuses an identity-bearing position.
---

Per-action-item rows are owned by native `TaskStore` and mutated through
`TaskService` with the parent task's expected revision and a stable mutation ID.
Every change advances task history and returns a mutation receipt.

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

Position and current-item state are rendered directly from structured rows in
the React Tasks view; no Markdown round-trip is involved.

## See also

- `tasks/task_delete` -- the soft-delete pattern action_items.delete shares with task deletion.
- `architecture/migrations` -- how this table is created and rolled forward.
