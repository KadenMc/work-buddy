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

  Durable widgets live in `dashboard-react/src/dashboard/widgets/durable/`: a keep-alive host above the grid owns one permanent wrapper per instance, portals the live `WidgetHost` in once, and light placeholder cells re-home the wrapper with appendChild when the grid remounts. The durable path pins `interactionMode` to operate (the draft-scope re-key is also structurally unreachable because durable forbids drafts) and maps a failed re-hydration with a previous good snapshot to a stale banner instead of unmounting. The navbar entry seam is `dashboard-react/src/dashboard/customize/` (a registration-handle controller; only the grid view host registers). Validation enforces durable implies single-instance and no drafts. Contract prose lives in `dashboard-react/ARCHITECTURE.md`.

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

Dashboard Core owns layout editing, constraint enforcement, collision feedback, reset, undo/redo, and portable personalization patches. The grid library remains an implementation detail rather than part of persisted view state. The Customize view entry control lives in the app shell navbar and activates when the mounted view registers a customize session, so grid views everywhere share one entry point while other surfaces leave it disabled.

Desktop customization uses the grid. Mobile uses document flow and drag-reordering of a canonical sequence. Responsive changes may reflow or scroll content, but they must not silently remove primary controls or hide that a capability exists.

## Operate, Arrange, and Preview

- **Operate** enables normal widget interaction and outward effects.
- **Arrange** enables layout controls while widget bodies remain inert.
- **Preview** freezes layout, forks drafts, permits local interaction, and simulates or blocks outward effects.

Canceling Preview discards the forked preview state; it never claims to roll back an effect that already reached another system.

App-owned durable widgets are the exception to both Arrange inertness and Preview's draft fork and effect simulation. A durable widget owns its own persistence and stays live in Operate, Arrange, and Preview alike, so its edits are always real and saved, never sandboxed. Because Preview then has nothing to sandbox on an all-durable view, such a view offers Arrange only, a generic rule keyed off `definition.durable`. A mixed view keeps Preview with honest copy that says the standard widgets are simulated while the live cards stay live and save.

## App-owned durable widgets

A widget definition may declare itself durable. Dashboard Core then keeps its renderer mounted for the life of the view in a keep-alive host above the grid and re-homes the same DOM into the widget's cell across layout remounts, so live client state such as an editor's document, cursor, and scroll survives customize toggles, interaction recovery, and the mobile switch. A durable widget is one cohesive App-owned surface. Like a single-surface view it may hold live state and talk to its own routes and the event stream directly, while every identity, input, and dispatch invariant is retained: its snapshot input stays JSON, it emits declared intents, and it never receives the provider or mutates a sibling. A durable widget owns its own persistence and saves its live state through its own app-owned seams, so its edits are always real and it declares one instance per view and no host drafts. The Co-work workspace card is the first durable widget.

## Host-owned working state and interaction surfaces

Widgets declare meaningful drafts; the host owns persistence, schema versions, revisions, clearing, and cross-tab behavior. Draft identity includes profile/workspace, publisher App, view, widget instance, widget type, draft name, and scope. Widgets do not persist arbitrary DOM inputs or create incompatible storage formats.

Short-lived notices and confirmation requests are reusable dashboard infrastructure. They are distinct from the durable notification/request system. Contextual Hover Help is another host mode with layered ownership: Dashboard, view placement, widget, and primitive. Help and Customize are mutually exclusive.

See `services/dashboard/react` for contribution hosting and migration boundaries, and `services/dashboard/react/appearance` for the visual contract every widget must honor.
