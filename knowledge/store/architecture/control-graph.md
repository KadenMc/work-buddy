---
name: Control Graph
kind: concept
description: Unified view-model over preferences, requirements, health, and registry. Powers the Settings tab; backs the fix + help systems.
summary: 'Graph of domains/subsystems/components/requirements/capabilities fused from the five existing subsystems. Settings tab renders it; Fix + Help + Reprobe endpoints mutate/refresh it. Read-only in spirit: source data lives in health/, preferences/, requirements/, registry — control/ just aggregates and exposes.'
tags:
- control-graph
- settings
- fix
- help
- preferences
- requirements
- observability
- orchestration
aliases:
- settings tab
- control graph
- fix system
- help agent
- reprobe
- component preferences
parents:
- architecture
- architecture
dev_notes: Added 'See also' cross-reference to architecture/health (and per-layer units) so agents landing here have a pointer back to the conceptual model. The control-graph unit stays the technical-aggregator reference; the health unit owns the four-question intuitive frame.
---

Unified view-model layer that fuses work-buddy's five loosely-coupled observability subsystems into one queryable graph. Lives in ``work_buddy/control/`` — pure Python, no state ownership, rebuilds from the authoritative sources on a 45-second TTL cache.

> **See also:** [architecture/health](architecture/health) frames the conceptual four-layer mental model behind the health-system subsystems this graph aggregates. Per-layer reference docs: [architecture/health/requirements](architecture/health/requirements), [architecture/health/components](architecture/health/components), [architecture/health/fixers](architecture/health/fixers).

## What it aggregates

* **Preferences** (``work_buddy.health.preferences``) — what the user wants.
* **Requirements** (``work_buddy.health.requirements``) — filesystem/config checks.
* **Health** (``work_buddy.health.engine``) — runtime probes + sidecar state.
* **Diagnostics** (``work_buddy.health.diagnostics``) — ordered troubleshooting.
* **Registry** (``work_buddy.mcp_server.registry``) — capabilities + workflows.

Before the control graph, each subsystem exposed its own slice and agents had to correlate state across five different shapes. The graph gives one node view that answers questions like "this workflow is blocked because a requirement of a dependency component isn't met" without cross-cutting the consumer.

## Node model (``work_buddy/control/nodes.py``)

Five kinds of ``ControlNode``:

* ``domain`` — top-level user-facing bucket (Journal, Notifications, Knowledge, Browser, Calendar, Runtime, System).
* ``subsystem`` — intermediate grouping under a domain (Daily Notes, Task Lifecycle, Hindsight, Bootstrap, Credentials, ...).
* ``component`` — concrete runtime entity from ``COMPONENT_CATALOG``. Carries the ``preference`` field.
* ``requirement`` — configuration check (wrapped from ``REQUIREMENT_REGISTRY``). Carries fix metadata (``fix_kind``, ``fix_fn``, ``fix_params``, ``fix_preview``).
* ``capability`` — registry entry (both atomic Capability and WorkflowDefinition). Unparented — surfaces via component ``affects_capabilities`` inverse edges rather than a noisy flat domain listing.

Each node carries two kinds of edges:

* ``grouping_parents`` — hierarchical roll-up ("I live under X"). Multi-parent allowed: ``component:dashboard`` appears under both ``domain:notifications`` and ``domain:runtime``.
* ``dependencies`` — runtime contract ("I need X healthy"). An ``Edge`` has ``target_id``, ``mode`` ("all" default; "any" reserved), and ``hardness`` ("hard" default / "soft").

## Hard vs soft dependencies

* **Hard** — failure cascades as ``blocked``. Use for targets without which this node literally cannot function (Hindsight → PostgreSQL).
* **Soft** — failure cascades as ``degraded`` at worst; disabled soft deps don't cascade at all. Use for optional helpers whose absence reduces functionality but doesn't break the node (dashboard → embedding: hybrid search falls back to substring).

Declared on ``ComponentDef`` via ``depends_on`` (hard) and ``soft_depends_on`` (soft).

## effective_state derivation

One derived label per node. Six values: ``ok``, ``degraded``, ``blocked``, ``disabled``, ``unconfigured``, ``unknown``.

Key rules:

