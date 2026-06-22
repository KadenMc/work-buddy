---
name: Workflow System
kind: concept
description: Workflow execution — DAG, execution policy, auto-run steps, conductor, step result visibility
summary: A workflow is a kind:workflow knowledge unit — one Markdown file per workflow under knowledge/store/, with the steps DAG in frontmatter and per-step prose under ## step-id body sections. DAG enforces dependency ordering. auto_run steps execute deterministic code automatically. Steps can declare a visibility spec (full/summary/none/auto) controlling what agents see inline vs on-demand via wb_step_result.
tags:
- workflows
- DAG
- execution-policy
- auto-run
- conductor
aliases:
- workflow DAG
- execution policy
- auto_run
- conductor
- workflow steps
parents:
- architecture
- architecture
dev_notes: |-
  ## Initial params — implementation pointers

  - Validation: `_validate_workflow_params(entry, params)` near the top of `work_buddy/mcp_server/conductor.py`. Returns `(ok, error_message)`. Strict policy enforced here — don't loosen without updating `features/user-jobs` and the `architecture/workflows` content above.
  - Source resolution: `_resolve_params_source(source, initial_params)` walks the dotted path. Returns `(found, value)`. Treats `__params__` alone as "whole dict."
  - input_map resolution: `_resolve_input_map(input_map, step_results, initial_params, optional_params)` — pure helper called by `_execute_auto_run`; returns `(kwargs, error)`. Honors schema optionality: an absent top-level `__params__.<key>` whose `<key>` is in `optional_params` is SKIPPED so the callable's own default applies; a *required* / *unknown* / *nested* (`a.b`) miss still errors (fail-loud). `optional_params` is the workflow's `required: false` key set, derived in `_build_response` from `WorkflowDefinition.params_schema`. Unit-tested in `tests/unit/test_conductor_input_map.py`.
  - DAG storage: `start_workflow` sets `dag.initial_params = dict(params or {})` after validation. The attribute is dynamic on the DAG instance (not in `__init__`) — read everywhere via `getattr(dag, 'initial_params', None)` to stay safe against pre-feature DAG instances.
  - Persistence: `WorkflowDAG._save` / `WorkflowDAG.load` (in `workflow.py`) round-trip `initial_params` via `getattr(self, 'initial_params', None)` and `raw.get('initial_params')`. Save files written before this feature simply restore as `None`; everything downstream tolerates that.

  ## Forward-compat warning

  If instruction-text template substitution (`{{params.foo}}`) is ever added, it becomes the third surface where params reach a step (alongside `input_map` and `initial_params` in the response). The substitution mechanism, validation rules, and error semantics must stay consistent with both existing paths — the agent should not see different validation behavior depending on which surface they read from. Prefer extending `_resolve_params_source` (so the dotted-walk semantics match) rather than writing a parallel resolver.

  ## Sidecar executor

  `work_buddy/sidecar/dispatch/executor.py::_execute_workflow(name, params)` forwards to `start_workflow(name, params=params or None)`. Job files (.md frontmatter) carry `params: {...}` under either capability or workflow types; `create_user_job_file` writes it for both. The whole pipeline is exercised by `tests/unit/test_workflow_params.py`.
---

A workflow is a `kind: workflow` knowledge unit — one Markdown file per workflow under `knowledge/store/`, with the `steps` DAG in YAML frontmatter and each step's prose under a `## <step-id>` body section. Each unit carries:
- workflow_name: registry slug
- execution: main | subagent (default policy)
- steps: [{id, name, step_type, depends_on, auto_run, optional, execution, ...}]
- step_instructions: {step_id: instruction text}
- params_schema: {param_name: {type, description, required}} (optional)
- content.full: workflow-level context (philosophy, what-not-to-do)

The conductor reads these at runtime via _discover_workflows_from_store(). Workflows can chain into sub-workflows via workflow_ref. Steps with auto_run specs are executed by the conductor automatically.

## Auto-Run Steps

Workflow steps can be marked `auto_run` in the unit's frontmatter. The conductor executes these transparently in a subprocess — the agent never sees them as "current." Results are stored in the DAG and delivered to the agent in `step_results`.

```yaml
steps:
  - id: load-config
    name: Load morning config
    step_type: code
    auto_run:
      callable: work_buddy.morning.get_morning_config  # dotted import path
      kwargs: {}              # optional static keyword args
      input_map: {}           # optional: {kwarg_name: step_id} wires prior results into kwargs
      timeout: 30             # seconds (default 30)
      retry_on_timeout: true  # default true; set false for non-idempotent steps
```

**When to use auto_run:** The step is deterministic, has no side effects, needs no agent reasoning, and produces data consumed by later steps. Examples: config loading, phase resolution, data formatting.

