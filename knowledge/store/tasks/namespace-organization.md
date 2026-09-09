---
name: Task Namespace Organization
kind: reference
description: Inventory, preview, atomic apply, and conditional undo for independent task namespaces.
summary: Namespace organization preserves projects and ordinary tags; every bulk change is previewed against the affected task revisions before one atomic write.
tags:
- tasks
- namespace
- dashboard
parents:
- tasks
entry_points:
- work_buddy.tasks.namespaces
- work_buddy.dashboard.tasks_api
dev_notes: |-
  TaskNamespaceService reuses native authority checks, compare-and-swap writes, mutation receipts, task history, collection revisions, and outbox events. Preview fingerprints include the canonical request, complete affected task revisions and before/after tag sets, mapping, and issues; pagination never narrows mutation scope. Apply replans under the write lock. Undo restores the full original tag tuples only when every affected task is still at its applied revision.
---

Namespace paths organize tasks independently of project associations. The
organizer reads actual namespace assignments, including implicit ancestor
branches. Ordinary tags are not silently reclassified as namespaces.

| Route | Contract |
|---|---|
| `GET /api/tasks/namespaces` | Hierarchy with distinct-task subtree/direct counts, lifecycle counts, collection revision, and recent operations with undo availability. |
| `POST /api/tasks/namespaces/preview` | Read-only plan from an action request; returns mapping, collisions, counts, issues, fingerprint, and paged affected tasks. |
| `POST /api/tasks/namespaces/apply` | Send the preview's `request`, `expected_fingerprint`, and stable `client_mutation_id`. Applies atomically or rejects a changed preview. |
| `POST /api/tasks/namespaces/<operation_id>/undo` | Send a new stable `client_mutation_id`. Refuses when any affected task changed after apply. |

Requests use `action`:

- `rename`: one `sources` path and a single-segment `name`; descendants follow.
- `move`: `sources` and destination parent; `destination: ""` means root.
- `merge`: `sources` and an existing destination; optionally set
  `include_descendants` (default true).
- `promote`: remove a grouping level by lifting children into its parent.
  Direct assignments require `parent_assignment: "keep"` or `"move"` with
  `parent_destination`.
- `remove`: remove assignments, optionally including descendants; task records
  remain intact.
- `assign`: explicit `task_ids`, `assignment_mode` (`add`, `remove`, `replace`),
  and `namespaces`. Replacing with an empty list removes all selected tasks'
  namespace assignments.

Structural operations cover all lifecycle statuses, regardless of browsing
filters. Assignment operations cover only the explicitly selected tasks. Preview
shows that scope, status counts, duplicate assignments removed, and tasks left
without a namespace. Use `tasks_offset` and `tasks_limit` (1–200) to inspect
affected-task pages.

Unexpected destination collisions block apply unless explicitly reviewed with
`merge_collisions: true`; Merge is itself an explicit merge choice. Self/descendant
moves and ordinary-tag collisions are rejected. Project links, task content,
and lifecycle are preserved. Apply and undo require dashboard mutation authority;
read-only mode cannot write. Exact retries replay their durable receipt. A stale
preview requires a new review, and undo never overwrites later task edits.
