---
name: Settings
kind: system
description: Registry-driven Work Buddy settings, authority boundaries, Apps-based information architecture, and persistence.
summary: Settings definitions are owned once, placed into Apps-based pages and sections, validated by their authority, and projected through the React dashboard without conflating configuration with system status.
tags:
- settings
- configuration
- registry
- authority
- dashboard
aliases:
- app settings
- settings registry
- settings broker
entry_points:
- work_buddy.settings
- dashboard-react/src/settings
dev_notes: |-
  Server/profile values live in `db/settings/settings.db`. The store runs the Settings migration ladder once per resolved database path. Definitions, pages, sections, and placements are separate records in the registry; stored values key by setting identity and scope, not navigation path.

  The broker owns typed validation, authority checks, optimistic revision matching, preview, mutation, reset, policy transitions, and event publication. React code consumes same-origin endpoints and keeps page-local lexical search controls mounted while filtering.
---

Settings is the authority and information architecture for configurable Work Buddy behavior.

## Navigation

Application-owned settings are organized under **Apps**, separated by provenance such as Built-in and Community. Journal appears once at `Apps -> Built-in -> Journal`. View- and System-related groups are sections inside the owning App page rather than duplicate global View entries.

The canonical Journal route is `/app/settings/apps/journal`. Contextual settings launchers navigate directly to the owning page. Compatibility routes may redirect there while preserving navigation state; they do not create a second setting identity.

## Registry model

The registry separates:

- a **definition**, which owns identity, type, validation, default, authority, and provenance;
- a **page**, which owns navigation identity;
- a **section**, which groups controls on a page; and
- a **placement**, which places one definition into one section.

A definition may have several placements without duplicating its stored value.

## Authority

Authority is declared per setting. Device-local settings cover presentation and accessibility behavior such as typography. Server/profile settings cover shared domain meaning, such as the Journal day boundary. Native and community contributions can use the same registry shape while receiving different trust and permission grants.

## Broker behavior

The same-origin Settings API exposes registry, values, preview, mutation, and reset operations. Mutations use typed validation, revision checks, authority enforcement, and the dashboard read-only gate. Successful changes publish `settings.changed` to the live UI projection.

Page search is immediate lexical filtering and keeps controls mounted so draft values are not destroyed. Global semantic Settings search is a separate integration boundary and is not supplied by browser calls to the embedding service.

`/app/settings/status` is a projection of the component dependency/control graph. It reports whether Work Buddy can operate and how requirements can be repaired; it is not a bag of ordinary editable settings.

See `services/dashboard/react`, `architecture/control-graph`, and `journal/day-lifecycle`.
