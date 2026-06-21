---
name: Event Source List
kind: capability
description: List authored event sources — name, type, interval, enabled state, action, condition, semantic (Tier-3 gate present?), and autonomy — plus any sources that failed validation and why. Read-only.
capability_name: event_source_list
category: events
op: op.wb.event_source_list
schema_version: wb-capability/v1
parameters: {}
mutates_state: false
retry_policy: none
is_action: false
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- events
- source
- list
aliases:
- list event sources
- list sources
parents:
- events
---

List every authored event source under `.data/event_sources/`. Valid sources are
returned with their `name`, `type`, poll `interval_s`, `enabled` flag, `action`,
`allowed_actions`, `condition`, and `autonomy`; sources that fail validation are
returned in `errors` with the specific reasons, so a malformed `.md` is visible
rather than silently skipped.
