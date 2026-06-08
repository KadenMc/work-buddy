---
name: MCP Registry Reload
kind: directions
description: 'When and how to refresh the MCP gateway capability registry. Pick by what changed: DATA (declarations / workflows / param schemas) → reload_capability_data, no restart; CODE (Op bodies, new Op modules, gateway functions) → Ctrl+R restart; transient tool-probe failure → recheck_disabled_capability.'
summary: 'reload_capability_data rebuilds the registry from fresh store data WITHOUT a sys.modules purge, so edited/added declarations, new workflows, and param-schema changes go live with no restart. Code changes (new/edited Ops, gateway functions) still need a Ctrl+R restart. The old heavy mcp_registry_reload was retired — it purged sys.modules but silently did nothing in the long-lived FastMCP gateway. See architecture/capability-registry.'
trigger: When the user invokes /wb-dev-mcp-reload, or after editing a capability declaration / workflow / parameter schema and needing it live without a server restart
command: wb-dev-mcp-reload
capabilities:
- reload_capability_data
tags:
- dev
- developmental
- mcp
- registry
- reload
- recheck
- recovery
aliases:
- reload registry
- mcp reload
- reload capabilities
- data-only reload
- reload declarations
- refresh capabilities without restart
- hot reload mcp
parents:
- dev
---

Pick the refresh path by **what changed** — data, code, or a transient probe.

## reload_capability_data — data changes (no restart)

Use when you edited or added a capability **declaration** (including its parameter schema) or a **workflow** unit, and want it live without a restart.

```
mcp__work-buddy__wb_run("reload_capability_data")
```

It resets the knowledge-store cache, clears `_REGISTRY`, and rebuilds in place — **without** purging `sys.modules`. Because no module is re-imported, `Capability` / `WorkflowDefinition` class identity stays stable and the long-lived FastMCP gateway reads the rebuilt registry directly. Cost: ~6–8s (it re-probes every tool as part of the rebuild); blocks `/health` for the duration.

Goes live with no restart:
- A new or edited capability **declaration** whose Op is already registered.
- A changed **parameter schema** on an existing capability.
- A new or edited **workflow** unit.
- Re-enabling a capability whose tool just came back (the rebuild re-probes + re-runs the requirements filter).

Verify a specific capability is now in the active registry:
```
mcp__work-buddy__wb_run("agent_docs", {"query": "<capability_name>", "depth": "summary"})
```
(agent_docs is the right discovery tool here — wb_search is capability-only and may miss prose units.)

## Restart (Ctrl+R) — code changes

`reload_capability_data` refreshes **data**, not **code**. Re-importing Python is what a process restart does safely, so a restart is required when you change:
- an Op's Python **body** (the callable a declaration points at),
- a **brand-new Op module**,
- `wb_run` / `wb_search` or any gateway function frozen by FastMCP at boot.

In Claude Code Desktop that's **Ctrl+R**. (For sidecar-hosted code, a sidecar reset re-reads code at startup.) See `dev/live-testing-directions` for the two-process model — which restart picks up what.

## recheck_disabled_capability(name) — transient probe failure

Use when a capability is in the disabled registry because its tool probe transient-failed (e.g. obsidian unreachable for 200ms during startup) but the underlying tool is now healthy, and you want to recover ONE capability without rebuilding.

```
from work_buddy.recovery import recheck_disabled_capability
recovered = recheck_disabled_capability("task_create")
# True  — capability is now in the active registry
# False — tool still probing as down; capability stays disabled
```

Cost: one tool probe (or zero within the 30s cool-down). Mutates `_REGISTRY` in place — no purge, no rebuild. There is no MCP wrapper; the gateway dispatch path and the sidecar retry sweep call it automatically, so agents rarely call it directly.

## Decision tree

```
What changed?
  - Declaration / workflow / parameter schema ... reload_capability_data (no restart)
  - Op code / new Op module / gateway function .. Ctrl+R restart (re-imports Python)
  - Capability stuck disabled (transient probe) . recheck_disabled_capability(name) — usually automatic
  - Knowledge unit edited (prose only) ......... agent_docs_rebuild
  - Just want to verify discovery .............. agent_docs(query=...) — no reload needed
```

## Retired: mcp_registry_reload

The heavy `mcp_registry_reload` was **retired from the agent surface**. It purged `work_buddy.*` from `sys.modules` to pick up code — but in the long-lived FastMCP gateway that silently did nothing: `wb_run` / `wb_search` are frozen against the boot module generation, so the rebuilt registry never reached dispatch, while the purge corrupted class identity (returning `{"status": "ok"}` while changing nothing). The underlying function `invalidate_registry()` still exists, dormant. For data changes use `reload_capability_data`; for code changes restart. Full background: `architecture/capability-registry` and `.data/designs/mcp-registry-reload`.
