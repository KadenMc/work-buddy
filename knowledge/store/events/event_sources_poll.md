---
name: Event Sources Poll
kind: capability
description: Poll every enabled, due event source once — fetch, diff against the last value, and publish ai.workbuddy.source.<name>.changed when a watched value changes. The periodic tick behind pull-based event sources; normally fired by the event-source-poll cron job, not called by hand.
capability_name: event_sources_poll
category: events
op: op.wb.event_sources_poll
schema_version: wb-capability/v1
parameters: {}
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- events
- source
- poll
- tick
aliases:
- poll event sources
- poll sources
parents:
- events
---

The reconciling poll tick for pull-based event sources. Loads every
`event_source` unit under `.data/event_sources/`, and for each enabled source
whose `interval` has elapsed since its last poll: fetches the payload, extracts
the watched value, and compares its content hash to the stored cursor. On a
**meaningful change** it publishes `ai.workbuddy.source.<name>.changed` onto the
spine — the `source-action` consumer then evaluates the source's condition and
runs its (scoped) action. The first observation is a silent baseline unless the
source sets `cursor.from: all`.

Idempotent and crash-safe: the cursor advances only after a successful poll, and
the spine dedupes on `(source, id)`, so a replay re-fetches without double-firing.
A single tick iterates all due sources — adding a watcher is one `.md`, never a
new thread or cron job.
