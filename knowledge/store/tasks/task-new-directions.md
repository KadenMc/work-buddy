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
dev_notes: Project and tag validation live in the native task service. The plan step's `project` field is a slug-only shortcut; deeper subtree paths land via `proposed_tags` (for example `projects/work-buddy/systems/task-system`). The enrich auto-run must stay on `work_buddy.tasks.capabilities.enrich_plan`; importing the legacy Obsidian namespace module would reintroduce a task runtime dependency.
---

Route user task requests through the task-new workflow. The workflow's DAG enforces plan → enrich → confirm → create; agents don't manually sequence the suggester anymore. Project assignment is first-class: the plan step infers a project + subtree path alongside free-form namespace tags, and the confirm step gates only the minting of *new* projects or *new* subtrees, not the application of existing ones.

## Structured tags

Native tasks store project and namespace tags as structured rows. A project tag
uses `projects/<slug>[/<subtree>...]`; free-form namespaces use paths such as
`admin/uhn` or `paper/ecg-classifier`. The registered project slug is validated,
while an intentional subtree may be minted after confirmation. There is no
`#todo`, Tasks-plugin, checkbox, or master-line syntax in the native contract.

## MIT tasks require a summary

When creating a Most Important task, provide `summary` as handoff-quality initial
content for its Co-work knowledge document: what needs to be done, why, and any
relevant context. A focused task without knowledge is a continuity gap.

## GTD vocabulary (optional)

`task_create` accepts 12 optional kwargs for GTD-shaped metadata: `task_kind` (`task` / `periodic` / `habit`), `density` (`sparse` / `developed`), `outcome_text`, `next_action_text`, `definition_of_done`, `creation_effort`, `user_involvement`, `creation_provenance` (open enum — use `manual` / `agent_inferred_from_journal` / `agent_inferred_from_chrome` / `agent_inferred_from_inline`), `has_deadline`, `deadline_date`, `has_dependency`, `dependency_hint`. Defaults match the legacy assumption (manual, sparse, developed effort, high involvement).

The task-new workflow's `plan` step doesn't currently emit these fields — they're available for agent-driven creators (Clarify populates them from captures) and for explicit caller use. Add them to the workflow's plan output if the user asks for a developed task with sub-action-items, or if context provides a clear deadline.

## Retroactive tag edits

`task_set_tags` replaces the complete structured tag set. Pass every desired
free-form namespace and `projects/<slug>/...` path. Unknown project slugs are
rejected (use `project_create` first).

Directly programmatic callers that bypass the task-new workflow (e.g. the triage executor creating a task from a Review decision that already carries suggested_namespace_tags, or a session-handoff helper minting a task from prior context) MUST pass `project=` (and full subtree paths via `tags`) themselves. The workflow's enrichment is the right place for inference; programmatic call sites without that inference are responsible for landing tasks with correct project assignment.
