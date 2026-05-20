---
name: Development Mode
kind: directions
description: Enter developmental agent mode — orient on architecture, key locations, and dev workflow for modifying work-buddy itself
summary: Developmental mode — modify the work-buddy codebase itself. Always run the dev-orient workflow before writing any code.
trigger: When the user invokes /wb-dev, asks to build something in work-buddy, or needs to add/fix a capability, workflow, service, or dashboard feature
command: wb-dev
workflow: dev/dev-orient
capabilities:
- mcp_registry_reload
- dev_mode_toggle
tags:
- dev
- developmental
- architecture
- mcp
- capabilities
- workflows
- services
- dashboard
aliases:
- enter dev mode
- development agent
- build work-buddy
- modify codebase
- developer orientation
- add capability
- add workflow
- add service
- orient for dev
- dev orientation
- dev-mode orient
- how to add workflow
- how to edit workflow
- doc hygiene
parents:
- dev
- dev
dev_notes: Added 'Health system' subsection under 'Key locations' that points at the new architecture/health namespace. Future devs touching components / requirements / fixers should land on the four-layer model first.
---

You are a **developmental agent**, not an operational one. Your job is to modify the work-buddy codebase itself — add capabilities, fix bugs, improve workflows, extend services. You are not running morning routines or triaging tasks.

## Step 1 — Orient before anything else

Start the orientation workflow:

```
mcp__work-buddy__wb_run("dev-orient")
```

This activates dev mode, forces you to search the knowledge store for the relevant subsystem, read the actual code, and declare what prior art you found — all before writing a single line. Do not skip it. The workflow's output is a structured record (three lists: units read, files read, wrappers found) that stays visible to the user, so "I already know this area" is not a valid reason to bypass.

If any list you advance with is empty or trivial, you have not oriented yet — go deeper and try again.

## Architectural constructs (reference)

Three core constructs, with design heuristics for deciding between them:

- **Capabilities** — atomic Python functions registered in `registry.py`. Single operation, reusable from anywhere. Invoked via `wb_run("name", params)`, executes immediately.
- **Workflows** — DAG definitions in `knowledge/store/workflows.json` as `WorkflowUnit` entries. Multi-step procedures requiring ordering, user decisions, or state threading. Started via `wb_run("name")`, advanced via `wb_advance(run_id, result)`.
- **Auto-run steps** — workflow steps marked `auto_run` in `workflows.json`. The conductor executes these transparently; the agent never sees them. Use for deterministic code (config loading, data formatting) that needs no agent reasoning.

**Decision heuristic.** Can you write a unit test with a fixed expected output? → Capability. Does the "correct" output depend on interpretation, user input, or synthesis? → Workflow step. Is the step itself deterministic with no side effects? → Auto-run step.

For gateway design tenets (Progressive Disclosure, Just-in-Time Retrieval, etc.), see the `dev/design-tenets` knowledge unit.

For the MCP import discipline (asyncio deadlock hazard), see `architecture/mcp-import-discipline`.

## Key locations

### MCP capability registry
`work_buddy/mcp_server/registry.py` — **all** MCP capabilities are registered here. Each category has a builder function (`_task_capabilities()`, `_sidecar_capabilities()`, etc.) returning a list of `Capability` dataclass instances.

**To add a capability:**
1. Write the function inside the relevant `_*_capabilities()` builder.
2. Add a `Capability(name=..., callable=..., ...)` to the returned list.
3. **Restart the MCP server** to register the new entry in the tool dispatcher. `mcp_registry_reload` alone picks up code changes inside *existing* capability callables but does NOT register new `Capability` entries — the gateway holds a stale reference to the registry module.
4. Verify: `mcp__work-buddy__wb_search("your_capability")`.

### Workflows
Workflow units live in `knowledge/store/workflows.json`. The conductor (`work_buddy/mcp_server/conductor.py`) loads them at runtime via `_discover_workflows_from_store()`, which scans every store file for `kind == "workflow"` — colocation in `workflows.json` is convention, not a requirement, but `workflow_create` routes there automatically.

**To add a workflow:**
1. Use `mcp__work-buddy__wb_run("workflow_create", {path, name, description, workflow_name, steps, step_instructions, ...})` — pass `steps` and `step_instructions` as JSON strings. Do NOT hand-edit `workflows.json`; DAG validation, parent-child reconciliation, and cache invalidation live inside the capability.
2. Create a matching slash command in `.claude/commands/wb-<name>.md` (thin launcher).
3. Create a behavioral directions unit via `docs_create` (kind="directions"), loaded by the slash command.
4. Update CLAUDE.md if the workflow belongs in a user-facing table.
5. **Restart the MCP server** — new workflow names require a full restart to be callable via `wb_run`; `mcp_registry_reload` does not suffice.

**To edit an existing workflow:** use `workflow_update`. Pass only the fields that change. `steps` replaces the DAG; `step_instructions` merges (keys you pass overwrite, keys you omit are preserved — pass the whole dict to replace cleanly).

### Knowledge units (prose: directions, system)
Use `docs_create` / `docs_update` / `docs_delete`. Never hand-edit `knowledge/store/*.json` — those capabilities run DAG validation, maintain parent-child symmetry, and invalidate the cache. `dev_notes` is a first-class parameter on both `docs_create` and `docs_update` (and `workflow_create` / `workflow_update`) — use that, not a direct JSON edit.

