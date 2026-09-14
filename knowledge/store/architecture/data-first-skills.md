---
name: Data-First Skills
kind: concept
description: The Op/skill-declaration split — executable Ops registered by stable ID, inert skill declarations that reference them, and the loader + load-time validator that resolves declarations against the Op registry.
tags:
- skill
- op
- op-registry
- skill-loader
- declaration
- data-first
- executable-inert
- architecture
aliases:
- op registry
- skill declaration
- skill loader
- data-first skills
- register_op
- declaration-based skill
parents:
- architecture
dev_notes: |-
  Authoring a declaration-based skill: (1) register the callable as an Op in a module under `work_buddy/mcp_server/ops/` (one module per category; `load_builtin_ops` discovers them via `pkgutil.iter_modules`); (2) author the skill declaration unit (`kind: skill`, with `op: op.wb.<name>`, `schema_version: wb-skill/v1`, `skill_name`, `category`, a `parameters` schema, `requires`, …) via the `docs_edit` workflow — it is an ordinary Markdown unit.

  For a skill with an effect manifest (only `task_create` today), the ops module also calls `register_op_effects("op.wb.<name>", [EffectSpec(...)])` — effects are code (the `EffectSpec` carries a `resolver` callable) so they cannot live in a data declaration.

  The loader runs inside `_build_registry()` before the tool-requirements filter pass, so declared skills with unmet `requires` are filtered by the same logic as anything else.

  `validate_signature` introspects with `follow_wrapped=True`; a callable that accepts `**kwargs` is treated as accepting any declared parameter name, and a callable whose signature cannot be introspected is treated as matching.

  A new or edited **declaration**, Workflow definition, Workflow executable alias/reference, or parameter schema goes live via `reload_skill_data`. The rebuild refreshes Workflow identity indexes and compiled revisions from fresh store data WITHOUT a `sys.modules` purge. New or edited **Op** Python code still needs a gateway process restart (Ctrl+R) to be importable.
---

## What

A skill has two halves that work-buddy keeps separate:

- An **Op** — the executable callable. Python code, registered under a stable `op.<namespace>.<name>` ID in the Op registry (`work_buddy/mcp_server/op_registry.py`). Built-in ops use the `op.wb.*` namespace and are organized by category under `work_buddy/mcp_server/ops/` — one module per category, registering its ops as an import side effect.
- A **skill declaration** — inert data. A `kind: "skill"` knowledge-store unit carrying prose (name, description, aliases), the parameter schema, runtime metadata (`mutates_state`, `retry_policy`, `requires`, `consent_operations`, `invokes`, `is_action`, `intrinsic_amplifiers`, `param_aliases`, `auto_retry`, `slash_command`), and an `op` field naming the Op it wraps.

The Workflow side uses the same broad data/code separation through a different compiler seam. A `kind: workflow` unit is inert authored data; registry compilation produces a `WorkflowDefinition`, resolves executable aliases and Workflow references to stable definition IDs, computes a revision, and exposes invocation through `WorkflowService`. A Skill declaration resolves to an Op-backed callable `Skill`; the shared registry currently holds these as distinct entry types.

## Registration

Every skill is a declaration. The skill loader (`work_buddy/knowledge/skill_loader.py`) reads each `kind: "skill"` unit, resolves its `op` against the Op registry, checks that `schema_version` is recognized (`wb-skill/v1`), and validates the declared parameter schema against the resolved callable's signature. Resolved skills are merged into the gateway registry by `_build_registry()` before the tool-requirements filter pass, so declared skills with unmet `requires` are filtered by the same logic as any other.

## Op registry

`work_buddy/mcp_server/op_registry.py` is an in-memory table keyed by op ID:

- `register_op(op_id, fn)` — register a callable; validates the `op.<namespace>.<name>` grammar and rejects duplicate IDs.
- `get_op(op_id)` / `list_ops()` — lookup.
- `load_builtin_ops()` — imports the `work_buddy/mcp_server/ops/` package, whose modules register their ops as an import side effect.
- `register_op_effects(op_id, effects)` / `get_op_effects(op_id)` — for skills with multi-effect manifests. An `EffectSpec` holds a `resolver` callable, so it cannot ride in a data declaration; the ops module registers the manifest and the loader threads it onto the resolved `Skill`.

The Op table (`_OPS`) survives a data-only `reload_skill_data` (no purge — declarations just re-resolve against the same callables). The dormant `invalidate_registry` purges `work_buddy.*` from `sys.modules`, which rebuilds `_OPS` fresh on the next registry build.

## Load-time validation

Resolution failures (missing op, unknown schema version, signature mismatch) are reported by the `skill_op_resolution` check in `docs_validate` as **warnings** — an unresolved declaration is surfaced but does not block the store. The live-store invariants in `tests/unit/test_skill_declarations_invariant.py` promote these to hard CI errors: every skill unit must be a declaration, every op id must be registered and well formed, every declaration must resolve with zero issues, and the resolved count must equal the unit count (no silent drops).

## Why the split exists

Welding prose to Python source blocks three things: editing a skill's description through gateway tools, shipping skills as inert shareable artifacts, and letting an agent synthesize a skill without writing Python. A declaration has none of those limits — its prose is editable via the `docs_edit` workflow, it is inert data safe to share, and an agent can author one. This is the executable/inert seam recorded as commitment C1 in the ecosystem architecture.