**When NOT to use auto_run:** The step requires LLM reasoning, user interaction, consent, or calls external services that may fail and need agent-mediated recovery.

**Safety:** Only `work_buddy.*` import paths are allowed. Failed auto_run steps are marked FAILED with the error surfaced to the agent. A 30s default timeout prevents runaway calls.

**Transient-timeout retry:** Subprocess timeouts (`subprocess.TimeoutExpired`) are usually transient — cold imports, concurrent registry rebuilds, antivirus scans. The conductor retries the subprocess once before failing the step, so a single contended host doesn't surface a flake to the agent. Each attempt gets the full `timeout`; worst-case wall time is two attempts. Crashes and invalid-JSON failures never retry — they signal real bugs. Set `retry_on_timeout: false` for steps that mutate external state (git commits, outbound message sends, source-pipeline drives) where a second attempt would not be idempotent.

**Data threading:** Every response from the conductor includes `step_results: {step_id: result}` — a map of all completed step results (auto_run and agent-completed). The `input_map` field lets auto_run steps consume upstream results declaratively: `input_map: {cfg: load-config}` passes the `load-config` result as the `cfg` kwarg.

**Implementation:** `_execute_auto_run()` in `conductor.py`. The auto-run loop is in `_build_response()` — it chains consecutive auto_run steps before returning control to the agent.

**Response shape — ledger vs. data.** The conductor's response surface is a tree, not a graph: each piece of data has exactly one canonical home. Specifically:

- `auto_ran` is a *status ledger*. Each entry is `{id, name}` for a successful run, `{id, name, error}` on failure, or `{id, name, skipped, reason}` when an optional step was skipped. The entry never carries the auto_run callable's actual return value.
- `step_results[<step_id>]` is the *data surface*. The auto_run callable's return value lives here, visibility-filtered. Read from this dict, not from the ledger.
- `prior_step` (in `wb_advance` responses) is a *pointer* — `{id: <just_completed_step_id>}`. The result lives in `step_results[prior_step.id]`. The pointer exists for continuity; the data is not duplicated.

This was not always true: an earlier conductor emitted the data both inside `auto_ran[*].result` and in `step_results`, plus inside `prior_step.result` and in `step_results`. The duplication silently inflated every workflow response that involved auto_run steps or advance calls. The single-canonical-home rule is now a test invariant (see `tests/unit/test_conductor_response_invariants.py`).

## Step results are deltas, not accumulations

Each reasoning step's `result` should be **only its new contribution** — the fields the step itself computed — not the running accumulation of every prior step's output. Upstream data already lives in `step_results.<upstream_id>`; downstream steps read from there directly. Re-emitting the upstream values under your own keys wastes tokens for no information gain and silently inflates response size as the workflow progresses.

**Wrong shape** — each step echoes the prior step's whole dict plus its delta:

```yaml
step_results:
  categorize: { items: [...], categories: [...] }
  rank:       { items: [...], categories: [...], ranks: [...] }
  summarize:  { items: [...], categories: [...], ranks: [...], summary: "..." }
```

The same `items` list appears three times. The same `categories` list appears twice.

**Right shape** — each step returns just its delta:

```yaml
step_results:
  categorize: { categories: [...] }
  rank:       { ranks: [...] }
  summarize:  { summary: "..." }
```

Three distinct values. Total response size = sum of three deltas, not (delta_n × n).

If a downstream step legitimately needs to *modify* an upstream value (annotate it, filter it, transform it), use a **new key** for the modified version (`annotated_items` rather than `items`). That keeps the dataflow explicit and lets the canonical-home rule from the section above keep holding.

**Detection.** The conductor logs a `WARN` when a just-completed step's result is a key-by-key superset of any prior step's result (see `_warn_if_accumulating` in `conductor.py`). The same logic backs a `assert_no_contained_subtrees` test invariant in `tests/unit/test_conductor_response_invariants.py`. Either fires when a workflow author lands the wrong shape.

## Caller-provided initial params

Workflows can declare a `params_schema` (mirrors `Capability.parameters`) and accept caller-provided params at start:

```json
"params_schema": {
    "project_id": {"type": "str", "description": "Project slug", "required": true},
    "depth":      {"type": "int", "description": "How many levels to walk"}
}
```

Caller passes them through any of the standard surfaces (`wb_run(name, params)`, the gateway's retry path, the sidecar executor's `_execute_workflow(name, params)`). Validation is strict: workflows with no schema reject any non-empty params; workflows with a schema reject calls missing required keys or supplying unknown keys. Validation errors return `{"error": "Missing required ..." | "Unknown param(s) ..."}` and the workflow does NOT start.

