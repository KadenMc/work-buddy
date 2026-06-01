---
name: Docs Edit
kind: workflow
description: Edit or create any knowledge unit by editing its Markdown file directly, with kind-aware validation and index propagation bracketing the edit.
workflow_name: docs-edit
execution: main
allow_override: false
command: null
params_schema:
  path:
    type: str
    description: Store path of the unit to edit or create (e.g. "tasks/my-directions").
    required: true
  create:
    type: bool
    description: If true, scaffold a new unit at `path` first (requires `kind`).
    required: false
  kind:
    type: str
    description: "(create only) Unit kind: directions, system, concept, reference, integration, service, capability, or workflow."
    required: false
steps:
- id: resolve
  name: Validate the request and resolve the file path
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.knowledge.edit_flow.resolve_for_edit
    input_map:
      params: __params__
    timeout: 30
- id: edit
  name: Edit the unit's Markdown file with your native Edit tool
  step_type: reasoning
  depends_on:
  - resolve
  result_schema:
    required_keys:
    - edited
    key_types:
      edited: bool
- id: commit
  name: Validate the edit, propagate to the store + index, and report
  step_type: code
  depends_on:
  - edit
  auto_run:
    callable: work_buddy.knowledge.edit_flow.commit_edit
    input_map:
      resolve: resolve
    timeout: 60
tags:
- docs
- editing
- knowledge-store
aliases:
- edit a knowledge unit
- create a knowledge unit
- update docs
- edit a directions unit
- edit a workflow unit
parents:
- context
---

`docs_edit` is how an agent edits or creates a knowledge unit. The system store is one Markdown file per unit, so **editing a unit is editing its file** — this workflow brackets that native `Edit` with a resolve step (validate the request, hand back the file path) and a commit step (validate the result, reconcile the store cache + search index) so the change is correct and immediately visible to `agent_docs` / `knowledge` queries.

It handles **every unit kind** — prose (directions, system, concept, reference, integration, service), capability declarations, and workflow units alike; the commit step's validation is kind-aware. Operations that are not content edits — deleting or moving a unit — stay on the `docs_delete` / `docs_move` capabilities.

## resolve

Auto-run. Validates the request and returns the absolute `.md` `file` to edit. With `create=true` it scaffolds a minimal valid unit of the given `kind` first and returns that file. You take no action here — read the returned `file` path for the next step.

## edit

Edit the unit file at the `file` path the `resolve` step returned, using your native `Edit` tool. The YAML frontmatter carries the unit's structured fields; the Markdown body is `content.full`. For a **workflow** unit, the `steps` DAG lives in frontmatter and each step's prose lives under a `## <step-id>` body section — keep the step ids in the frontmatter and the body headings in sync. When the edit is done, advance with `{"edited": true}`.

## commit

Auto-run. Re-reads the file and runs the kind-aware validation suite — DAG integrity, duplicate placeholders, required and kind-specific fields, capability op-resolution, directions→workflow binding resolution, and (for workflow units) step-DAG cycles / dangling dependencies and `## heading` ↔ step-id consistency — then reconciles the store cache and search index. If it returns `status: "error"`, fix the reported issues in the file and run `docs_edit` again; the reported `unit_errors` are scoped to the unit you edited.
