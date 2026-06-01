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
dev_notes: |-
  ## Stale class identity after sys.modules purge

  `mcp_registry_reload` purges `work_buddy.*` from `sys.modules` and rebuilds the registry.  Any code that captured a class reference — most commonly an exception class used in an `except` clause, or a dataclass used for `isinstance` discrimination — holds onto the *pre-reload* class object.  The freshly-imported class is a different object even when its name, base classes, and shape are identical, so `isinstance(post_reload_instance, captured_pre_reload_class)` returns False.

  Symptoms observed in the activity ledger and reproduced in unit tests:

  - `'Capability' object has no attribute 'execution'` leaking through the gateway's parameter-error path (`gateway.py:1439`, `gateway.py:1524`).  Root cause: `_entry_to_dict` at `registry.py:673-705` discriminated via `isinstance(entry, Capability)`.  Post-reload, that isinstance check fails for actual Capability instances cached anywhere outside the live registry; the function fell through to the WorkflowDefinition branch and accessed `.execution` on what was structurally a Capability.

  - `"Execution failed: ConsentRequired: ConsentRequired: ..."` (double-prefix) in operation error_summary fields for capabilities that raise ConsentRequired manually (e.g. `context_wrappers.py:project_create` at line 944, with a lazy `from work_buddy.consent import ConsentRequired`).  Root cause: the gateway's typed `except ConsentRequired:` at `gateway.py:1535` captures the class object at module-import time.  After a sys.modules purge, the raise site re-imports a fresh class.  The captured reference no longer matches; the exception falls through to the broad `except Exception:` at `gateway.py:1583`, which stringifies via `f"{type(exc).__name__}: {exc}"` — producing a double prefix if the exception's own message also includes a leading `"ConsentRequired:"` token.  `ConsentRequired.__init__` deliberately omits the type-name prefix from its message for exactly this reason.

  ## Defensive patterns

  - **Shape-discrimination via `hasattr` instead of isinstance** for serialization-only paths.  Pattern in `_entry_to_dict`: `if hasattr(entry, "callable") and not hasattr(entry, "steps"):` reads as the Capability branch; the workflow branch is `else`.  Survives any class identity drift, including module reload.

  - **Duck-typed fallback in broad-exception handlers** for typed exceptions whose typed `except` clause may catch the stale reference.  Pattern in the gateway's broad `except Exception:` at `gateway.py:1583`: check `type(exc).__name__ == "ConsentRequired"` and route to the same auto-consent flow the typed handler would have taken.

  - **Drop redundant type-name prefixes from exception messages.**  `ConsentRequired.__init__` no longer prepends `"ConsentRequired: "` to its message; callers that stringify via `f"{type(exc).__name__}: {exc}"` now produce a single clean prefix instead of a double-prefix even when the typed catch is bypassed.

  - **Class identity is structurally fragile across sys.modules purges**; any new typed-exception catch or `isinstance` discrimination on a work_buddy class should consciously decide whether to use a shape-based fallback.  The hazard is not specific to the known cases — it applies anywhere a pre-reload reference is held.

  ## Related tests

  - `tests/unit/test_consent_stale_class_identity.py` demonstrates the bare mechanism (two locally-defined classes with the same name, isinstance fails across them).
  - `tests/unit/test_obsidian_retry_consent.py` covers the consent-propagation behaviour the defensive patterns protect.
  - `tests/unit/test_registry_invariants.py::TestEntryToDictDuckTyping` covers the shape-discrimination of `_entry_to_dict`.
---

## What

The capability registry (`work_buddy/mcp_server/registry.py`) holds two maps: `_REGISTRY` (active capabilities, directly callable via the MCP gateway) and `_DISABLED_REGISTRY` (capabilities whose `requires=[...]` tool probe failed at build time, stashed but not callable). At build-time, the registry filter pass moves any capability whose tool probe failed from `_REGISTRY` to `_DISABLED_REGISTRY` and adds a row to `work_buddy.tools.DISABLED_CAPABILITIES` listing the missing tools.

**The Obsidian-bridge tool family is the one exception.** When the bridge itself is down, the filter skips not just `obsidian` but every tool that transitively depends on it in the probe graph — the in-Obsidian plugins `datacore`, `smart_connections`, `google_calendar` (`work_buddy.tools.obsidian_backed_tools()`). The bridge is a transiently-flaky shared dependency, not a genuinely-absent one, so those capabilities stay admitted and are governed at runtime by a circuit breaker on the gateway dispatch (see `architecture/resilience` and `work_buddy/mcp_server/dispatch_resilience.py`): they fail fast per call while the bridge is down and recover the instant it returns — no session-long disable, no reload. This carve-out is **transitive-only**: it applies *only* when the bridge itself is down. If the bridge is up but a plugin is genuinely missing (e.g. `datacore` not installed), that plugin's capabilities still hard-disable here. The build-time disable below therefore applies to genuinely-absent dependencies (a missing plugin while the bridge is up, hindsight, thunderbird, ...).

Disabled state is cached. A capability disabled by a transient probe failure (e.g. the hindsight memory service unreachable for 200ms during sidecar startup) stays disabled until something explicitly re-probes it.

## Capability schema (selected fields)

- `name`, `description`, `parameters`, `callable` — the dispatch surface.
- `requires: list[str]` — tool IDs the dispatcher gates on.
- `mutates_state: bool`, `retry_policy: "manual" | "replay" | "verify_first"` — inform the gateway's auto-enqueue policy.
- `consent_operations: list[str]` — declarations for the gateway's pre-flight consent bundling.
- `op_id: str | None` — set when the capability was resolved from an inert declaration rather than instantiated directly (see "Declaration-based capabilities" below); None for directly-registered capabilities.
- `effects: list[EffectSpec]` — manifest of externally-visible effects for capabilities that produce more than one. When non-empty, the post-write-verify recovery path uses `verify_post_write_effects` (walks every declared effect; can return `partial`) instead of single-effect verify. Capabilities with declared effects MUST be idempotent under retry. Schema lives at `work_buddy.obsidian.effects.EffectSpec`; recovery semantics in `architecture/retry-queue`.
- `timeout_seconds: float | None | Callable[[params], float | None]` — the wall-time budget for one gateway dispatch, owned by the operation (never the caller). A scalar is a fixed ceiling; a callable derives the budget from the actual params (for operations whose runtime scales with input); unset (`None`) means the gateway applies the domain default — Obsidian-bridge capabilities run unbounded (they self-retry), everything else gets 30s. Resolved at dispatch in `work_buddy/mcp_server/dispatch_resilience.py`; a timed-out dispatch returns `error_kind="mcp_gateway_timeout"`.

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