Validated params reach steps via two paths:

- **`auto_run` steps via `input_map`** — use the synthetic source key `__params__` (whole dict) or `__params__.foo` / `__params__.a.b` (dotted-key walk) to wire a param into a kwarg. There are two ways to handle optional params, and the schema's `required` flag is authoritative for both:
  - **Dotted, schema-optional** — `input_map: {target: __params__.target}` where `target` is declared `required: false`. If the caller omits it, the resolver **skips the kwarg** so the callable's own default applies (it does NOT error). A missing key that is *required*, *not declared*, or *nested* (`a.b`) still fails like an unresolved step source. This lets a step wire one named param directly while still working when the caller omits it.
  - **Whole-dict** — `input_map: {params: __params__}` passes the entire (possibly empty) dict; the callable destructures and defaults internally. Use when a step consumes several params.
  ```json
  "input_map": {"project_id": "__params__.project_id"}
  ```
  (Resolution lives in `_resolve_input_map` / `_execute_auto_run` in `conductor.py`; the `workflow_delegation_resolution` validator check flags a nested `wb_run("W", {...})` delegation that passes a key `W` doesn't declare — a caller/callee contract mismatch — before it can fail at runtime.)
- **Reasoning steps via the first-step response** — the response includes an `initial_params` field alongside `workflow_context`, so the agent reading the first instruction can inspect what was passed in. There is no `{{params.foo}}` template substitution into instruction text — agents read params from the response payload.

Workflows are authored / edited through the `docs_edit` workflow — you edit the unit's `.md` directly (frontmatter `steps` and `params_schema`, plus the `## <step-id>` body sections), and the commit step validates the step DAG (cycles, dangling deps) and reconciles the store + index.

## Workflow-level blanket consent

Starting a workflow grants blanket consent for all its steps (grant_workflow_consent). The blanket is revoked when the workflow completes; a step can opt out with requires_individual_consent: true (which suspends it for that step only); and an orphaned blanket — one left live in a session's consent.db after an MCP-server restart wiped the conductor's in-memory run map — is reconciled away when that session next re-registers (reconcile_workflow_consent, called from the gateway's _register_session). The 3h default TTL is only a backstop. Steps need not manage any of this — the conductor handles it.

## DAG resilience

- Step results over 50K chars are capped with a summary (_cap_step_results)
- Timed-out auto_run steps produce timeout_recovery hints with re-poll instructions
- Smart trimming: only relevant predecessor results are sent to each step (not all results)

## Terminal response types

A workflow ends in one of two terminal envelopes, returned by `start_workflow` / `advance_workflow` / `wb_advance` when no step is available to run next:

- `type: "workflow_complete"` — every node reached `completed` (or `skipped`). The work was done. Field `progress: "N/N steps completed"`.
- `type: "workflow_blocked"` — a step failed and its descendants are unreachable. Fields: `progress: "<done>/<total> steps completed (blocked: <n> failed)"`, `failed_steps: [<step_id>, …]`, `error: "<first_failed_id>: <first_error_message>"`. The work was not done; the caller needs to act on the failure (retry the workflow, fix the underlying cause, or escalate).

Both states are terminal — the active-run entry is cleaned up and the workflow-run consent grant is revoked. They share `summary`, `step_results`, and `diagram` fields, so non-discriminating consumers (e.g. read-only logs) can use either uniformly; discriminating consumers (agents, dispatchers) should branch on `type`.

`fail_task` cascades downstream `pending` nodes to `blocked` so the embedded diagram and `summary()` markdown agree about why those nodes won't run — no node sits in `pending` once an upstream has failed.

## Slash commands

All slash commands (.claude/commands/wb-*.md) are thin launchers that load behavioral directions from the knowledge store via agent_docs. The slash command is the entry point; the knowledge store directions unit contains the behavioral content; the `kind: workflow` unit contains the DAG structure.

## Reasoning-step instructions and directions binding

A `reasoning` step's behavioral prose normally lives in the **bound directions unit** — the `kind: directions` unit whose `workflow:` frontmatter field targets this workflow — not in the step body. This keeps a single source, so duplicating that prose into `## <step-id>` body sections only invites drift.

A reasoning step is therefore well-formed when it is either (a) covered by such a bound directions unit, or (b) carries its own `## <step-id>` instruction in the workflow body. There is no legitimate *bare* reasoning step: if it has neither, it is either undocumented (write the `## <step-id>` prose) or miscategorized (the work is deterministic → make it a `code`/`auto_run` step).

**The binding is a runtime delivery contract, not just a doc link.** When the conductor serves an instruction-less reasoning step, it resolves the workflow's bound directions unit and delivers that unit's rendered full content as the step's instruction — the same content (and renderer) `agent_docs` produces at `depth="full"`. So form (a) holds on *every* entry path: the slash command, a nested `wb_run("<workflow>")` delegation from inside another workflow, and headless/sidecar runs all reach the bare step with the directions in hand — not only the slash-command path. The binding is precomputed once at registry-build time (`WorkflowDefinition.bound_directions_path`), and the served step carries a `directions_source` pointer naming the delivered unit. Delivery degrades safely: if the unit cannot be rendered, the step falls back to the empty-instruction warning. (Mechanism: `_resolve_bound_directions` in `conductor.py`, `_index_directions_by_workflow` in `registry.py`.)

Three `docs_validate` checks back this contract:
- `workflow_step_consistency` warns on a bare reasoning step **only** when no directions unit binds the workflow (a bound workflow's empty reasoning steps are intentional — their content is delivered at runtime).
- `directions_workflow_resolution` errors when a directions unit's `workflow:` does not resolve to a real `kind: workflow` unit. A dangling binding both defeats the suppression above and leaves the conductor with nothing to deliver, so the link must always point somewhere real (full path, e.g. `tasks/task-me`, not the bare slug).
- `workflow_delegation_resolution` checks nested `wb_run("<workflow>")` delegations between workflows: it errors on a delegation to a non-existent workflow, on a delegation into a workflow whose reasoning steps are bare *and* unbound (runtime delivery cannot rescue what has no bound directions), and on a delegation that passes a param the target workflow does not declare in its `params_schema` (a caller/callee contract mismatch that would be rejected at the param gate).

## Step result visibility

Steps can declare a `visibility` spec that controls what agents see inline vs on-demand. Full results are always in the DAG on disk — visibility only affects the MCP response.

| Mode | Behavior |
|---|---|
| `full` | Complete result returned inline |
| `summary` | Manifest with key names / sizes; optional `include_keys` for partial data |
| `none` | Bare status card; no data returned inline |
| `auto` | Default: `full` if ≤10KB, else `summary` |

Agents retrieve elided data on demand via `wb_step_result(workflow_run_id, step_id, key?)`. When a step result shows `_manifest: true`, data is available on demand without cluttering the response.

Declare visibility in the step's dict in the workflow unit's frontmatter:

```
"visibility": {"mode": "none"}
"visibility": {"mode": "summary", "include_keys": ["total", "items"]}
```

**How to pick the right mode.** Pick `summary` *only* when `include_keys` will list a small subset (1–3 keys) of the result. The rule of thumb: if you'd list 80% or more of the result's keys, you actually want `full` (or `auto` if size varies) — `summary` with all keys whitelisted is functionally identical to `full` but written via a longer code path, and the manifest envelope is wasted overhead. `summary` is for the case where the agent needs `total` and `count` inline but the 10MB `items` array can stay behind a manifest.

Decision rules:

- **All keys small + always wanted** → `full`. (Result fits inline; agent always wants every field.)
- **Variable / size-dependent** → `auto` (the default). The conductor picks `full` when the result serializes ≤ 10KB, manifest-only otherwise.
- **One huge optional key surrounded by small ones** → `summary` with the *small* keys in `include_keys`, omitting the big one. (Agent sees the small fields inline; fetches the big one on demand via `wb_step_result`.)

The keys named in `include_keys` are the literal top-level keys of the dict the auto_run callable returns. There's no schema validation on these — listing a key that doesn't exist on the return is silently a no-op. Read the callable's source before authoring the visibility block.


## Result schema (step output validation)

Reasoning steps can declare a `result_schema` that validates the agent's `wb_advance` result before the step is marked complete. Supported fields:

```
"result_schema": {
  "required_keys": ["units_read", "files_read"],
  "key_types":     {"units_read": "list", "files_read": "list"},
  "min_items":     {"units_read": 1, "files_read": 1}
}
```

- `required_keys` — every key must be present on the result dict; missing keys fail validation.
- `key_types` — each listed key must be of the named Python type (`str`, `int`, `float`, `bool`, `list`, `dict`).
- `min_items` — each listed key's value must have `len(value) >= min_count`. Applies to lists, dicts, and strings. Useful when a step exists specifically to produce content (e.g., a citation list) and empty submission would bypass the step's intent.

Validation failures return `type: "validation_error"` with a specific message; the step stays in progress until the agent advances with a conformant result. When the result is an empty dict — the shape the conductor receives when an agent passes no `step_result` at all, or names the kwarg incorrectly (e.g. `result=`, which FastMCP silently drops) — the `error` and `hint` both name `step_result` explicitly instead of the generic dict-shape framing. See `work_buddy.mcp_server.conductor._validate_step_result`.
