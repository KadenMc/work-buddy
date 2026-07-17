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
- work_buddy.calendar.providers.google_native
- work_buddy.calendar.providers.fake
- work_buddy.calendar.google_auth
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

Consumers depend on `work_buddy.calendar.provider` + `work_buddy.calendar.models`, never on `env.py`. The `google_native` (own-OAuth) adapter reads and writes Google Calendar directly (no Obsidian dependency) and is the path that retires the bridge for Google at read+write parity. Graph / CalDAV / ICS adapters are first-class product goals sequenced by demand — provider neutrality is a product requirement (work-buddy is multi-user), not YAGNI.

## Provider abstraction

`CalendarProvider` is a `@runtime_checkable` Protocol; backends live under `work_buddy.calendar.providers.*`. `get_calendar_provider()` reads `calendar.provider` from config (default `obsidian_bridge`; `google_native` for direct own-OAuth; `fake` in-memory for tests); `calendar.enabled: false` short-circuits with `CalendarProviderDisabled`. The protocol declares the full surface (reads + writes); there is no `ensure_calendar` / provisioning method — WB writes to the user's real calendars, not a sandbox.

`google_native` auth lives in `work_buddy.calendar.google_auth`: a persisted OAuth token (under the data root) refreshed silently via `google-auth`; the interactive consent flow (`google-auth-oauthlib`) is a setup-time step surfaced through the `google_calendar_native` health component (`/wb-setup`). Least-privilege scopes `calendar.events` + `calendar.calendarlist.readonly` (listing calendars is a separate Google permission from event CRUD); publish the OAuth consent screen to Production so the refresh token does not expire.

## Canonical models

`CalendarRef`, `EventTime`, `CalendarEvent`, `CoverageReport` (`work_buddy.calendar.models`). Timezone contract: `EventTime` serializes its offset-aware datetime verbatim (no `.astimezone()` on load) so display matches the source calendar; all-day events stay timezone-free. WB's own timezone decisions read `config.USER_TZ` (the single source of truth).

## Stable keys

`stable_key_for` (`work_buddy.calendar.identity`) returns `ical:<uid>` when an RFC 5545 iCalUID is present, else `loc:<provider>:<calendar_id>:<event_id>`. The bridge payload lacks iCalUID, so bridge events use the `loc:` form; the native adapter supplies real UIDs and dedups the same meeting across calendars.

## Capabilities

Reads:
- `calendar_health` — provider readiness probe.
- `calendar_list_events` — events in a date window (optionally per calendar).
- `calendar_get_event` — one event by id (windowed lookup on the bridge; a real `events.get` on native).
- `calendar_coverage` — which calendars are visible / blacklisted / errored, with per-calendar event counts.

Writes (heavy, per-change consent — see `obsidian/calendar` and the capability layer):
- `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` — mutate the user's real calendars. Consent lives one layer up from the adapter (in `capabilities.py`) so every provider inherits identical gating: per-write `consent_weight="high"`, a change-specific prompt body (target calendar, title, start–end in `USER_TZ`, before→after diff), `default_ttl=0` on update/delete (always re-prompt), and a shared-calendar escalation.

## Degradation

The `calendar_*` capabilities are gated by the provider-aware `calendar` tool probe, which dispatches on `calendar.provider`: bridge → the cached Obsidian plugin check; `google_native` → the OAuth token is present (no Obsidian); `fake` → always. So the capabilities follow whichever provider is configured rather than hard-depending on Obsidian. Deep native API health is a separate `google_calendar_native` health component (diagnose-only). Either way the collector degrades to a clear 'not available' report rather than crashing the morning bundle.

## Related

- `obsidian/calendar` — the bridge-specific integration (plugin runtime API, write consent, JS snippets).
- `services/dashboard/react/calendar-surface` — the React presentation contract. It consumes canonical temporal data and keeps the rendering library private; provider selection, credentials, synchronization, and write authority remain in this subsystem.
