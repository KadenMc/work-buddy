---
name: Events backbone
kind: integration
description: The durable, in-process delivery spine for event-shaped facts — a thing happened ("X happened"), emitted or received, published to 0..N consumers that may react asynchronously. A CloudEvents-superset envelope + a SQLite log (inbox-dedup + per-consumer offsets + DLQ) + one drain thread + a consent gate. RPC, observability, and supervision are excluded by category (the inclusion rule).
entry_points:
- work_buddy.events
- work_buddy.events.dispatcher
- work_buddy.events.store
- work_buddy.events.envelope
- work_buddy.events.protocol
tags:
- events
- backbone
- spine
- cloudevents
- dispatcher
- pub-sub
aliases:
- event backbone
- events system
- event spine
- durable event bus
dev_notes: '**The inclusion rule is the boundary**: only *event-shaped* comms ride the spine — fire-and-forget facts, 0..N consumers, no response expected. RPC/commands (`wb_run`, embedding, broker), observability surfaces (the dashboard `EventBus`, the sidecar `EventLog`), and supervision (health/evictors) are excluded *by category*, not by omission — routing request/response through an at-least-once tick-drained log adds latency and breaks the return channel. **+1 thread, period**: one `event-drain` thread iterates ALL consumers; a consumer is a registry entry, not a thread (idle = one dict entry). A *blocking* handler would stall the single drain — keep handlers fast (offload to a shared bounded pool later if needed). **Cross-process is via the shared SQLite file**: an `event_publish` from the gateway appends to `db/events`; the sidecar drain reads it — no IPC. The lossy dashboard fan-out is the existing `dashboard.events.publish_auto` (sidecar → messaging bridge → SSE), best-effort. **Retention** is offset-aware: a row is kept while undelivered (`seq > min_live_offset`), in the DLQ, or — *external events only* — inside the 7-day replay window; everything else reaps on a per-type `expires_at` (noisy `schedule.tick` ~3h). **Activation**: a new op (`event_publish`) needs a one-time gateway+sidecar restart (new Op code is not picked up by the data-only `reload_capability_data`). **Threads is a sink, never a producer** (no `ThreadsAdapter`).'
---

## Why it exists

work-buddy already had ~5 event-shaped mechanisms reinvented privately (scheduler ticks, message poller, source pipelines, the threads FSM log, the dashboard bus). The Events backbone is the **one canonical spine** those become producers/consumers of — so external webhooks and cron-poll-diffs are *just two more producers of the same `Event`*, not an Nth ad-hoc mechanism.

## The inclusion rule (what is on the spine)

An **Event** is a durable, fire-and-forget announcement that "X happened" — observed from outside (webhook, poll) **or declared by work-buddy itself** — published to 0..N consumers that react asynchronously. A piece of inter-component communication joins the spine only if it is fire-and-forget, broadcast (0..N), a past-fact (not "do Y"), and async-tolerant. The instant a response is required it is a command/query/RPC and stays on its existing rails (CloudEvents: "events represent facts and therefore do not include a destination").

## Architecture (the spine)

```
producer ──publish()──►  EventStore.append (db/events, UNIQUE(source,id) = dedup)
                          │                       └─ immediate lossy fan-out → dashboard bus (projection)
                          ▼
                 event-drain thread (~45s)  ──drain()──►  per-consumer: read_since(offset) →
                          policy_check (consent gate) → Processor.run → commit offset
                          (bounded retry → event_dlq on poison)
```

- **`envelope.py`** — frozen `Event` (CloudEvents core + work-buddy extension attrs). The general form of the in-tree `thread_events` / `work_item_events` logs; the envelope is kept field-compatible with `work_item_events`.
- **`store.py`** — `EventStore` over `db/events`: `events` (the log + per-row `expires_at`), `consumer_offsets` (one watermark each — restart replay), `event_dlq`. `append` returns `None` on a `(source,id)` duplicate.
- **`dispatcher.py`** — `publish()` (append + fan-out) and `drain()` (at-least-once delivery, offsets, bounded-retry→DLQ) + a consumer registry.
- **`drain.py`** — the single `event-drain` daemon thread (the backbone's only added thread).
- **`policy.py`** — `policy_check` at the processor boundary (consent enforced *between* dispatch and `Processor.run`, reusing `ConsentCache`).
- **`artifact.py`** — registers `events` as an artifact so the existing `artifact_cleanup` sweep bounds it (offset-aware retention predicate).
- **`protocol.py`** — `Source` / `Processor` / `Condition` ports (interfaces defined; concrete Sources/Conditions are not yet implemented).

## Not the dashboard event-bus

Distinct from `architecture/event-bus` (`dashboard/events.py`) — that is the *lossy SSE fan-out* for real-time UI. The durable spine *feeds* it as a projection; it is never the spine.

## Producers and the demo consumer

- `event_publish` capability — manual/agent emit (the controlled path).
- thin `CronAdapter` (`producers/cron.py`) — emits `ai.workbuddy.schedule.tick` after `scheduler.tick()`; additive (the scheduler is unmodified), throttled, short-TTL.
- `notify-demo` consumer (`consumers/notify_demo.py`) — writes a notification on `ai.workbuddy.demo.ping` (the visible end-to-end effect).

## Extension points (not yet implemented)

The `Source` and `Condition` protocols are the designed extension surface for *user-defined* events — polling/webhook sources with diff conditions (e.g. a stock-watcher), authored conversationally. Other internal mechanisms (`MessagePoller`, pipelines, the retry queue as the durable outbox) move onto the spine **only when a real second consumer needs their events**, not as a blanket migration. A webhook ingress and an n8n/Node-RED `external_flow` processor are designed-for but gated on a concrete consumer.
