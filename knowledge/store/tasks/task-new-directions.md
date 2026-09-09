---
name: Task Creation Directions
kind: directions
description: How to create a task via the task-new workflow, choosing registered project links independently of free-form namespace organization.
summary: 'Route user task requests through plan → enrich → confirm → create. Tasks can link to several registered projects through project_ids. Namespaces are independent; task_set_tags never changes project membership. Avoid unnecessary prompting for existing or explicitly requested organization.'
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
dev_notes: Project and tag validation live in the native task service. The optional legacy project field resolves a slug or alias only at explicit ingress; project_ids carries stable registry membership. Enrichment returns registry IDs in known_projects and treats old projects/ subtree hints only as namespace context. The enrich auto-run must stay on work_buddy.tasks.capabilities.enrich_plan; importing the legacy Obsidian namespace module would reintroduce a task runtime dependency.
---

Route user task requests through the task-new workflow. The workflow's DAG enforces plan → enrich → confirm → create; agents don't manually sequence the suggester. Choose project associations from the registry when supported by the request or context. Zero or several projects are valid. Choose namespaces separately according to the user's organization; no project prefix is required.

## Structured tags

Native tasks store `project_ids` independently from namespace assignments.
Namespaces use freely chosen paths such as `admin/uhn` or
`work-buddy/task-system`. A `projects/<slug>` namespace has no special membership
meaning and should not be generated from a project selection. Existing paths
can be used without extra prompting; honor explicit user choices when creating
new organization. There is no
`#todo`, Tasks-plugin, checkbox, or master-line syntax in the native contract.

## MIT tasks require a summary

When creating a Most Important task, provide a handoff-quality `summary`: what
needs to be done, why, and relevant context. This is a scalar task field, not an
implicit request to create a Co-work document. A separate note requires the
explicit note-role and initial-content parameters described by `task_create`.

## GTD vocabulary (optional)

`task_create` accepts 12 optional kwargs for GTD-shaped metadata: `task_kind` (`task` / `periodic` / `habit`), `density` (`sparse` / `developed`), `outcome_text`, `next_action_text`, `definition_of_done`, `creation_effort`, `user_involvement`, `creation_provenance` (open enum — use `manual` / `agent_inferred_from_journal` / `agent_inferred_from_chrome` / `agent_inferred_from_inline`), `has_deadline`, `deadline_date`, `has_dependency`, `dependency_hint`. Defaults match the legacy assumption (manual, sparse, developed effort, high involvement).

The task-new workflow's `plan` step doesn't currently emit these fields — they're available for agent-driven creators (Clarify populates them from captures) and for explicit caller use. Add them to the workflow's plan output if the user asks for a developed task with sub-action-items, or if context provides a clear deadline.

## Retroactive tag edits

`task_set_tags` replaces the complete structured tag set and marks every supplied
entry as a namespace. Use task detail's separate fields when preserving ordinary
tags. This capability does not assign or remove projects. Edit
project associations separately in task detail. Registered IDs are required
for new links; create an explicitly requested new project through `project_create`
first. Namespace organization offers previewed bulk changes and guarded Undo.

Programmatic creators must pass intended `project_ids` explicitly, or the legacy
`project` slug/alias argument at an existing compatibility boundary. Namespace
spelling and a contract slug do not create project membership. Preserve every
known association when carrying a shared task between producer surfaces.