**Capability units** in `_generated_capabilities.json` are auto-generated from `registry.py` by `python -m work_buddy.knowledge.build --write`. Do not try to `docs_create`/`docs_update` them.

### Health system (preferences / requirements / components / fixers)

For "how do I add a new component / requirement / fixer / wizard check" — read [architecture/health](architecture/health) first. It frames the four-layer model (do I want this? / is it set up? / is it running? / how do I repair it?) and links to per-layer references.

Quick map:

- **Component** — `work_buddy/health/components.py`. Register a `ComponentDef`. Runtime health-check functions live in `work_buddy/health/checks.py`. See [architecture/health/components](architecture/health/components).
- **Requirement** — `work_buddy/health/requirements.py`. Register a `RequirementDef`. Requirement check functions live in `work_buddy/health/requirement_checks.py` (no HTTP, no service pings — delegate to a `checks.py` helper if you need a runtime probe). See [architecture/health/requirements](architecture/health/requirements).
- **Fixer** — `work_buddy/health/fixers.py`. Wire it via the requirement's `fix_kind` (`programmatic` / `input_required` / `agent_handoff`) and matching `fix_fn` / `fix_params` / `fix_agent_brief`. See [architecture/health/fixers](architecture/health/fixers).
- **Preferences** — `work_buddy/health/preferences.py` plus `config.local.yaml` `features.<id>.{wanted, reason}`. Mostly automatic when a component is non-core. Behavioral guidance for agents: [features/preferences](features/preferences).

The Settings tab UI picks up new components automatically via the control graph; you don't need to touch the dashboard frontend. After registering: `mcp_registry_reload` is sufficient (no new `Capability` is added). For the unified view-model + cascade rules + endpoint surface, see [architecture/control-graph](architecture/control-graph).

### Doc hygiene after changes
Run `/wb-dev-document` before committing. It scans current changes against the knowledge store, proposes edits for stale units (and creates new ones where needed), and applies them via the sanctioned capabilities. Doc drift is a recurring failure mode; this workflow makes the check a DAG step that cannot be silently skipped.

### Slash commands
`.claude/commands/wb-*.md` — thin launchers that load behavioral directions from the knowledge store via `agent_docs`. Behavioral guidance goes in the knowledge store directions unit, not in workflow step instructions (see the priming hazard note under `dev/design-tenets`).

### Sidecar services
Each service follows the pattern: `work_buddy/<service>/service.py` with Flask app, `/health` endpoint, and `main()` entry point. Configured in `config.yaml` under `sidecar.services`.

**To add a service:**
1. Create `work_buddy/<name>/` with `__init__.py`, `__main__.py`, `service.py`.
2. Add entry to `config.yaml` under `sidecar.services`.
3. Service must have `GET /health` returning `{"status": "ok"}`.
4. Sidecar auto-starts, health-checks, and restarts it.

### Dashboard
`work_buddy/dashboard/` — web UI on port 5127. See `services/dashboard` knowledge unit for the tab-adding pattern.

## Dev workflow

### Running Python
```
powershell.exe -Command "cd <vault-root>\repos\work-buddy; conda activate work-buddy; <command>"
```
(Platform-neutral users: your own activation path — this invocation is Windows/conda-specific.)

### Testing capabilities
```
mcp__work-buddy__wb_run("mcp_registry_reload")        # pick up code changes inside existing callables
mcp__work-buddy__wb_search("your_query")              # verify discovery
mcp__work-buddy__wb_run("capability_name", {...})     # test execution
```

**Caveat:** `mcp_registry_reload` does NOT register newly added `Capability` entries or newly added workflow names. For either, restart the MCP server. The reload is a hot-patch for code-inside-existing-callables only.

### Restarting services
```
mcp__work-buddy__wb_run("service_restart", {"service": "dashboard"})
```

### Dependencies
**Never use `pip install`.** Use Poetry:
- Production: `poetry add <package>`
- Temporary/testing: `poetry add --group temp <package>` (cleanly removable)

### Committing
work-buddy enforces a Developer Certificate of Origin: **every commit must be signed off** with `git commit -s`, which appends a `Signed-off-by` trailer. The `DCO` status check is required on `main` — a pull request with any unsigned commit cannot merge. `/wb-dev-pr` signs off in its commit step; if you commit by hand, always pass `-s`.

## What NOT to do

- **Don't skip the orientation workflow.** Every documented failure to orient has produced wrong code. You are the next data point if you skip.
- **Don't run operational workflows** — you're here to build, not to operate.
- **Don't guess at imports** — `mcp__work-buddy__wb_search()` first, then check the code.
- **Don't add features without slash commands** — every user-facing capability needs one.
- **Don't forget doc hygiene** — run `/wb-dev-document` before `/wb-dev-pr`.
- **Don't hand-edit `knowledge/store/*.json`** — use `docs_*` / `workflow_*` / the auto-generator for capability units.
- **Don't commit unrelated files** — stage only what you changed.
- **Don't commit without `-s`** — work-buddy enforces a DCO; an unsigned commit fails the required `DCO` check and blocks the PR.
- **Don't ship transient narrative in durable surfaces.** See `<<wb:dev/durable-surfaces>>`.
