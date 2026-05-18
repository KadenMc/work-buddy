---
name: Task New
kind: workflow
description: Interactive task creation with project + namespace-tag inference. Plans the task, enriches with project-registry + tag-universe context, confirms with the user (only when minting a new project, new project subtree, or new namespace), then applies via task_create.
workflow_name: task-new
execution: main
allow_override: false
steps:
- id: plan
  name: Plan the task and propose tags
  step_type: reasoning
  depends_on: []
  result_schema:
    required_keys:
    - task_text
    key_types:
      task_text: str
      urgency: str
      project: str
      due_date: str
      contract: str
      summary: str
      proposed_tags: list
  invokes: []
- id: enrich
  name: Enrich plan with tag-universe context
  step_type: code
  depends_on:
  - plan
  auto_run:
    callable: work_buddy.obsidian.tasks.namespace_suggest.enrich_plan
    input_map:
      plan: plan
    timeout: 30
  visibility:
    mode: summary
    include_keys:
    - suggestions
    - tag_status
    - project_status
    - universe_size
  invokes: []
- id: confirm
  name: Confirm plan with user (especially any new namespaces or new project assignments)
  step_type: reasoning
  depends_on:
  - enrich
  result_schema:
    required_keys:
    - final_plan
    - confirmed
    key_types:
      final_plan: dict
      confirmed: bool
  invokes: []
- id: apply
  name: Create the task (via task_create)
  step_type: code
  depends_on:
  - confirm
  visibility:
    mode: summary
    include_keys:
    - success
    - task_id
    - task_line
    - skipped
    - message
  invokes:
  - task_create
- id: report
  name: Report the created task
  step_type: reasoning
  depends_on:
  - apply
  invokes: []
tags:
- tasks
- task
- new
- create
- namespace
parents:
- tasks
---

## plan

Agentic step. Read the user's request (from the slash-command argument or the surrounding conversation) and emit a structured plan.

Advance with a dict of this exact shape (omit optional fields when unknown; do NOT invent):

```json
{
  "task_text": "short single-line description (required)",
  "urgency": "low | medium | high",
  "project": "slug, if obvious or explicit",
  "contract": "contract slug the task serves, if known",
  "due_date": "YYYY-MM-DD, only if the user mentioned a date",
  "summary": "only if the user asked for a linked note",
  "proposed_tags": ["projects/work-buddy/systems/task-system", "admin/uhn"]
}
```

Reason about both project assignment and free-form namespace tags here, using whatever context you have (session, active contract, recent git, recent conversation, current working directory, the task_text itself):

1. **Project**: try to infer one. If a project is obvious (the user named it, the cwd is a project repo, the task is clearly an ECG-paper task, etc.), set `project` to its slug. If you also have a sensible subtree, include the full path as a tag in `proposed_tags` — `projects/<slug>/<area>/<subarea>` matches the in-vault convention better than the bare slug. Skip only when the project is genuinely ambiguous; don't default to skipping.
2. **Namespace tags**: free-form user namespaces (`#admin/uhn`, `#paper/ecg-classifier`, etc.) go in `proposed_tags`.
3. Don't propose `#todo` (auto-added), `#tasker/*` (state metadata), or `#wb/todo`/`#wb/done` (inline-todo markers). Those are managed elsewhere.

The next step enriches your proposal with project-registry checks, existing-subtree lookups, and near-match data against the namespace universe.

## enrich

Auto-run. Calls namespace_suggest.enrich_plan on the plan from the prior step. Returns:
- `suggestions`: ranked existing namespaces relevant to task_text (includes #projects/* tags)
- `tag_status`: per proposed_tag, whether it already exists, and if not, the closest near-matches
- `project_status`: registry-aware project info — `known_projects` (the registered project list), `proposed_slug` (echoed back), `slug_exists` (whether plan.project / the project slug from proposed_tags is in the registry), `near_subtrees` (existing #projects/<slug>/... paths under the proposed slug), `subtree_matches` (did-you-mean ranker output if a full subtree path was proposed)
- `universe_size`: total registered namespaces
You don't call this directly — the conductor does.

## confirm

Agentic step. Using the enriched output:

1. **Project gate**:
   - If `project_status.proposed_slug` is set and `project_status.slug_exists` is true, accept the slug silently — it's a registered project.
   - If `proposed_slug` is set but `slug_exists` is false, the agent is about to mint a new project. Ask: is this a real new project (then call `project_create` first) or did you mean one of the existing slugs (`project_status.known_projects`)? Do NOT silently call task_create — the slug will be rejected by the registry validation in `_normalize_tags` / `create_task`.
   - If a full subtree path was proposed (`projects/<slug>/<subtree>`) and `near_subtrees` shows existing paths, surface them only when the proposed subtree is novel under an existing project (e.g., proposing `systems/artifacts` when only `systems/knowledge` and `systems/projects` exist). Default-silent when the subtree already exists or when the user clearly named it.
2. **Tag gate**:
   - If all proposed_tags have `tag_status[tag].exists == true`, accept silently.
   - If any proposed_tag has `exists: false`, you're minting a new namespace. Present the tag, its near-matches, and ask: keep as new / use an existing near-match / rename.
3. **Suggestion gate** (lowest-priority): if `suggestions` includes a strong match you hadn't proposed, consider it briefly and surface to the user only if it changes the answer.

Advance with:

```json
{
  "final_plan": {
    "task_text": "...",
    "tags": ["projects/work-buddy/systems/task-system", "admin/uhn"],
    "project": "work-buddy",
    "urgency": "medium",
    "contract": "optional",
    "due_date": "optional",
    "summary": "optional"
  },
  "confirmed": true
}
```

If the user declines (e.g. changed their mind), advance with `{"final_plan": {}, "confirmed": false}`. The apply step will no-op.

## apply

If the confirm step's `confirmed` is false, skip the create call entirely and return `{"success": true, "skipped": true}`.

Otherwise, read the confirm step's `final_plan` and call task_create via the gateway (which handles consent):

```
final_plan = <confirm.final_plan>
params = {"task_text": final_plan["task_text"]}
for k in ("urgency", "project", "due_date", "contract", "summary", "tags"):
    if final_plan.get(k) is not None:
        params[k] = final_plan[k]
result = mcp__work-buddy__wb_run("task_create", params)
```

Return the task_create result as the step output. If task_create returns a consent timeout, the user can approve on any surface — do not retry inside this step; the report step will tell them what happened.

## report

Agentic step. One-line confirmation: task ID and a short paraphrase. Do NOT open the note, do NOT suggest follow-ups unless the user asked. If apply.skipped is true, say the task was not created.
