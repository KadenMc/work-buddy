---
name: Task action items
kind: reference
description: 'Per-action-item rows attached to a parent task. Each item carries its own risk profile, required contexts, and definition_of_done. Execution invariant: an agent_unapproved item must not be executed by an agent.'
entry_points:
- work_buddy.tasks.service.TaskApplicationService.create_action_item
- work_buddy.tasks.service.TaskApplicationService.update_action_item
- work_buddy.tasks.service.TaskApplicationService.reorder_action_items
- work_buddy.tasks.service.TaskApplicationService.set_current_action_item
- work_buddy.tasks.service.TaskApplicationService.approve_action_item
- work_buddy.tasks.service.TaskApplicationService.delete_action_item
- work_buddy.tasks.service.TaskApplicationService.restore_action_item
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
dev_notes: |-
  Native writes validate required-context arrays, definition_of_done, and risk_profile_json before opening a receipt transaction. A risk profile string must decode to a JSON object.

  Create and restore allocate the next sequence from live rows (`deleted_at IS NULL`). Soft deletion moves the row to a negative sequence, so a later live item may reuse its former positive display position; the stable identity is the action-item `id`, not `sequence`.
---

Per-action-item rows are owned by native `TaskStore` and mutated through
`TaskApplicationService` with the parent task's expected revision and a stable
client mutation ID.
Every change advances task history and returns a mutation receipt.

## Schema

Per-item: `id`, `task_id` (FK), `sequence` (UNIQUE per task), `description`, `state` (`pending` | `in_progress` | `done` | `skipped`), `risk_profile_json`, `agent_required_contexts`, `user_required_contexts`, `definition_of_done`, `authorship`, `completed_at`, `handoff_package_path`, `deleted_at`, `created_at`, `updated_at`.

The `authorship` column is a three-valued enum (`work_buddy.threads.enums.Authorship`):

| Value | Meaning |
|---|---|
| `user` (`Authorship.USER`) | User wrote the item directly. Executable. |
| `agent_approved` (`Authorship.AGENT_APPROVED`) | Agent proposed it and the user approved it. Executable. |
| `agent_unapproved` (`Authorship.AGENT_UNAPPROVED`) | Agent proposed it and the user has not approved it. Not executable. |

## CRUD entry points

- `create_action_item(task_id, *, description, ...)` appends the next live sequence.
- `update_action_item(task_id, action_item_id, *, changes=...)` validates and applies supported field changes.
- `reorder_action_items(task_id, *, action_item_ids=...)` requires every live item exactly once.
- `set_current_action_item(task_id, *, action_item_id=...)` points `task_metadata.current_action_item_id`; pass `None` to clear it.
- `approve_action_item(task_id, action_item_id)` writes `agent_approved` through the ordinary revisioned update path.
- `delete_action_item(task_id, action_item_id)` soft-deletes an item; `restore_action_item(...)` returns it at the end of the live order.

There are no standalone native `get(item_id)` or `list_for_task(...)` entry
points. `TaskStore.get` / `TaskApplicationService.get` and list queries return
action items embedded in their parent `Task`.

## Safety rule

The native task domain does not currently expose an action-item executor or an
`is_executable` helper. Any native executor added later must treat an item as
executable only when:

- `authorship` is `user` or `agent_approved`, AND
- `state` is not in `{done, skipped}` (terminal exclusion), AND
- `deleted_at` is `NULL`.

The similarly named helper under `work_buddy.obsidian.tasks.action_items` is
frozen legacy compatibility code and must not become a live execution
dependency. Enforce the invariant inside the native boundary before any future
executor, acceptance action, or develop-at-pickup write runs an item.

## position_in_task

Position and current-item state are rendered directly from structured rows in
the React Tasks view; no Markdown round-trip is involved.

## See also

- `tasks/task_delete` -- the soft-delete pattern action_items.delete shares with task deletion.
- `architecture/migrations` -- how this table is created and rolled forward.
