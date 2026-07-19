---
name: Health System (mental model)
kind: concept
description: 'Four-layer mental model of work-buddy''s health system: do I want this? / is the setup correct? / is it running right now? / how does the user repair it?'
tags:
- health
- architecture
- mental-model
- preferences
- requirements
- components
- fixers
- overview
aliases:
- health system
- health layers
- four-question model
- four-layer model
- health overview
- health architecture
- what-do-i-want
- wanted feature
- is-it-set-up
- is-it-running
- how-do-i-repair
parents:
- architecture
- architecture
dev_notes: Created to give the four-layer health system an intuitive entry point. Before this unit, agents had to synthesize the four-question model by reading control-graph + features/preferences + three module docstrings. control-graph stays a sibling (it's a consumer that also pulls from registry) — see the 'Why this is a sibling, not a child' justification in the originating plan. Keep this unit short (~30 lines max). Field-level schemas live in the per-layer system units, not here. Resist scope creep into technical-aggregator territory — control-graph already does that.
---

Work-buddy's health system answers four genuinely different questions about a feature, each owned by its own layer with its own registry, check functions, and dashboard surface.

## The four questions

| Question | Layer | Where it lives |
|---|---|---|
| *Do I want this?* | **Preferences** | `work_buddy.health.preferences` + `config.local.yaml` |
| *Is the setup correct?* | **Requirements** | `architecture/health/requirements` |
| *Is it running right now?* | **Components / health checks** | `architecture/health/components` |
| *How does the user repair it?* | **Fixers** | `architecture/health/fixers` |

The layers are separate because they answer different questions on different cadences with different remediation stories. Preferences only change when the user clicks; requirements get checked on demand; components are continuously probed; fixers run on click.

## Preferences (subsystem inline)

The smallest layer; documented here rather than a dedicated unit. Module: `work_buddy.health.preferences`. Storage: `config.local.yaml` under `features.<component_id>.{wanted, reason}` (three-state: Want / No thanks / Undecided). The `is_wanted()` filter is consulted by `RequirementChecker.check_all`, by component probes in `engine.py`, and by the setup wizard — anything that probes a component skips it when `wanted=False`. Mutating the preference goes through `apply_preference_updates()` (consent-gated). For agent-facing behavioral guidance ("check `wanted` before recommending or probing") see [features/preferences](features/preferences).

## How the layers compose

* `ComponentDef` declares `requirements=[<requirement_id>, ...]`. Diagnose runs requirements first, then the component's `check_sequence`.
* Requirements opt into a fixer via `fix_kind` (`none` / `programmatic` / `input_required` / `agent_handoff`). Fixers are not attached to components or to runtime probes — they only repair *configuration-time* state.
* Preferences gate everything downstream. A component marked `wanted=False` skips its requirement checks AND its component probes.

## See also

* [architecture/control-graph](architecture/control-graph) — the aggregator that fuses these four layers plus the registry into a unified view-model for the dashboard's Settings tab. This is the right place to look for: cascade rules, hard vs soft dependencies, `effective_state` derivation, dashboard endpoints (`/api/control/*`), and the fix/help/reprobe spawning logic.
* [features/preferences](features/preferences) — agent-behavior directions for working with preferences.
* `dev/dev-mode` — how to add a new component or workflow.
