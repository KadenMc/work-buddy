---
name: Feature Cards
kind: concept
description: Component-gated dashboard cards — the reusable pattern for widgets whose existence (and backend work) is justified by an opted-in component.
tags:
- dashboard
- cards
- feature-card
- gate
- preferences
- control
- registry
aliases:
- feature card
- card registry
- dashboard card
- gate AST
- component-gated widget
- wbCardRenderers
- wbMountCards
- DashboardCard
parents:
- architecture
- architecture
dev_notes: |-
  ## SCRIPTS ordering

  ``core/card_registry.py`` must precede every ``tabs/cards/*`` module in the ``SCRIPTS`` list — it initializes ``window.wbCardRenderers``, which the card modules assign into. All card modules must precede ``page.script`` (the standing TDZ rule).

  ## Card renderer scope

  Card renderer modules define functions only; they reference page globals (``fetchJSON``, ``WB_READ_ONLY_MODE``, ``_wbMorphReplace``) which resolve at call time, well after page load — so a bare reference to a ``let``-declared global like ``WB_READ_ONLY_MODE`` is safe inside a renderer body.

  ## Async renderers

  ``wbMountCards`` awaits each renderer, so a renderer that needs its own fetch (e.g. the notification log hitting ``/api/notification-log``) can be ``async`` and return fully-rendered HTML — no post-mount DOM race.

  ## Why preference, not effective_state

  ``cards.active_component_ids()`` reads ``is_wanted()`` directly. An earlier design gated on the control graph's component ``effective_state != "disabled"``, but ``effective_state`` is derived partly from the health probe cache: re-enabling a component leaves it stale-``disabled`` until the next reprobe (~60 s), so a re-enabled card would stay hidden. The preference read is live in both directions, keeping opt-out and opt-in symmetric and instant.
---

work-buddy's dashboard hosts widgets whose existence is justified by an opted-in component — the Obsidian bridge sparkline only makes sense if the user wants Obsidian. A **feature card** is the reusable pattern that ties such a widget to its component: when the component is opted out, the card is not rendered (no placeholder) and the backend work that feeds it stops.

## Gate AST — ``work_buddy/control/gates.py``

A ``Gate`` is a typed boolean expression over component-active state: ``Component`` leaves combined with ``And``, ``Or``, ``Not``. It is JSON-serializable, evaluable in Python and JS, and introspectable.

* ``evaluate(gate, active_ids)`` — ``True`` when the expression holds for the given active-component set. A ``None`` gate is always active.
* ``referenced_components(gate)`` — every ``Component`` id in the tree; used for validation and "which cards depend on X?" diagnostics.
* ``validate(gate, known_components)`` — raises ``ValueError`` if a gate names a component not in ``COMPONENT_CATALOG``.
* ``to_json`` / ``from_json`` — wire format ``{"op": "and"|"or"|"not"|"component", ...}``.
* ``parse_gate(expr)`` — string-DSL convenience: ``parse_gate("obsidian & (thunderbird | outlook)")``. Operators low-to-high precedence: ``|``, ``&``, ``!``; parentheses group.

The same ``Gate`` type is the intended home for future scheduler-side job gating.

## Card registry — ``work_buddy/dashboard/cards.py``

A ``DashboardCard`` descriptor carries ``id`` (namespaced, e.g. ``obsidian.bridge_sparkline``), ``mount_point``, ``gate``, ``mount_slot`` (render order), ``needs_state_keys``, and ``background_jobs``. ``register_card()`` validates the gate at registration. ``cards_for_tab(mount_point)`` returns the active card descriptors in slot order.

**Active = not explicitly opted out.** A component counts as active unless its feature preference is ``unwanted`` (``is_wanted(id) is not False`` — undecided, wanted, required, and core all count as active). The gate evaluates against feature preferences, NOT the control graph's ``effective_state``.

## Endpoint

``GET /api/dashboard/cards/<mount_point>`` → ``{"cards": [{"id", "mount_slot"}, ...]}`` — the active cards for a mount point, gates evaluated server-side. Read-only.

## Frontend — ``core/card_registry.py`` + ``tabs/cards/``

``window.wbCardRenderers`` maps card id → renderer function (sync, returning an HTML string, or async, returning a Promise of one). ``window.wbMountCards(mountPoint, container, state)`` fetches the active list, runs each renderer, and morphdom-merges the result. Each card's renderer lives in its own ``frontend/scripts/tabs/cards/<id>.py`` module.

## First consumer — Settings → Activity

The Settings tab's Activity sub-view is registry-driven. ``loadActivity()`` calls ``wbMountCards('activity', ...)``. Three cards mount there: ``obsidian.bridge_sparkline`` (gated on ``Component("obsidian")``), ``core.event_log``, and ``core.notification_log`` (both ungated).

## Backend gating

The frontend gate hides the card; the backend must independently stop the supporting work, because ``/api/state`` is fetched by every tab regardless of which card renders. ``get_system_state()`` skips ``get_bridge_status()`` when ``is_wanted("obsidian") is False``, so an Obsidian opt-out also halts the bridge probe and its rolling latency history.

## Live re-render

Toggling a preference fires ``component.preference_changed``, which the event bus routes to ``settingsSurface.refresh()``; that re-runs ``loadActivity()``, which re-fetches the gated card list. A card whose component was just opted out disappears within ~250 ms with no page reload; opting back in restores it just as fast.

## Adding a card

1. ``register_card(DashboardCard(id=..., mount_point=..., gate=...))`` in ``cards.py`` (or, for a plugin, from the plugin's own module).
2. Add a ``frontend/scripts/tabs/cards/<id>.py`` module whose ``script()`` registers ``window.wbCardRenderers['<id>']``.
3. Add that module to the ``SCRIPTS`` registry in ``frontend/scripts/__init__.py``.

No edit to the mount point's loader is required — the registry plus endpoint do the rest.

## Deferred

``DashboardCard.background_jobs`` reserves a declaration surface for scheduler-side gating — a scheduled job that should only fire when its supporting components are opted in. The scheduler rule that consumes it is not yet implemented; when built it must evaluate the same ``Gate`` AST.
