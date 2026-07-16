---
name: React Dashboard Calendar Surface
kind: concept
description: Library-neutral calendar model and shared Timeline/List presentation contract.
summary: Calendar widgets consume a Work Buddy temporal model; the private rendering adapter supplies Timeline and List projections without owning providers, credentials, or domain mutations.
tags:
- dashboard
- react
- calendar
- timeline
- temporal
aliases:
- CalendarSurface
- Day Timeline widget
- timeline adapter
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/widget-library/timeline
- dashboard-react/src/dev/calendar-spike
dev_notes: |-
  FullCalendar Standard is the private rendering engine behind the Work Buddy adapter. Dependency objects, event shapes, and layout state must not escape into App contribution contracts or persisted personalization.

  The fixture spike exercises overlap, compact sizing, inspectors, list projection, and event-kind actions. It is a development harness, not a production data provider.
---

`CalendarSurface` is the library-neutral presentation contract for time-oriented dashboard widgets. Calendar providers and Apps own data acquisition, credentials, synchronization, and mutation authority. A widget does not connect directly to Google Calendar or another external account.

## Temporal model

Items distinguish:

- **point records**, which happened at an instant and have no synthetic domain end;
- **timed spans**, which have a start and end; and
- **all-day items**, which use date semantics rather than invented clock times.

A renderer may give a point a minimum visual footprint, but that display affordance does not change the domain data.

Visible ranges are timezone-aware and may follow a logical Journal day rather than civil midnight. Consumers receive resolved instants; they do not reconstruct ranges by adding 24 hours.

## Timeline and List

Timeline and List are two projections of the same item set, selection, inspector, and action-resolution contract. Switching projection does not change what an item means or which actions it supports. Compact layouts retain the projection selector because List is often the more usable small-card presentation.

Overlapping spans remain independently selectable. Short items prioritize title and essential status, then reveal full metadata in the inspector rather than allowing content to clip unpredictably.

## Inspectors and actions

Left-click selection opens the shared inspector, positioned according to available space. Actions resolve by item kind and declared capability. Records, plans, external calendar events, and future App-defined kinds may expose different menus. A provider-owned external event remains read-only when the provider does not grant edit authority.

See `calendar` for provider integration ownership.
