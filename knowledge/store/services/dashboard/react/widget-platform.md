---
name: React Dashboard Widget Platform
kind: concept
description: Standard React widget composition, identity, layout, drafts, help, and interaction contracts.
summary: Views compose reusable widget types into purpose-bearing slots and placed instances while Dashboard Core owns layout, safety modes, drafts, help, and shared interaction surfaces.
tags:
- dashboard
- react
- widgets
- views
- layout
- drafts
- interactions
aliases:
- work-buddy widgets
- widget runtime
- view composition
- widget slots
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/dashboard
- dashboard-react/src/widget-library
dev_notes: |-
  The desktop grid engine is private behind `ReactGridLayoutAdapter`; persisted personalization remains library-neutral. The desktop layout uses preserved outer gaps, collision prevention, explicit tidy behavior, and all-edge resize affordances. Mobile renders normal document flow from a persisted canonical order.

  Draft repositories use schema-versioned records, compare-and-swap revisions, retention metadata, and cross-tab signaling. Production uses IndexedDB; tests inject in-memory repositories. Arrange and Preview safety is enforced by the host from declared intent effects, not by inspecting DOM elements or HTTP methods.
---

The widget platform is the standard composition model for React dashboard views. It preserves extensive personalization without turning every shareable App into an unrelated page implementation.

## Three identities

Never collapse these identities into one namespace:

1. **Widget type** identifies a reusable renderer and its typed contract, such as Quick Capture.
2. **View slot** identifies the purpose a widget serves in one view, such as the Journal capture role.
3. **Widget instance** identifies one placement, layout record, and local-state owner.

A required slot may accept a replacement renderer that satisfies the same contract. Requiredness therefore attaches to purpose, not permanently to a default widget type.

## Placement availability

Each slot is one of:

- **required** — removing it would make the view unusable or violate its core purpose;
- **default-on** — present in the recommended layout but removable; or
- **default-off** — available from the catalog without occupying the initial layout.

Journal requires Capture and Day Timeline. Running Notes is default-on.

## Renderer boundary

Renderers receive typed UI input and emit declared intents. Local presentation state can remain local. Outward reads, mutations, navigation, downloads, or external actions declare semantic effects so the host can enforce mode safety without guessing from buttons, keyboard events, timers, or fetch calls.

## Layout and personalization

Dashboard Core owns layout editing, constraint enforcement, collision feedback, reset, undo/redo, and portable personalization patches. The grid library remains an implementation detail rather than part of persisted view state.

Desktop customization uses the grid. Mobile uses document flow and drag-reordering of a canonical sequence. Responsive changes may reflow or scroll content, but they must not silently remove primary controls or hide that a capability exists.

## Operate, Arrange, and Preview

- **Operate** enables normal widget interaction and outward effects.
- **Arrange** enables layout controls while widget bodies remain inert.
- **Preview** freezes layout, forks drafts, permits local interaction, and simulates or blocks outward effects.

Canceling Preview discards the forked preview state; it never claims to roll back an effect that already reached another system.

## Host-owned working state and interaction surfaces

Widgets declare meaningful drafts; the host owns persistence, schema versions, revisions, clearing, and cross-tab behavior. Draft identity includes profile/workspace, publisher App, view, widget instance, widget type, draft name, and scope. Widgets do not persist arbitrary DOM inputs or create incompatible storage formats.

Short-lived notices and confirmation requests are reusable dashboard infrastructure. They are distinct from the durable notification/request system. Contextual Hover Help is another host mode with layered ownership: Dashboard, view placement, widget, and primitive. Help and Customize are mutually exclusive.

See `services/dashboard/react` for contribution hosting and migration boundaries, and `services/dashboard/react/appearance` for the visual contract every widget must honor.
