---
name: Calendar Health
kind: capability
description: Liveness probe for the calendar provider. Returns the configured adapter's readiness payload (ready flag, calendar count, plugin version). Use it to distinguish 'bridge/provider down' from 'no calendars visible' when calendar features are missing.
capability_name: calendar_health
category: calendar
op: op.wb.calendar_health
schema_version: wb-capability/v1
tags:
- calendar
- health
aliases:
- calendar health
- calendar provider status
- is calendar working
- calendar bridge status
parents:
- calendar
requires:
- google_calendar
---
