---
name: Task New
kind: workflow
description: Interactive task creation with independent registered project associations and namespace choices. Plans the task, enriches with registry and namespace context, resolves uncertain new organization, then applies via task_create.
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
      project_ids: list
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
    callable: work_buddy.tasks.capabilities.enrich_plan
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
  name: Resolve uncertain project or namespace choices
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
    - revision
    - receipt
    - document
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
  "project_ids": [12, 34],
  "contract": "contract slug the task serves, if known",
  "due_date": "YYYY-MM-DD, only if the user mentioned a date",
  "summary": "scalar task summary, when useful or requested",
  "proposed_tags": ["work-buddy/task-system", "admin/uhn"]
}
```

The numeric IDs above are illustrative: use actual registered IDs, never invent
them. The optional legacy `project` slug/alias field remains available for
existing single-project callers. Reason about membership and namespaces
independently using the request and available context:

1. **Projects**: use `project_ids` for every intended registered association.
   A task can have none, one, or several. The project registry supplies the IDs;
   a contract slug or repository spelling is context, not an ID.
2. **Namespaces**: user organization such as `admin/uhn` or
   `work-buddy/task-system` goes in `proposed_tags`. Do not generate `projects/`
   paths from project links.
3. Native attention, completion, and lifecycle are separate fields.

The next step enriches the proposal with registered project choices and
near-matches against the namespace universe.

## enrich

Auto-run. Calls the native `work_buddy.tasks.capabilities.enrich_plan` on the plan from the prior step. Returns:
- `suggestions`: ranked existing namespaces relevant to task_text, including any historical projects/ paths still assigned
- `tag_status`: per proposed_tag, whether it already exists, and if not, the closest near-matches
- `project_status`: registry context — `known_projects` includes `project_id`, slug, name, and status. `proposed_slug` and `slug_exists` describe the explicit legacy `plan.project` field; tag spelling does not infer that field. `near_subtrees` and `subtree_matches` retain historical namespace hints only. The task service validates final project IDs at creation.
- `universe_size`: total registered namespaces
You don't call this directly — the conductor does.

## confirm

Agentic step. Using the enriched output:

1. **Project gate**:
   - Accept supported existing project selections without extra prompting. Resolve
     legacy slugs or aliases to registry IDs when preparing the final plan.
   - When a proposed project cannot be uniquely resolved, use the known registry
     choices to resolve the ambiguity. Create a new registry project only when
     the user requested that organization; never substitute a namespace for a link.
   - Preserve all intended associations. Do not choose a primary project merely
     because an older example used a scalar field.
2. **Tag gate**:
   - If all proposed_tags have `tag_status[tag].exists == true`, accept silently.
   - If a proposed_tag has `exists: false` and the user has not already chosen it,
     use near-matches to resolve uncertain organization. Honor explicit new-path
     requests without repeating confirmation. New namespace spelling never
     creates or requires a registered project.
3. **Suggestion gate** (lowest-priority): if `suggestions` includes a strong match you hadn't proposed, consider it briefly and surface to the user only if it changes the answer.

Advance with:

```json
{
  "final_plan": {
    "task_text": "...",
    "tags": ["work-buddy/task-system", "admin/uhn"],
    "project_ids": [12, 34],
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
for k in ("urgency", "project_ids", "project", "due_date", "contract", "summary", "tags"):
    if final_plan.get(k) is not None:
        params[k] = final_plan[k]
result = mcp__work-buddy__wb_run("task_create", params)
```

Return the task_create result as the step output. If task_create returns a consent timeout, the user can approve on any surface — do not retry inside this step; the report step will tell them what happened.

## report

Agentic step. One-line confirmation: native task ID and a short paraphrase. Do not open the Co-work document or suggest follow-ups unless the user asked. If apply.skipped is true, say the task was not created.
