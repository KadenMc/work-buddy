---
name: Development Mode
kind: directions
description: Enter developmental agent mode — orient on architecture, key locations, and dev workflow for modifying work-buddy itself
summary: Developmental mode — modify the work-buddy codebase itself. Always run the dev-orient workflow before writing any code.
trigger: When the user invokes /wb-dev, asks to build something in work-buddy, or needs to add/fix a capability, workflow, service, or dashboard feature
command: wb-dev
workflow: dev/dev-orient
capabilities:
- reload_capability_data
- mode_toggle
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
- **Workflows** — `kind: workflow` units, one Markdown file per workflow under `knowledge/store/`. Multi-step procedures requiring ordering, user decisions, or state threading. Started via `wb_run("name")`, advanced via `wb_advance(run_id, result)`.
- **Auto-run steps** — workflow steps marked `auto_run` in the unit's frontmatter. The conductor executes these transparently in a subprocess; the agent never sees them. Use for deterministic code (config loading, data formatting) that needs no agent reasoning.

**Decision heuristic.** Can you write a unit test with a fixed expected output? → Capability. Does the "correct" output depend on interpretation, user input, or synthesis? → Workflow step. Is the step itself deterministic with no side effects? → Auto-run step.

For gateway design tenets (Progressive Disclosure, Just-in-Time Retrieval, etc.), see the `dev/design-tenets` knowledge unit.

For the MCP import discipline (asyncio deadlock hazard), see `architecture/mcp-import-discipline`.

## Key locations

### MCP capabilities (Op + declaration)
A capability is an **Op** (a Python callable) plus a **declaration unit** (a `kind: capability` Markdown unit that names the Op). At build time the registry resolves declarations against the Op registry via `load_declared_capabilities` — there is no `registry.py` `Capability(...)` builder anymore. See `architecture/data-first-capabilities`.

**To add a capability:**
1. Write the callable and register it in the relevant `work_buddy/mcp_server/ops/<domain>_ops.py` with `register_op("op.wb.<name>", fn)`.
2. Author a declaration unit at `knowledge/store/<domain>/<name>.md` (`kind: capability`, with `capability_name`, `op`, `category`, and a `parameters` schema) — via the `docs_edit` workflow.
3. **Restart the MCP server (Ctrl+R)** so the new Op's code is imported and the capability enters the dispatcher. A new Op is new Python — `reload_capability_data` only refreshes *data* (declarations, workflows, and param schemas whose Op already exists), so it cannot pick up a brand-new Op.
4. Verify: `mcp__work-buddy__wb_search("your_capability")`.

### Workflows
A workflow is a `kind: workflow` unit — one Markdown file per workflow under `knowledge/store/`. The conductor (`work_buddy/mcp_server/conductor.py`) discovers them at runtime via `_discover_workflows_from_store()`, which scans every store file for `kind == "workflow"`. The `steps` DAG lives in the unit's YAML frontmatter; each step's prose lives under a `## <step-id>` body section.

