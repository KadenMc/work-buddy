---
name: Task Creation Directions
kind: directions
description: How to create a task via the task-new workflow — minimal prompting, project + namespace inference, gates only on minting new projects/namespaces
summary: 'Route user task requests through the task-new workflow. The DAG enforces plan → enrich → confirm → create. Project assignment is first-class: the plan step infers a project + subtree path alongside free-form namespace tags, and the confirm step gates only the minting of *new* projects, *new* project subtrees, or *new* namespaces. Retroactive tag edits (including projects) go through task_set_tags.'
trigger: user asks to create a new task
command: wb-task-new
workflow: tasks/task-new
capabilities:
- tasks/task_create
tags:
- tasks
- create
- new
- directions
aliases:
- create task
- new task
- add task
parents:
- tasks
dev_notes: Project-slug validation lives in `_normalize_tags` (work_buddy/obsidian/tasks/mutations.py) with a `validate_project_slugs=False` escape hatch for the idempotency-cache resolver. The structural cutoff for what `task_set_tags` can/can't edit is the preserve list inside `_rewrite_namespace_tags` — if you add a new reserved prefix here, sync's `RESERVED_TAG_PREFIXES` has to know too. The plan step's `project` field is a slug-only shortcut; deeper subtree paths land via `proposed_tags` (e.g. `projects/work-buddy/systems/task-system`).
---

Route user task requests through the task-new workflow. The workflow's DAG enforces plan → enrich → confirm → create; agents don't manually sequence the suggester anymore. Project assignment is first-class: the plan step infers a project + subtree path alongside free-form namespace tags, and the confirm step gates only the minting of *new* projects or *new* subtrees, not the application of existing ones.

## Tag categories on a task line

The master-list line for a task carries up to four kinds of `#tag`:

| Kind | Example | Set via | Notes |
|---|---|---|---|
| `#todo` | `#todo` | `task_create` (auto) | Plugin marker; never propose. |
| `#projects/<slug>[/<subtree>...]` | `#projects/work-buddy/systems/task-system` | `task_create`'s `project` kwarg AND/OR `tags` list | Slug validates against the project registry; subtree below is free-form. Multiple `#projects/...` tokens per task are fine — the in-vault convention uses `<project>/<area>/<subarea>`. |
| Free-form namespace | `#admin/uhn`, `#paper/ecg-classifier` | `task_create`'s `tags` list | Any well-formed slug; classified by sync's discovery threshold. |
| `#tasker/*` | `#tasker/state/focused` | `task_change_state` | Mirrors store metadata; do NOT touch via tag tools. |

The inline-todo markers `#wb/todo` / `#wb/done` are workflow-internal and not user-modifiable through this surface.

## Slice 2 GTD vocabulary (optional)

`task_create` accepts 12 optional kwargs for GTD-shaped metadata: `task_kind` (`task` / `periodic` / `habit`), `density` (`sparse` / `developed`), `outcome_text`, `next_action_text`, `definition_of_done`, `creation_effort`, `user_involvement`, `creation_provenance` (open enum — use `manual` / `agent_inferred_from_journal` / `agent_inferred_from_chrome` / `agent_inferred_from_inline`), `has_deadline`, `deadline_date`, `has_dependency`, `dependency_hint`. Defaults match the legacy assumption (manual, sparse, developed effort, high involvement).

The task-new workflow's `plan` step doesn't currently emit these fields — they're available for agent-driven creators (Slice 3's Clarify will populate them from captures) and for explicit caller use. Add them to the workflow's plan output if the user asks for a developed task with sub-action-items, or if context provides a clear deadline.

## Retroactive tag edits

`task_set_tags` manages every user-modifiable tag on the line at once — free-form namespaces AND `#projects/<slug>/...` paths AND opt-in prefixes. Pass the complete desired list. Project slugs validate against the registry; unknown slugs raise ValueError (use `project_create` first).

Directly programmatic callers that bypass the task-new workflow (e.g. the triage executor creating a task from a Review decision that already carries suggested_namespace_tags, or a session-handoff helper minting a task from prior context) MUST pass `project=` (and full subtree paths via `tags`) themselves. The workflow's enrichment is the right place for inference; programmatic call sites without that inference are responsible for landing tasks with correct project assignment.
