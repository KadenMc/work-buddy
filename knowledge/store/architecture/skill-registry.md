---
name: Skill Registry
kind: concept
description: How Skills and Workflow definitions are registered, how Workflows resolve by stable identity and executable aliases, and how probe-disabled Skills recover without a module reload.
summary: 'The shared gateway registry stores active Skills and canonical Workflow names in _REGISTRY, stable Workflow identities and aliases in secondary indexes, and probe-failed Skills in _DISABLED_SKILL_REGISTRY. reload_skill_data rebuilds declarations, Workflow definitions, aliases, references, revisions, and parameter schemas without purging modules; transient Skill probe failures recover through per-Skill re-probe.'
tags:
- skill
- registry
- tool-probe
- disabled
- recovery
- cp-a3
- per-skill
- reload
- architecture
aliases:
- registry
- skill registry
- tool probes
- disabled skill
- recover skill
- reload skill
- recheck skill
- registry recovery
- DISABLED_SKILLS
- recheck_disabled_skill
parents:
- architecture
dev_notes: |-
  ## Stale class identity after sys.modules purge

  The dormant `invalidate_registry` purges `work_buddy.*` from `sys.modules` and rebuilds the registry.  Any code that captured a class reference — most commonly an exception class used in an `except` clause, or a dataclass used for `isinstance` discrimination — holds onto the *pre-reload* class object.  The freshly-imported class is a different object even when its name, base classes, and shape are identical, so `isinstance(post_reload_instance, captured_pre_reload_class)` returns False.

  Symptoms observed in the activity ledger and reproduced in unit tests:

  - `'Capability' object has no attribute 'execution'` leaking through the gateway's parameter-error path (`gateway.py:1439`, `gateway.py:1524`).  At the time, `_entry_to_dict` discriminated via `isinstance(entry, Capability)`.  Post-reload, that check failed for instances cached outside the live registry; the function fell through to the `WorkflowDefinition` branch and accessed `.execution` on what was structurally the then-named `Capability` entry.

  - `"Execution failed: ConsentRequired: ConsentRequired: ..."` (double-prefix) in operation error_summary fields for skills that raise `ConsentRequired` manually (e.g. `context_wrappers.py:project_create` at line 944, with a lazy `from work_buddy.consent import ConsentRequired`).  Root cause: the gateway's typed `except ConsentRequired:` at `gateway.py:1535` captures the class object at module-import time.  After a sys.modules purge, the raise site re-imports a fresh class.  The captured reference no longer matches; the exception falls through to the broad `except Exception:` at `gateway.py:1583`, which stringifies via `f"{type(exc).__name__}: {exc}"` — producing a double prefix if the exception's own message also includes a leading `"ConsentRequired:"` token.  `ConsentRequired.__init__` deliberately omits the type-name prefix from its message for exactly this reason.

  ## Defensive patterns

  - **Shape-discrimination via `hasattr` instead of isinstance** for serialization-only paths.  Pattern in `_entry_to_dict`: `if hasattr(entry, "callable") and not hasattr(entry, "steps"):` reads as the Skill branch; the workflow branch is `else`.  Survives any class identity drift, including module reload.

  - **Duck-typed fallback in broad-exception handlers** for typed exceptions whose typed `except` clause may catch the stale reference.  Pattern in the gateway's broad `except Exception:` at `gateway.py:1583`: check `type(exc).__name__ == "ConsentRequired"` and route to the same auto-consent flow the typed handler would have taken.

  - **Drop redundant type-name prefixes from exception messages.**  `ConsentRequired.__init__` no longer prepends `"ConsentRequired: "` to its message; callers that stringify via `f"{type(exc).__name__}: {exc}"` now produce a single clean prefix instead of a double-prefix even when the typed catch is bypassed.

  - **Class identity is structurally fragile across sys.modules purges**; any new typed-exception catch or `isinstance` discrimination on a work_buddy class should consciously decide whether to use a shape-based fallback.  The hazard is not specific to the known cases — it applies anywhere a pre-reload reference is held.

  ## Related tests

  - `tests/unit/test_consent_stale_class_identity.py` demonstrates the bare mechanism (two locally-defined classes with the same name, isinstance fails across them).
  - `tests/unit/test_obsidian_retry_consent.py` covers the consent-propagation behaviour the defensive patterns protect.
  - `tests/unit/test_registry_invariants.py::TestEntryToDictDuckTyping` covers the shape-discrimination of `_entry_to_dict`.
---

## What

The shared registry in `work_buddy/mcp_server/registry.py` has distinct lookup roles:

- `_REGISTRY` contains active Skills under Skill names and Workflows under their canonical `workflow_name`.
- `_WORKFLOW_ID_INDEX` resolves stable `workflow_id` values.
- `_WORKFLOW_ALIAS_INDEX` resolves canonical and historical `workflow_aliases` addresses.
- `_DISABLED_SKILL_REGISTRY` holds Skills removed from active dispatch because a build-time tool probe failed.

