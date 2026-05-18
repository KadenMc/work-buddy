---
name: Components & health checks layer
kind: system
description: 'Runtime probes: is this component (service, integration, plugin) actually running and reachable right now?'
tags:
- health
- components
- health-checks
- runtime
- engine
- diagnostics
- probes
- architecture
aliases:
- components
- ComponentDef
- COMPONENT_CATALOG
- health checks
- CheckStep
- check_sequence
- health_source
- HealthEngine
- DiagnosticRunner
- is it running
- runtime probes
parents:
- architecture/health
- architecture/health
dev_notes: 'When adding a component: (1) pick the right `health_source` — ''custom'' avoids continuous polling for things you don''t want to spam (external integrations like Tailscale); (2) put runtime check_fns in checks.py, requirement check_fns in requirement_checks.py — they are different files for a reason (no-HTTP rule on requirement_checks); (3) declare requirements explicitly so diagnose can surface missing setup before broken probes; (4) on_fail strings are user-facing instructions, not stack traces — lead with the action. The DiagnosticRunner is described here rather than a separate unit because it has no schema of its own; it''s pure behavior on top of check_sequences.'
---

Runtime probes. Answers "is this thing running and reachable right now?" — distinct from requirements ("is the setup correct?"). Components describe runtime entities: a sidecar service, an external integration's HTTP bridge, an Obsidian plugin's responding endpoint.

## `ComponentDef` schema

Registered in `COMPONENT_CATALOG` (a `dict[str, ComponentDef]` keyed by id). Fields:

* `id` — unique component id (matches what wizard / diagnose / settings tab reference).
* `display_name` — human-readable label for the UI.
* `category` — `external` | `integration` | `service` | `plugin`.
* `is_core` — if `True`, user cannot opt out via preferences. Use for components without which work-buddy can't function.
* `health_source` — how runtime status is determined. See taxonomy below.
* `depends_on: list[str]` — hard runtime dependencies on other components. Failure cascades as `blocked`.
* `soft_depends_on: list[str]` + `soft_dep_notes: dict[str, str]` — optional helpers; failure cascades as `degraded` at worst.
* `requirements: list[str]` — requirement ids that gate this component. Diagnose runs these first.
* `check_sequence: list[CheckStep]` — ordered diagnostic steps run by `DiagnosticRunner`.
* `sidecar_service: str | None` — set when `health_source` involves a sidecar service.

## `CheckStep` schema

* `description` — what the step verifies.
* `check_fn` — dotted import path to a callable returning `{ok: bool, detail: str}`. Functions live in `work_buddy.health.checks`.
* `on_fail` — human-readable fix instructions surfaced when this step fails. Should tell the user what to do, not what failed.

## `health_source` taxonomy

* `tool_probe` — component exposes an HTTP `/health` endpoint or equivalent that the engine pings on a cadence. Default for sidecar services.
* `sidecar` — status comes from the sidecar's view of supervised processes (pid alive, last heartbeat).
* `composite` — status is derived from multiple probes combined (e.g., dashboard `up` requires both Flask answering AND messaging bridge healthy).
* `custom` — status resolves only when `diagnose` runs; no continuous probe. Use for external integrations where polling is wasteful or undesirable (e.g., Tailscale).

## How diagnose walks check sequences

`DiagnosticRunner.diagnose(component_id)` does a depth-first walk of the component's dependency chain (parents first, so missing upstream surfaces before broken downstream), then runs the component's own `check_sequence` in declared order. On the first `ok=False`, it returns a `DiagnosticResult` with `status="failed"`, the failed step's `description` as the root cause, and the step's `on_fail` text as the fix hint. Runtime exceptions in a check_fn are caught and converted to `status="error"` rather than propagated. This is what powers `setup_wizard(mode="diagnose")` and `setup_help(component=...)`.

## Hard vs soft dependencies (cascade summary)

* **Hard (`depends_on`)**: known-bad upstream → downstream is `blocked`. Use when the target is non-substitutable (Hindsight → PostgreSQL).
* **Soft (`soft_depends_on`)**: known-bad upstream → downstream is `degraded` at worst; disabled soft deps don't cascade. Use for optional helpers (dashboard → embedding: hybrid search falls back to substring).
* **Unknown is distinct from failure** — a hard dep in `unknown` (probe pending) does NOT cascade to `blocked`. Full `effective_state` rules are in [architecture/control-graph](architecture/control-graph).

## Distinction from requirements

| | Components / health checks | Requirements |
|---|---|---|
| Question | Is it running right now? | Is the setup correct? |
| Lifecycle | Runtime, changes every restart | Configuration-time, mostly stable |
| Cadence | Continuous (per `health_source`) or on-demand via diagnose | On demand or scheduled audit |
| Remediation | Restart, fix upstream component, run diagnose | Fixers (`fix_kind`) |

## Composition with requirements and preferences

* Component declares `requirements=[<requirement_id>, ...]`. Diagnose runs requirements first — missing setup surfaces before runtime probes.
* Preferences (`is_wanted()`) gate everything. A component opted out skips both requirement checks and runtime probes.

## See also

* [architecture/health](architecture/health) — the four-layer overview.
* [architecture/health/requirements](architecture/health/requirements) — the configuration-time gating layer.
* [architecture/health/fixers](architecture/health/fixers) — fixers attach to requirements, not components; runtime failures need different remediation.
* [architecture/control-graph](architecture/control-graph) — cascade rules, `effective_state` derivation, dashboard endpoints.
