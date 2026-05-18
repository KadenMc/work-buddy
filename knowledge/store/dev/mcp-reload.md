---
name: MCP Registry Reload
kind: directions
description: 'When and how to reload the MCP gateway capability registry. Two paths: heavy full rebuild (mcp_registry_reload) and light per-capability re-probe (recheck_disabled_capability). Picks the right one for the situation.'
summary: Slash command /wb-dev-mcp-reload calls the heavy mcp_registry_reload — only correct after CODE CHANGES to existing capability callables. For transient probe failures (capability disabled despite working tool), use recheck_disabled_capability instead. See architecture/capability-registry for the full picture.
trigger: When the user invokes /wb-dev-mcp-reload or after making code changes to capabilities and needing to reload the registry without a server restart
command: wb-dev-mcp-reload
capabilities:
- mcp_registry_reload
tags:
- dev
- developmental
- mcp
- registry
- reload
- recheck
- cp-a3
- recovery
aliases:
- reload registry
- mcp reload
- reload capabilities
- pick up code changes
- hot reload mcp
parents:
- dev
- dev
---

There are TWO ways to refresh the registry. Pick by situation.

## /wb-dev-mcp-reload — heavy full rebuild

Use when:
  - You changed code inside an existing capability callable AND want the running gateway to pick up the change without a sidecar restart.
  - You suspect the entire registry is stale (rare).

Cost: ~6-8s on a typical machine (re-probes every tool, purges work_buddy.* from sys.modules, rebuilds every capability). Blocks /health for the duration.

Call:
```
mcp__work-buddy__wb_run("mcp_registry_reload")
```

Verify a specific capability is now in the active registry:
```
mcp__work-buddy__wb_run("agent_docs", {"query": "<capability_name>", "depth": "summary"})
```

Note: agent_docs is the right discovery tool here — wb_search is for capability-only discovery and may miss prose units (directions, system, concept, reference, integration, service) that describe the capability.

Caveats:
  - This does NOT register newly-added Capability entries or new workflow names. Those require a sidecar restart.
  - It also does NOT pick up changes to capability *parameter schemas* (e.g., adding a new param to an existing capability) — those are cached by the gateway and require a sidecar restart.
  - If your goal is ONLY to recover a disabled capability (transient tool probe failure), this is the wrong tool — see below.

## recheck_disabled_capability(name) — light per-capability re-probe

Use when:
  - A capability is in the disabled registry because its tool probe transient-failed (e.g. obsidian unreachable for 200ms during sidecar startup) but the underlying tool is now healthy.
  - You want to recover ONE capability without rebuilding the whole registry.

Cost: one tool probe (or zero if within the 30s cool-down). Mutates _REGISTRY in place on success — no sys.modules purge, no rebuild step. Used by the gateway dispatch path (gateway.py) and the sidecar retry sweep (retry_sweep.py).

Call (from Python):
```
from work_buddy.recovery import recheck_disabled_capability
recovered = recheck_disabled_capability("task_create")
# True  — capability is now in the active registry
# False — tool still probing as down; capability stays disabled
```

There is no MCP capability wrapper — it's an internal recovery function called automatically by the dispatch and retry paths. Agents normally don't call it directly; if you find yourself reaching for it, check whether mcp_registry_reload is genuinely what you want first.

## Decision tree

```
Why am I reloading?
  - Code changed (existing callable) ......... mcp_registry_reload
  - New Capability / workflow added .......... sidecar restart (reload alone won't register it)
  - New parameter on existing capability ..... sidecar restart (parameter schema is cached)
  - Capability stuck disabled (transient) .... recheck_disabled_capability(name) — usually automatic
  - Knowledge unit edited .................... agent_docs_rebuild
  - Just want to verify discovery ............ agent_docs(query=...) — no reload needed
```

See architecture/capability-registry for the deep reference.