* ``preference=unwanted`` → ``disabled`` (the cascade rule: unwanted is invisible; probes skip it).
* ``preference=required`` (core components, ``is_core=True``) is treated like ``wanted`` for cascade.
* **Unknown is distinct from failure.** A hard dep in ``unknown`` (pending probe) → downstream is also ``unknown``, NOT ``blocked``. Propagating uncertainty as certainty-of-failure is what painted the whole graph red on dashboard startup before this rule landed.
* Soft-dep ``unknown`` doesn't degrade either — we don't announce a known reduction in functionality on the basis of a probe that hasn't completed.
* Known-bad hard dep (``blocked``/``unconfigured``/``degraded``) → downstream ``blocked``.
* Grouping roll-up uses worst-child-wins with ranking ``blocked > unconfigured > degraded > unknown > ok > disabled``; all-children-disabled → parent is ``disabled``.

Note that a component's ``effective_state`` lags a re-enable: it is derived partly from the health probe cache, so a component flipped back to ``wanted`` stays stale-``disabled`` until the next reprobe. Surfaces that must react instantly to a preference change (e.g. the dashboard card registry) should read the preference directly rather than ``effective_state``. See ``architecture/feature-cards``.

## Fix system (``work_buddy/control/fix_runner.py``)

Every requirement may opt into a fix. Four kinds:

* ``none`` (default) — no automated fix; fix_hint only.
* ``programmatic`` — ``fix_fn()`` does the fix end-to-end. UI shows an inline confirm panel with ``fix_preview``, then applies on user click.
* ``input_required`` — ``fix_fn(**form_values)``. UI renders an inline form from ``fix_params``; user submits, fix applies.
* ``agent_handoff`` — clicking **Walk me through** spawns a Claude Code session with the registered ``fix_agent_brief`` as prompt (desktop session, non-remote).

Fixers live in ``work_buddy/health/fixers.py``. They are idempotent, specific in their detail messages, and return ``{ok, detail, side_effects}`` without raising (the dispatcher converts exceptions for consistent endpoint behavior). Post-fix, the requirement's check is re-run and the graph cache busts.

## Help system (``work_buddy/control/help_briefs.py``)

Universal ``?`` button on every non-ok requirement (except those already offering agent_handoff as their fix, which redundantly spawn) and every component. Spawns a Claude Code session with a structured brief that bundles DiagnosticRunner output, requirement metadata, current state, blocking issues, and pointers to relevant agent docs. Subsumes the legacy Status-tab ``🪄 /wb-setup diagnose`` hint.

## Endpoints (``work_buddy/dashboard/service.py``)

* ``GET /api/control/graph[?force=1]`` — serialized node map + cache info.
* ``POST /api/control/reprobe`` — runs ``probe_all(force=True)`` (re-pings every service, ~10s worst case), rewrites tool_status.json, returns fresh graph. Read-only-gated.
* ``POST /api/control/preference`` — ``{updates: {component_id: {wanted, reason}}}``. Auto-consents; calls ``apply_preference_updates`` + invalidates graph.
* ``POST /api/control/fix/<req_id>`` — applies the fix; body ``{params}`` for input_required. Re-runs the check, returns ``{ok, detail, side_effects, recheck, spawned}``.
* ``POST /api/control/help/<node_id>`` — spawns a help session.

## Caching and invalidation

* ``build_graph()`` in ``work_buddy/control/graph.py`` has a 45-s TTL with a ``threading.Lock``.
* ``invalidate_graph()`` is called from ``preferences.set_preference``, ``preferences.apply_preference_updates``, and every mutating endpoint above.
* ``?force=1`` / ``build_graph(force=True)`` bypasses cache but does NOT reprobe; use ``POST /api/control/reprobe`` when you want fresh probe data on top of a fresh graph.

## Settings tab

The primary consumer lives at ``work_buddy/dashboard/frontend/scripts/tabs/settings.py`` + ``panel-settings`` in ``html.py``. Renders the graph as a hierarchy of domains; exposes preference toggles (3-state: Want / No thanks / Undecided), Configure / Walk me through action buttons per requirement, universal ``?`` help button, per-component ↻ reprobe button, and clickable bulk-state chips for drill-down into problem nodes.

## Relationship to other surfaces

* ``SetupWizard.guided()`` consumes the same domains (Phase G migration).
* The bridge latency chart, sidecar event log, and notification log live in the Settings → Activity sub-tab as registry-driven cards; the bridge card is gated on the ``obsidian`` component preference. See ``architecture/feature-cards``.
* Agents can call ``agent_docs(scope="architecture/control-graph")`` for this overview, then ``/api/control/graph`` for live state.
