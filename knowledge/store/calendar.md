---
name: Calendar subsystem (provider-neutral)
kind: integration
description: How work-buddy reads calendar data through a provider-neutral seam (canonical models + Protocol + factory), mirroring the email subsystem. The Obsidian google-calendar bridge is the first, transitional adapter.
entry_points:
- work_buddy.calendar
- work_buddy.calendar.provider
- work_buddy.calendar.models
- work_buddy.calendar.identity
- work_buddy.calendar.providers.obsidian_bridge
- work_buddy.calendar.providers.fake
- work_buddy.calendar.capabilities
- work_buddy.collectors.calendar_collector
tags:
- calendar
- google-calendar
- provider
- adapter
- seam
- eval_js
- bundle
aliases:
- calendar subsystem
- calendar integration
- calendar provider
- calendar core
- calendar seam
---

## Architecture

```
work-buddy agents
   │  MCP
   ▼
calendar capabilities (calendar_*)
   │  CalendarProvider protocol
   ▼
ObsidianBridgeCalendarProvider  (transitional bootstrap adapter)
   │  wraps work_buddy.calendar.env (eval_js)
   ▼
Obsidian google-calendar plugin → Google Calendar API
```

Consumers depend on `work_buddy.calendar.provider` + `work_buddy.calendar.models`, never on `env.py`. A future `google_native` (own-OAuth) adapter replaces the bridge and retires it at read+write parity. Graph / CalDAV / ICS adapters are first-class product goals sequenced by demand — provider neutrality is a product requirement (work-buddy is multi-user), not YAGNI.

## Provider abstraction

`CalendarProvider` is a `@runtime_checkable` Protocol; backends live under `work_buddy.calendar.providers.*`. `get_calendar_provider()` reads `calendar.provider` from config (default `obsidian_bridge`); `fake` selects an in-memory provider for tests; `calendar.enabled: false` short-circuits with `CalendarProviderDisabled`. The protocol declares the full surface (reads + writes) so the contract is stable across PRs; there is no `ensure_calendar` / provisioning method — WB writes to the user's real calendars, not a sandbox.

## Canonical models

`CalendarRef`, `EventTime`, `CalendarEvent`, `CoverageReport` (`work_buddy.calendar.models`). Timezone contract: `EventTime` serializes its offset-aware datetime verbatim (no `.astimezone()` on load) so display matches the source calendar; all-day events stay timezone-free. WB's own timezone decisions read `config.USER_TZ` (the single source of truth).

## Stable keys

`stable_key_for` (`work_buddy.calendar.identity`) returns `ical:<uid>` when an RFC 5545 iCalUID is present, else `loc:<provider>:<calendar_id>:<event_id>`. The bridge payload lacks iCalUID, so bridge events use the `loc:` form; the native adapter supplies real UIDs and dedups the same meeting across calendars.

## Capabilities (reads)

- `calendar_health` — provider readiness probe.
- `calendar_list_events` — events in a date window (optionally per calendar).
- `calendar_get_event` — one event by id (windowed lookup; the plugin's getEvent is broken).
- `calendar_coverage` — which calendars are visible / blacklisted / errored, with per-calendar event counts.

Write capabilities (heavy-consent mutation) live one layer up from the adapter so every provider inherits identical consent gating; they arrive in a later PR.

## Degradation

Gated by the `google_calendar` tool probe: when Obsidian / the plugin isn't reachable the `calendar_*` capabilities are filtered out of the live registry. The collector degrades to a clear 'not available' report rather than crashing the morning bundle.

## Related

- `obsidian/calendar` — the bridge-specific integration (plugin runtime API, write consent, JS snippets).