At build time, the requirements filter moves a Skill whose probe failed from `_REGISTRY` to `_DISABLED_SKILL_REGISTRY` and adds a row to `work_buddy.tools.DISABLED_SKILLS` listing the missing tools. Workflows are not moved into the disabled-Skill map: their modes, preferences, component requirements, executor facilities, and authorization are evaluated for each invocation by `WorkflowAdmission`.

**The Obsidian-bridge tool family is the one exception.** When the bridge itself is down, the filter skips not just `obsidian` but every tool that transitively depends on it in the probe graph — the in-Obsidian plugins `datacore`, `google_calendar` (`work_buddy.tools.obsidian_backed_tools()`). The bridge is a transiently-flaky shared dependency, not a genuinely-absent one, so those skills stay admitted and are governed at runtime by a circuit breaker on gateway dispatch (see `architecture/resilience` and `work_buddy/mcp_server/dispatch_resilience.py`): they fail fast per call while the bridge is down and recover the instant it returns — no session-long disable, no reload. This carve-out is **transitive-only**: it applies *only* when the bridge itself is down. If the bridge is up but a plugin is genuinely missing (e.g. `datacore` not installed), that plugin's skills still hard-disable here. The build-time disable below therefore applies to genuinely-absent dependencies (a missing plugin while the bridge is up, hindsight, thunderbird, ...).

Disabled state is cached. A skill disabled by a transient probe failure (e.g. the hindsight memory service unreachable for 200ms during sidecar startup) stays disabled until something explicitly re-probes it.

## Skill schema (selected fields)

- `name`, `description`, `parameters`, `callable` — the dispatch surface.
- `requires: list[str]` — tool IDs the dispatcher gates on.
- `mutates_state: bool`, `retry_policy: "manual" | "replay" | "verify_first"` — inform the gateway's auto-enqueue policy.
- `consent_operations: list[str]` — declarations for the gateway's pre-flight consent bundling.
- `op_id: str | None` — set when the skill was resolved from an inert declaration rather than instantiated directly (see "Declaration-based skills" below); `None` for directly registered skills.
- `effects: list[EffectSpec]` — manifest of externally-visible effects for skills that produce more than one. When non-empty, the post-write-verify recovery path uses `verify_post_write_effects` (walks every declared effect; can return `partial`) instead of single-effect verify. Skills with declared effects MUST be idempotent under retry. Schema lives at `work_buddy.obsidian.effects.EffectSpec`; recovery semantics in `architecture/retry-queue`.
- `timeout_seconds: float | None | Callable[[params], float | None]` — the wall-time budget for one gateway dispatch, owned by the operation (never the caller). The gateway timeout is **opt-in**: a scalar is a fixed ceiling, a callable derives the budget from the actual params (for operations whose runtime scales with input), and unset (`None`) is **unbounded** — the gateway imposes no cap. No flat default is applied: skills that deliberately block or run long (human-in-the-loop `request_send`/`request_poll`, the `obsidian_retry`/`retry` wrappers, `llm_submit`) would be wrongly cut off by a too-low default, so a real default must be calibrated from observed dispatch p99 and paired with explicit exemptions. Resolved at dispatch in `work_buddy/mcp_server/dispatch_resilience.py`; a skill that declares a finite budget and overruns it gets `error_kind="mcp_gateway_timeout"`.

## Compiled Workflow identity

A compiled `WorkflowDefinition` carries:

- stable `workflow_id` and deterministic `workflow_revision`;
- canonical `name`, human-facing `display_name`, and executable `aliases`;
- the full transitive `requires` closure;
- `required_components`, which omits dependencies reachable only through optional Steps.

`get_entry(address)` resolves a canonical Workflow name, executable alias, or stable ID. Exact `search_registry` lookups use the same resolver and return identity, revision, display name, aliases, and Step target metadata.

Registry construction rejects duplicate definition IDs, duplicate executable addresses, addresses in the reserved `wfd_` namespace, Workflow address/ID collisions, and collisions between Workflow addresses or IDs and Skill names. An authored Workflow reference is compiled from its source address to `WorkflowStep.target_workflow_id`. Dependency compilation follows Skill `invokes` chains and referenced Workflows transitively, including dependencies of disabled Skills, so filtering a child Skill cannot hide a required component from Workflow admission.

## Two recovery paths — use the right one

### Per-skill re-probe (preferred for runtime recovery)

**`work_buddy.recovery.recheck_disabled_skill(name, *, force=False)`** re-probes ONLY the skill's missing tools, with per-tool cool-down (default 30s, env-overridable via `WB_RECHECK_COOLDOWN_SECS`), single `_RECOVERY_LOCK` (RLock) for concurrent-caller safety. On success, mutates `_REGISTRY` in place to restore the skill. No rebuild, no module purge.

Returns `True` if the skill is now in the live registry, `False` if it remains disabled (with `DISABLED_SKILLS[name]` updated to reflect any partially recovered tools).

Companion: **`recheck_tool(tool_id, *, force=False)`** for re-probing a single tool without scoping to a skill. Same cool-down, same lock.

