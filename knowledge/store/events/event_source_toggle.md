---
name: Event Source Toggle
kind: capability
description: Enable or disable an authored event source by rewriting its .md. A disabled source is skipped by the poll tick (no fetch, no fire) but its definition and cursor are preserved.
capability_name: event_source_toggle
category: events
op: op.wb.event_source_toggle
schema_version: wb-capability/v1
parameters:
  name:
    type: str
    description: The event source name (the .md file stem)
    required: true
  enabled:
    type: bool
    description: True to enable polling, False to suspend it
    required: true
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- events
- source
- toggle
- enable
- disable
aliases:
- enable event source
- disable event source
- suspend source
parents:
- events
---

Flip an event source's `enabled` flag, rewriting its `.md` in place. A disabled
source is skipped by the poll tick — no fetch, no diff, no fire — while its
definition and cursor state are preserved, so re-enabling resumes from where it
left off. Used by the rate-limit auto-suspend path and for manual pause/resume.
