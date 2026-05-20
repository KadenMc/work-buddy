---
name: Feature Preferences
kind: directions
description: How to check feature preferences before recommending or using a component, and how the requirements system differs from runtime health checks.
summary: Check features.<name>.wanted in config.local.yaml before recommending or diagnosing a feature. wanted:false means don't probe; point at /wb-setup preferences.
trigger: Before recommending, probing, or diagnosing a feature; whenever a user asks 'why isn't X working?'
capabilities:
- setup_wizard
- feature_status
tags:
- features
- preferences
- opt-in
- setup-wizard
- requirements
aliases:
- feature preferences
- opt-in features
- wanted feature
- user opted out
- feature disabled
- setup preferences
parents:
- features
- features
dev_notes: Added one-line cross-ref to architecture/health for the preferences subsystem mechanics. This unit stays as agent-facing behavioral directions ('check wanted before recommending or probing'); the architecture overview owns the subsystem-mechanics framing.
---

Users can opt in or out of components via ``features:`` in ``config.local.yaml``. Three surfaces manage these preferences: the setup wizard (``/wb-setup``), the dashboard Settings tab (gear icon in the header), and direct config-file edits. All three go through ``work_buddy.health.preferences`` so they stay in sync.

For the preferences subsystem mechanics in context (``is_wanted()`` filter, three-state semantics, consent gate) and how preferences compose with requirements / components / fixers, see [architecture/health](architecture/health).

## Check preferences before acting

**Before recommending or using a feature, check preferences:**

- If ``wanted: false`` — do **not** suggest, probe, or diagnose it. If the user asks "why isn't X working?", mention they opted out and point them at the Settings tab (preferred) or ``/wb-setup preferences``.
- If ``wanted: true`` or ``wanted: null`` (undecided) — use normally. The probe loop already skips ``wanted=false`` components, so ``feature_status`` will reflect opt-out as ``disabled`` state.
- ``feature_status`` returns preferences + tool availability in one call. Prefer it over probing components individually.

## Core (non-opt-out) components

Some components are marked ``is_core=True`` in ``COMPONENT_CATALOG``: ``sidecar``, ``messaging``, ``embedding``, ``dashboard``. Nothing in work-buddy works without them, so ``is_wanted()`` forces ``True`` regardless of what the config says. The control graph labels their preference ``required``; the Settings UI shows a green "Required (no opt-out)" indicator instead of a toggle. A user writing ``features.sidecar.wanted: false`` in config.local.yaml is silently ignored at the ``is_wanted()`` boundary.

## Requirements system vs health checks

Two adjacent systems, distinct purposes:

- **Requirements** (``work_buddy/health/requirements.py``) — configuration-time checks that validate hidden assumptions: vault sections exist, required plugins are enabled, config keys are set. Failures come with fix instructions AND — increasingly — a registered fix (``fix_kind``: programmatic / input_required / agent_handoff) that the Settings tab can apply with a button.
- **Health checks** — runtime checks that a service is actually up and responding.

Rule of thumb: "is it configured?" → requirements. "is it running?" → health.

## The control graph

The Settings tab renders a unified **control graph** (see ``architecture/control-graph``) that fuses preferences + requirements + health + registry into one domain → subsystem → component → requirement hierarchy. Preference toggles, requirement fixes, and per-component reprobes all live there. When an agent needs to understand "is this feature usable right now, and if not, what would unblock it," ``GET /api/control/graph`` answers in one call.

## Capabilities

- ``setup_wizard`` — modes: ``status`` (overview), ``guided`` (interactive setup; step 2 now iterates control-graph domains), ``diagnose`` (deep diagnostic), ``preferences`` (view/edit; returns domain info per component).
- ``feature_status`` — includes ``preferences`` and ``bootstrap_requirements`` sections alongside tool-probe results.