Used by:
- The gateway's `wb_run` dispatch path (`work_buddy/mcp_server/tools/gateway.py`). On hitting a disabled skill, the gateway calls `recheck_disabled_skill` before returning the disabled error.
- The sidecar's retry sweep `_replay` (`work_buddy/sidecar/retry_sweep.py`). On hitting a disabled skill during a queued retry, the sweep calls `recheck_disabled_skill` rather than reporting "not found in registry". It falls back to invoking the disabled entry's callable when recheck still says no, since the bridge call inside raises a typed transient exception and the operation re-queues correctly.

### Data-only registry reload (declaration / workflow / param-schema changed)

**`reload_skill_data`** skill (calls `reload_skill_data()` in `registry.py`) resets the knowledge-store cache and clears `_REGISTRY`, then rebuilds in place via `get_registry()` — WITHOUT purging `sys.modules`. Because no module is re-imported, `Skill` / `WorkflowDefinition` class identity stays stable and the long-lived FastMCP gateway reads the rebuilt registry directly. It costs about 6–8 seconds because `_build_registry` re-probes every tool.

Use when you edited or added a Skill **declaration** or a **Workflow** unit and want it live without a restart. For Workflows the rebuild refreshes canonical entries, stable-ID and alias indexes, compiled references, dependency closures, revisions, and parameter schemas. It also re-enables a Skill whose tool just came back because the rebuild re-probes and re-runs the requirements filter.

It does NOT pick up edited Op **code** or a brand-new Op **module** — re-importing Python is what a process restart (Ctrl+R) does safely.

> **Retired:** `mcp_registry_reload` (the function `invalidate_registry()` lives on, dormant) purged `work_buddy.*` from `sys.modules` to pick up code. In the long-lived FastMCP gateway that silently did nothing — `wb_run` / `wb_search` are frozen against the boot module generation, so the rebuilt registry never reached dispatch, while the purge corrupted the then-named `Capability` class identity. It was removed from the agent surface; use `reload_skill_data` for data and a `Ctrl+R` restart for code. See `dev/mcp-reload` and `.data/designs/mcp-registry-reload`.

**Do NOT use `reload_skill_data` for transient probe failures.** The dispatch path already auto-recovers a disabled skill via `recheck_disabled_skill` (a per-skill re-probe); the full rebuild is heavier than needed for that.

## Decision tree

```
Skill is disabled / not found in active registry
  - Declaration / workflow / param-schema changed -> reload_skill_data
  - Op code or new Op module changed -> restart the gateway (Ctrl+R)
  - Probe transient-failed -> recheck_disabled_skill(name)
      - Returns True -> skill is back in _REGISTRY, proceed
      - Returns False -> tools still down
          - Caller wants to retry later -> re-queue / surface
          - Caller can run the skill anyway ->
              invoke disabled_entry.callable(...)
              (typed bridge exception -> @bridge_retry handles)
```

## Declaration-based skills

Not every skill is a `Skill(...)` instance written directly in `registry.py`. A skill can also be an inert **declaration** in the knowledge store that names an **Op** (a callable registered by ID in the Op registry). The skill loader resolves declarations at registry-build time and merges the resulting `Skill` objects into `_REGISTRY` alongside directly registered ones — a declared skill is indistinguishable at dispatch time except for its `op_id` field. See `architecture/data-first-skills` for the Op registry, loader, and load-time validation.

## Key files

- `work_buddy/mcp_server/registry.py` — shared Skill/Workflow registry, stable-ID and alias indexes, compiled Workflow references and dependencies
- `work_buddy/mcp_server/op_registry.py` — Op registry backing declaration-based skills (see `architecture/data-first-skills`)
- `work_buddy/knowledge/skill_loader.py` — resolves skill declarations against the Op registry
- `work_buddy/workflows/identity.py` — definition-ID grammar, generation, read-compat derivation, and deterministic revision hashing
- `work_buddy/workflows/admission.py` — invocation-time Workflow policy
- `work_buddy/workflows/service.py` — application façade used by driving adapters
- `work_buddy/recovery.py` — `recheck_disabled_skill`, `recheck_tool`, `_RECOVERY_LOCK`, `_LAST_RECHECK_AT`
- `work_buddy/obsidian/effects.py` — `EffectSpec` schema for the `Skill.effects` manifest
- `work_buddy/obsidian/post_write_verify.py` — `verify_post_write_effects` walker
- `work_buddy/tools.py` — `DISABLED_SKILLS`, `is_tool_available`, `reprobe_one`
- `work_buddy/mcp_server/tools/gateway.py` — dispatch path with effects-aware PWU handler
- `work_buddy/sidecar/retry_sweep.py` — sweep path with disabled-cap recovery and effects-aware pre-verify

## When in doubt

Per-skill re-probe is almost always right at runtime. `reload_skill_data` is for data changes (declarations, workflows, param schemas) and full inventory rebuilds; an Op **code** change or a new Op module needs a gateway restart (Ctrl+R). The 30-second cool-down on each skill stops aggressive callers from hammering a genuinely-down tool.