**To add a workflow:**
1. Scaffold and author the unit with the `docs_edit` workflow: `mcp__work-buddy__wb_run("docs-edit", {"path": "<domain>/<name>", "create": true, "kind": "workflow"})`, then edit the scaffold's frontmatter `steps` and the `## <step-id>` body sections with your native `Edit` tool. The commit step validates the step DAG (cycles, dangling deps, heading↔step-id consistency) and reconciles.
2. Create a matching slash command in `.claude/commands/wb-<name>.md` (thin launcher) if it's user-facing.
3. Create a behavioral directions unit (`kind: directions`) via `docs_edit`, loaded by the slash command.
4. Update CLAUDE.md if the workflow belongs in a user-facing table.
5. **Reload with `reload_capability_data`** — a new workflow is data, so the data-only reload makes it callable via `wb_run` with no restart (a restart also works but isn't needed).

**To edit an existing workflow:** use `docs_edit` and edit the unit's `.md` directly — frontmatter `steps` (the DAG) and the `## <step-id>` body sections. The commit step re-validates the DAG.

### Knowledge units (any kind)
The system store is one Markdown file per unit (`knowledge/store/<path>.md`) — editing a unit is editing its file. Use the **`docs_edit` workflow** (`wb_run("docs-edit", {"path": ...})`): it returns the file path, you edit it with your native `Edit` tool, and the commit step validates (kind-aware) and reconciles the store cache + search index. `create: true` + `kind` scaffolds a new unit. `dev_notes` is just a frontmatter field — edit it inline.

A direct `Edit` of a unit's `.md` is equally valid; if you bypass the workflow, run `agent_docs_rebuild` afterward so the store and index reflect the change. Structural operations that aren't content edits — deleting or moving a unit — use the `docs_delete` / `docs_move` capabilities. **Capability units** (`kind: capability`) are authored the same way (see "MCP capabilities" above for the Op + declaration pair).

### Health system (preferences / requirements / components / fixers)

For "how do I add a new component / requirement / fixer / wizard check" — read [architecture/health](architecture/health) first. It frames the four-layer model (do I want this? / is it set up? / is it running? / how do I repair it?) and links to per-layer references.

Quick map:

- **Component** — `work_buddy/health/components.py`. Register a `ComponentDef`. Runtime health-check functions live in `work_buddy/health/checks.py`. See [architecture/health/components](architecture/health/components).
- **Requirement** — `work_buddy/health/requirements.py`. Register a `RequirementDef`. Requirement check functions live in `work_buddy/health/requirement_checks.py` (no HTTP, no service pings — delegate to a `checks.py` helper if you need a runtime probe). See [architecture/health/requirements](architecture/health/requirements).
- **Fixer** — `work_buddy/health/fixers.py`. Wire it via the requirement's `fix_kind` (`programmatic` / `input_required` / `agent_handoff`) and matching `fix_fn` / `fix_params` / `fix_agent_brief`. See [architecture/health/fixers](architecture/health/fixers).
- **Preferences** — `work_buddy/health/preferences.py` plus `config.local.yaml` `features.<id>.{wanted, reason}`. Mostly automatic when a component is non-core. Behavioral guidance for agents: [features/preferences](features/preferences).

The Settings tab UI picks up new components automatically via the control graph; you don't need to touch the dashboard frontend. A component registration is Python (in `components.py`), so **restart the MCP server (Ctrl+R)** to import it — `reload_capability_data` only refreshes data, not code. For the unified view-model + cascade rules + endpoint surface, see [architecture/control-graph](architecture/control-graph).

### Doc hygiene after changes
`/wb-dev-pr` runs `/wb-dev-document` as a **mandatory chained step** (doc-update sits between the test and PII-scan steps in the `dev-pr` workflow). So committing through `/wb-dev-pr` already keeps the knowledge store in sync — do NOT run `/wb-dev-document` as a separate step first. Run `/wb-dev-document` standalone only when you want to *preview* the proposed doc edits outside the commit flow. It scans current changes against the knowledge store, proposes edits for stale units (and creates new ones where needed), and applies them via the sanctioned capabilities. Doc drift is a recurring failure mode; chaining it into `/wb-dev-pr` makes the check a DAG step that cannot be silently skipped.

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
mcp__work-buddy__wb_run("reload_capability_data")     # pick up declaration / workflow / param-schema edits (data only)
mcp__work-buddy__wb_search("your_query")              # verify discovery
mcp__work-buddy__wb_run("capability_name", {...})     # test execution
```

**Caveat — data vs code:** `reload_capability_data` makes *data* changes live with no restart: new/edited **declarations** (including param schemas) and new **workflows** whose Op already exists. It does NOT pick up edited Op **code** or a brand-new Op **module** — those are Python and need a `Ctrl+R` restart. (The retired `mcp_registry_reload` claimed to hot-patch code but silently did nothing in the long-lived FastMCP gateway — see `dev/mcp-reload`.)

### Restarting services
```
mcp__work-buddy__wb_run("service_restart", {"service": "dashboard"})
```

### Live testing

Run `/wb-dev-live-testing` to drive an end-to-end test of an in-progress change against the running MCP server + sidecar + surfaces. Distinct from `pytest` — unit tests catch logic bugs; live tests catch wiring bugs (FastMCP tool registration, surface dispatchers, sidecar message routing, session-scoped storage). The protocol (precondition → trigger → user action → verify → cleanup) lives in `dev/live-testing-directions`; the slash command loads it. Use after any change that touches gateway entry points, notification surfaces, or session-scoped behavior.

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
- **Don't double-run doc hygiene** — `/wb-dev-pr` already runs `/wb-dev-document` as a chained step, so never tell the user (or yourself) to "run /wb-dev-document then /wb-dev-pr." Run `/wb-dev-document` standalone only to *preview* doc edits before the PR flow.
- **Reconcile after a direct file edit** — a raw `Edit` of a unit's `.md` is fine, but run `agent_docs_rebuild` (or use the `docs_edit` workflow, which does it for you) so the store cache and search index pick up the change.
- **Don't commit unrelated files** — stage only what you changed.
- **Don't commit without `-s`** — work-buddy enforces a DCO; an unsigned commit fails the required `DCO` check and blocks the PR.
- **Don't ship transient narrative in durable surfaces.** See `<<wb:dev/durable-surfaces>>`.
