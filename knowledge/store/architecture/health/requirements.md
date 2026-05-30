---
name: Requirements layer
kind: system
description: 'Configuration-time validation: does the environment have what a component needs (plugins installed, configs present, secrets reachable, directories created)?'
tags:
- health
- requirements
- setup
- validation
- bootstrap
- fix
- architecture
aliases:
- requirements
- RequirementDef
- REQUIREMENT_REGISTRY
- RequirementChecker
- requirement checks
- setup validation
- config-time checks
- is the setup correct
parents:
- architecture/health
- architecture/health
dev_notes: 'When adding a new requirement: (1) pick the right domain prefix (core / obsidian / services / integrations); (2) write the check_fn in requirement_checks.py with the no-HTTP rule — if you need a runtime probe, delegate to checks.py via the lmstudio pattern; (3) decide fix_kind early — most should be at least programmatic or agent_handoff, only use ''none'' when there''s genuinely no automated path. Author/edit the requirement''s knowledge unit via the docs_edit workflow (or a direct .md edit + agent_docs_rebuild).'
---

Configuration-time validation. Answers "is the environment set up correctly for this subsystem to work?" — distinct from health checks ("is the service running right now?"). Requirements check stable, one-shot setup state: a plugin is installed, a config file exists, a directory was created, a secret is reachable.

## `RequirementDef` schema

Each requirement is a `RequirementDef` registered in `REQUIREMENT_REGISTRY` (a `dict[str, RequirementDef]` keyed by id). Fields:

* `id` — hierarchical path `{domain}/{subsystem}/{check-name}`. Domains: `core/` (bootstrap), `obsidian/` (vault structure, plugins), `services/` (sidecar + external services), `integrations/` (Chrome, Hindsight, etc.).
* `component` — component id this belongs to, or `None` for core requirements.
* `description` — human-readable description of what's checked.
* `check_fn` — dotted import path to a callable returning `{ok: bool, detail: str}`.
* `severity` — `"required"` (failure blocks setup) or `"recommended"` (failure surfaces but doesn't block).
* `fix_hint` — human-readable fix instructions (always present; legacy for `fix_kind=none`).
* `setup_group` — wizard grouping: `repository`, `journal`, `tasks`, `obsidian`, `contracts`, `knowledge`, `chrome`, `memory`, `embedding`, `telegram`, `thunderbird`, `credentials`, `remote_access`, etc.
* Fix-system fields: `fix_kind` / `fix_fn` / `fix_params` / `fix_preview` / `fix_agent_brief`. See [architecture/health/fixers](architecture/health/fixers).

## `RequirementChecker` API

* `check_all(include_unwanted=False)` — validates all requirements. Skips ones whose component is opted out via preferences (`is_wanted()` returns `False`). Core requirements (`component=None`) always run.
* `check_bootstrap()` — only `core/*` requirements (fast, no preference filter).
* `check_component(component_id)` — requirements for one component.
* `check_group(group_name)` — requirements in one `setup_group`.
* `summarize(results)` — produces summary counts (passed, failed_required, failed_recommended, all_required_pass, failures).

## Check function convention

Lives in `work_buddy.health.requirement_checks`. Each check function: sync, fast (filesystem and config inspection only — no HTTP, no service pings, no bridge communication), returns `{"ok": bool, "detail": str}`. Failures should not raise; the dispatcher catches exceptions and converts to `{ok: False, detail: "Check raised an error: ..."}`. For checks that need a runtime probe (e.g., LM Studio reachability, Tailscale Serve config), delegate to the relevant component health check function rather than re-implementing the probe — see `check_lmstudio_reachable` for the pattern.

## Distinction from health checks

| | Requirements | Components / health checks |
|---|---|---|
| Question | Is the setup correct? | Is it running right now? |
| Lifecycle | Configuration-time, mostly stable | Runtime, changes every restart |
| Cadence | On demand or scheduled audit | Continuously probed by `engine.py` |
| Remediation | Fixers (`fix_kind`) | Restart the service / fix upstream |

## Composition with components

`ComponentDef.requirements: list[str]` declares which requirement ids gate the component. `SetupWizard.diagnose(component)` runs requirements first; on failure, halts before probing runtime state — the missing setup is the root cause.

## See also

* [architecture/health](architecture/health) — the four-layer overview.
* [architecture/health/fixers](architecture/health/fixers) — how requirements opt into automated repair.
* [architecture/health/components](architecture/health/components) — the runtime layer requirements gate.
* [architecture/control-graph](architecture/control-graph) — how requirements appear as `requirement` nodes in the unified graph; cascade and `effective_state` rules.
