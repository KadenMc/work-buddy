---
name: Data-First Capabilities
kind: concept
description: The Op/capability-declaration split — executable Ops registered by stable ID, inert capability declarations that reference them, and the loader + load-time validator that resolves declarations against the Op registry.
tags:
- capability
- op
- op-registry
- capability-loader
- declaration
- data-first
- executable-inert
- architecture
aliases:
- op registry
- capability declaration
- capability loader
- data-first capabilities
- register_op
- declaration-based capability
parents:
- architecture
- architecture
dev_notes: |-
  Authoring a declaration-based capability: (1) register the callable as an Op in a module under `work_buddy/mcp_server/ops/` (one module per category; `load_builtin_ops` discovers them via `pkgutil.iter_modules`); (2) create the capability unit with `docs_create(kind="capability", op="op.wb.<name>", schema_version="wb-capability/v1", capability_name=..., category=..., parameters=<JSON string>, requires=..., ...)`.

  The loader runs inside `_build_registry()` *before* the tool-requirements filter pass, so declared capabilities with unmet `requires` are filtered by the same logic as directly-registered ones.

  `Capability.op_id` marks declaration-sourced entries. `build_capability_units()` in `build.py` skips any registry entry whose `op_id` is set, so a declared capability is never re-emitted into `_generated_capabilities.json` (which would recreate two sources of truth). `_get_unfiltered_registry()` builds from the `_*_capabilities` builders directly today, so declared capabilities do not reach that loop anyway — the guard is defensive.

  `validate_signature` introspects with `follow_wrapped=True`; a callable that accepts `**kwargs` is treated as accepting any declared parameter name, and a callable whose signature cannot be introspected is treated as matching.

  A new op or a new declaration needs a full registry rebuild to go live. `mcp_registry_reload` rebuilds the registry, but a running gateway can still hold a stale capability *schema* for editor capabilities (e.g. `docs_create`) until a server restart.

  The canonical design is `.data/designs/data-first-capabilities/DECISIONS.md` and `.data/designs/ecosystem/architecture-commitments.md` (gitignored, local-only).
---

## What

A capability has two halves that work-buddy keeps separate:

- An **Op** — the executable callable. Python code, registered under a stable `op.<namespace>.<name>` ID in the Op registry (`work_buddy/mcp_server/op_registry.py`). Built-in ops use the `op.wb.*` namespace.
- A **capability declaration** — inert data. A `kind: "capability"` unit in the knowledge store carrying prose (name, description, aliases), the parameter schema, runtime metadata (`mutates_state`, `retry_policy`, `requires`), and an `op` field naming the Op it wraps.

This mirrors how workflows already work: a workflow is inert data that references capabilities by name; a capability declaration is inert data that references an Op by ID. Executable code (Ops) is held apart from the editable, shareable, agent-authorable data (declarations).

## Two registration paths

Capabilities reach the gateway registry through two paths, both live:

1. **Direct registration** — a `Capability(...)` instance in `work_buddy/mcp_server/registry.py` bundles callable + prose + schema together. The build script (`work_buddy/knowledge/build.py`) compiles these into `knowledge/store/_generated_capabilities.json`.
2. **Declaration resolution** — a capability declaration in the knowledge store names an Op; the capability loader (`work_buddy/knowledge/capability_loader.py`) resolves it at registry-build time and emits an equivalent `Capability` object.

A capability unit is declaration-based **iff** its `op` field is non-empty. Directly-registered (generated) units have no `op`, so the loader skips them. That single discriminator is what lets both paths run side by side.

## Op registry

`work_buddy/mcp_server/op_registry.py` is an in-memory table keyed by op ID:

- `register_op(op_id, fn)` — register a callable; validates the `op.<namespace>.<name>` grammar and rejects duplicate IDs.
- `get_op(op_id)` / `list_ops()` — lookup.
- `load_builtin_ops()` — imports the `work_buddy/mcp_server/ops/` package, whose modules register their ops as an import side effect.

The registry keeps no state across a reload: `mcp_registry_reload` purges `work_buddy.*` from `sys.modules`, so the table rebuilds fresh on the next registry build.

## Capability loader and load-time validation

`load_declared_capabilities()` reads every declaration-based capability unit and, for each one: resolves its `op` against the Op registry, checks the `schema_version` is recognized (`wb-capability/v1`), and validates the declared parameter schema against the resolved callable's signature. It returns ready-to-dispatch `Capability` objects plus a list of issues.

`_build_registry()` merges the resolved capabilities into the registry. A name already present keeps the directly-registered entry — a name claimed by both paths is a registration mistake, surfaced as an error log rather than silently shadowed.

Resolution failures (missing op, unknown schema version, signature mismatch) are reported by the `capability_op_resolution` check in `docs_validate` as **warnings** — an unresolved declaration is surfaced but does not block the store.

## Why the split exists

Welding prose to Python source blocks three things: editing a capability's description through gateway tools (the editor refuses writes to `_generated_*` files), shipping capabilities as inert shareable artifacts, and letting an agent synthesize a capability without writing Python. A declaration-based capability has none of those limits — its prose is editable via `docs_update`, it is inert data safe to share, and an agent can author one. This is the executable/inert seam recorded as commitment C1 in the ecosystem architecture.
