---
name: Event Publish
kind: skill
description: Publish one event onto the work-buddy Events backbone — a fire-and-forget fact ("X happened") delivered to 0..N consumers. For manual or agent-initiated emits. The type is reverse-DNS (ai.workbuddy.<domain>.<thing>.<verb>).
category: events
op: op.wb.event_publish
schema_version: wb-skill/v1
parameters:
  type:
    type: str
    description: Reverse-DNS event type, e.g. ai.workbuddy.demo.ping
    required: true
  data:
    type: dict
    description: Opaque JSON-serializable payload
    required: false
  source:
    type: str
    description: URI-ref identifying the producer (default /wb/agent)
    required: false
  durable:
    type: bool
    description: If true (default), the event is logged + delivered at-least-once; if false it is a lossy UI-only fan-out
    required: false
  subject:
    type: str
    description: Optional CloudEvents subject
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
skill_name: event_publish
tags:
- events
- event
- publish
- emit
aliases:
- emit event
- publish event
- fire event
parents:
- events
---

Publish a single event onto the Events backbone. Durable events are appended to
`db/events` (deduped on `(source, id)`) and delivered at-least-once to any
registered consumers whose type filter matches; they also fan out immediately to
the lossy dashboard projection. `durable=false` events skip the log entirely
(lossy fan-out only — for UI refresh / heartbeats).

This is the controlled producer used to exercise the spine end-to-end. It is a
*fact* emitter, not a command — to make something happen, a consumer reacts to
the event.
