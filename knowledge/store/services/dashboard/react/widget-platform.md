---
name: React Dashboard Widget Platform
kind: concept
description: Standard React widget composition, identity, placement, and library-neutral layout contracts.
summary: Views compose reusable widget types into purpose-bearing slots and placed instances while Dashboard Core owns validation, layout, and personalization portability.
tags:
- dashboard
- react
- widgets
- views
- layout
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
  The desktop grid engine is private behind `ReactGridLayoutAdapter`; persisted personalization remains library-neutral. Mobile renders normal document flow from a persisted canonical order. Contribution validation rejects unsupported roots, incompatible slots, untrusted capabilities, and dependency-specific persisted state.
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

Renderers receive typed UI input and emit declared intents. Local presentation state can remain local. Renderers do not fetch global context or external provider data directly; an App/provider binds the input and handles outward intent.

## Layout and personalization

Dashboard Core owns layout editing, constraint enforcement, collision feedback, reset, undo/redo, and portable personalization patches. The grid library remains an implementation detail rather than part of persisted view state.

Desktop customization uses the grid. Mobile uses document flow and drag-reordering of a canonical sequence. Responsive changes may reflow or scroll content, but they must not silently remove primary controls or hide that a capability exists.

See `services/dashboard/react` for contribution hosting and migration boundaries, and `services/dashboard/react/appearance` for the visual contract every widget must honor.
