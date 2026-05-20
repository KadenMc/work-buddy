---
name: Capability Registry
kind: concept
description: How capabilities are registered, probed for tool availability, disabled when a probe fails, and recovered cheaply via per-capability re-probe (CP-A3) instead of a full registry rebuild. Authoritative reference for the heavy-vs-light recovery decision.
summary: 'The registry has two maps: _REGISTRY (active) and _DISABLED_REGISTRY (probe failed at build time). Two recovery paths: (1) heavy mcp_registry_reload (~6-8s, full rebuild, sys.modules purge); (2) light recheck_disabled_capability(name) (per-tool, 30s cool-down, in-place restore). Use (2) for transient probe failures; (1) only when code changed.'
tags:
- capability
- registry
- tool-probe
- disabled
- recovery
- cp-a3
- per-capability
- reload
- architecture
aliases:
- registry
- capability registry
- tool probes
- disabled capability
- recover capability
- reload capability
- recheck capability
- registry recovery
- DISABLED_CAPABILITIES
- recheck_disabled_capability
parents:
- architecture
---

## What

The capability registry (`work_buddy/mcp_server/registry.py`) holds two maps: `_REGISTRY` (active capabilities, directly callable via the MCP gateway) and `_DISABLED_REGISTRY` (capabilities whose `requires=[...]` tool probe failed at build time, stashed but not callable). At build-time, the registry filter pass moves any capability whose tool probe failed from `_REGISTRY` to `_DISABLED_REGISTRY` and adds a row to `work_buddy.tools.DISABLED_CAPABILITIES` listing the missing tools.

Disabled state is cached. A capability disabled by a transient probe failure (e.g. obsidian unreachable for 200ms during sidecar startup) stays disabled until something explicitly re-probes it.

## Capability schema (selected fields)

- `name`, `description`, `parameters`, `callable` — the dispatch surface.
- `requires: list[str]` — tool IDs the dispatcher gates on.
- `mutates_state: bool`, `retry_policy: "manual" | "replay" | "verify_first"` — inform the gateway's auto-enqueue policy.
- `consent_operations: list[str]` — declarations for the gateway's pre-flight consent bundling.
- `op_id: str | None` — set when the capability was resolved from an inert declaration rather than instantiated directly (see "Declaration-based capabilities" below); None for directly-registered capabilities.
- `effects: list[EffectSpec]` — manifest of externally-visible effects for capabilities that produce more than one. When non-empty, the post-write-verify recovery path uses `verify_post_write_effects` (walks every declared effect; can return `partial`) instead of single-effect verify. Capabilities with declared effects MUST be idempotent under retry. Schema lives at `work_buddy.obsidian.effects.EffectSpec`; recovery semantics in `architecture/retry-queue`.

## Two recovery paths — use the right one

### Per-capability re-probe (preferred for runtime recovery)

**`work_buddy.recovery.recheck_disabled_capability(name, *, force=False)`** re-probes ONLY the capability's missing tools, with per-tool cool-down (default 30s, env-overridable via `WB_RECHECK_COOLDOWN_SECS`), single `_RECOVERY_LOCK` (RLock) for concurrent-caller safety. On success, mutates `_REGISTRY` in place to restore the capability. No rebuild, no module purge.

Returns `True` if the capability is now in the live registry, `False` if it remains disabled (with `DISABLED_CAPABILITIES[name]` updated to reflect any partially-recovered tools).

Companion: **`recheck_tool(tool_id, *, force=False)`** for re-probing a single tool without scoping to a capability. Same cool-down, same lock.

Used by:
- The gateway's wb_run dispatch path (`work_buddy/mcp_server/tools/gateway.py`). On hitting a disabled capability, the gateway calls `recheck_disabled_capability` before returning the disabled-error.
- The sidecar's retry sweep `_replay` (`work_buddy/sidecar/retry_sweep.py`). On hitting a disabled capability during a queued retry, the sweep calls `recheck_disabled_capability` rather than reporting "not found in registry". Falls back to invoking the disabled entry's callable when recheck still says no, since the bridge call inside raises a typed transient exception and the operation re-queues correctly.

### Full registry rebuild (only when code changed)

**`mcp_registry_reload`** capability (calls `invalidate_registry()` in `registry.py`) clears `_REGISTRY` AND purges all `work_buddy.*` from `sys.modules` so the next `get_registry()` rebuilds from current source. Costs ~6–8 seconds (tool_probes ~5s + capability registration ~2s) on a typical machine and re-probes every tool.

Use only when:
- You changed capability code mid-session and want the running gateway to pick up the change without a sidecar restart
- You suspect the entire registry is stale (e.g. a workflow / knowledge unit was added)

**Do NOT use `mcp_registry_reload` for transient probe failures.** It's a sledgehammer where a per-capability re-probe is the right tool. The 8-second rebuild blocks `/health` and slows down the gateway for the duration.

## Decision tree

```
Capability is disabled / not found in active registry
  - Code changed -> mcp_registry_reload
  - Probe transient-failed -> recheck_disabled_capability(name)
      - Returns True -> capability is back in _REGISTRY, proceed
      - Returns False -> tools still down
          - Caller wants to retry later -> re-queue / surface
          - Caller can run the capability anyway ->
              invoke disabled_entry.callable(...)
              (typed bridge exception -> @bridge_retry handles)
```

## Declaration-based capabilities

Not every capability is a `Capability(...)` instance in `registry.py`. A capability can also be an inert **declaration** in the knowledge store that names an **Op** (a callable registered by ID in the Op registry). The capability loader resolves declarations at registry-build time and merges the resulting `Capability` objects into `_REGISTRY` alongside the directly-registered ones — a declared capability is indistinguishable at dispatch time except for its `op_id` field. See `architecture/data-first-capabilities` for the Op registry, the loader, and load-time validation.

## Key files

- `work_buddy/mcp_server/registry.py` — `_REGISTRY`, `_DISABLED_REGISTRY`, `Capability` dataclass, `get_registry`, `get_disabled_registry`, `invalidate_registry`
- `work_buddy/mcp_server/op_registry.py` — Op registry backing declaration-based capabilities (see `architecture/data-first-capabilities`)
- `work_buddy/knowledge/capability_loader.py` — resolves capability declarations against the Op registry
- `work_buddy/recovery.py` — `recheck_disabled_capability`, `recheck_tool`, `_RECOVERY_LOCK`, `_LAST_RECHECK_AT`
- `work_buddy/obsidian/effects.py` — `EffectSpec` schema for the `Capability.effects` manifest
- `work_buddy/obsidian/post_write_verify.py` — `verify_post_write_effects` walker
- `work_buddy/tools/__init__.py` — `DISABLED_CAPABILITIES`, `is_tool_available`, `reprobe_one`
- `work_buddy/mcp_server/tools/gateway.py` — dispatch path with effects-aware PWU handler
- `work_buddy/sidecar/retry_sweep.py` — sweep path with disabled-cap recovery and effects-aware pre-verify

## When in doubt

Per-capability is almost always right at runtime. Full reload is for code changes, full inventory rebuilds, or panic-mode diagnosis. The 30s cool-down on per-capability is your friend — it stops aggressive callers from hammering a genuinely-down tool.
