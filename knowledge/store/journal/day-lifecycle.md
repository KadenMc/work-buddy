---
name: Journal Day Lifecycle
kind: concept
description: Timezone-aware Journal logical-day windows, boundary policy, DST handling, and safe transitions.
summary: Journal today is resolved from a configured IANA timezone and local boundary; windows are half-open, DST-aware, and versioned through policy epochs so existing days are not silently reinterpreted.
tags:
- journal
- timezone
- day-boundary
- dst
- lifecycle
aliases:
- logical Journal day
- Journal day boundary
- day policy
parents:
- journal
entry_points:
- work_buddy.journal_day
- work_buddy.settings.broker
dev_notes: |-
  `work_buddy.journal_day` owns parsing and resolution helpers. Durable policy, pending transition, and policy epochs live in `settings.db`; the configured value is bootstrap input and the revisioned Settings row becomes authority.

  Boundary transitions take effect at the next safe boundary and record the previous policy epoch. Callers consume `JournalDayWindow` values and must not reproduce timezone arithmetic independently.
---

Journal `today` is the active logical Journal day, not necessarily the current civil date.

## Policy

The active policy contains an IANA timezone and a local boundary time. An instant belongs to the half-open window `[start, end)` for one logical date. The backend resolves that date and returns aware start/end instants to UI and downstream consumers.

The built-in default boundary is `05:00`, but it is a configurable Journal policy rather than a universal invariant.

Callers must not compute the end by adding 24 hours. Day length can differ around daylight-saving transitions, and browser or host timezone inference can disagree with Work Buddy's configured timezone.

## DST behavior

Local boundary construction resolves gaps and folds explicitly. A nonexistent wall time advances to the first valid instant. An ambiguous wall time uses the policy's deterministic fold rule. The resulting adjacent windows remain contiguous and non-overlapping.

## Policy changes

Changing the boundary creates a pending transition that becomes effective at the next safe day boundary. Policy epochs retain the timezone, boundary, and effective range under which days were created. Existing Journal days are not silently reassigned to a different window.

`journal_state` exposes logical date, timezone, boundary, window start/end, and policy transition metadata. UI surfaces display those values rather than hardcoding one user's boundary or timezone.

The configured logical-day policy replaces early-morning prompts that ask whether the user means today or yesterday. Explicit date targets remain explicit.

See `journal/journal_state`, `settings`, and `services/dashboard/react/calendar-surface`.
