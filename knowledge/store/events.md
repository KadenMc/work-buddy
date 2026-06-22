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
dev_notes: '**The inclusion rule is the boundary**: only *event-shaped* comms ride the spine — fire-and-forget facts, 0..N consumers, no response expected. RPC/commands (`wb_run`, embedding, broker), observability surfaces (the dashboard `EventBus`, the sidecar `EventLog`), and supervision (health/evictors) are excluded *by category*, not by omission — routing request/response through an at-least-once tick-drained log adds latency and breaks the return channel. **+1 thread, period**: one `event-drain` thread iterates ALL consumers; a consumer is a registry entry, not a thread (idle = one dict entry). A *blocking* handler would stall the single drain — keep handlers fast (offload to a shared bounded pool later if needed). **Cross-process is via the shared SQLite file**: an `event_publish` from the gateway appends to `db/events`; the sidecar drain reads it — no IPC. The lossy dashboard fan-out is the existing `dashboard.events.publish_auto` (sidecar → messaging bridge → SSE), best-effort. **Retention** is offset-aware: a row is kept while undelivered (`seq > min_live_offset`), in the DLQ, or — *external events only* — inside the 7-day replay window; everything else reaps on a per-type `expires_at` (noisy `schedule.tick` ~3h). **Activation**: a new op (`event_publish`) needs a one-time gateway+sidecar restart (new Op code is not picked up by the data-only `reload_capability_data`). **Threads is a sink, never a producer** (no `ThreadsAdapter`). **Source layer**: the producer/reaction split is load-bearing — the poller (`sources/poller.py`) only fetches→diffs→publishes `ai.workbuddy.source.<name>.changed`; condition eval + the scoped action live in `SourceActionProcessor` (`consumers/source_action.py`, a `type_prefix` subscriber), so the reaction inherits at-least-once + DLQ. The rate-limit fire-log (`<state>/<name>.fires.json`) is SEPARATE from the cursor file (`<name>.json`): different writers (consumer vs poller), so one shared file would clobber. celpy ships no `abs`/`math.*` — `conditions/cel.py:_cel_functions` registers a curated `abs`; the activation exposes `event.data`/`prev.data` (+ a `current` shorthand) with no bare `prev` scalar (it would collide with the `prev` map). `event_source_dry_run` takes `name` (saved) OR `proposal` (the unsaved `event_source_create` field set) so the `/wb-event-new` confirm gate precedes any write. Actions are `Action(name, run, consent_action, consent_weight)` records (not `Processor` instances) because an action needs the source def for `action_params`; `registry.known_actions()` must stay in sync with `definition.KNOWN_ACTIONS` (a test asserts it). The `event-source-poll` cron re-loads every source each tick, so a new/edited source needs no hot-reload hook. **Tier-3 semantic gate** (`conditions/semantic_llm.py`): runs LAST in `SourceActionProcessor` (after every cheap gate) so a search + local-model call never happens for a fire a cheaper gate already closed; fail-closed throughout. `classify_evidence` already broker-admits at BACKGROUND — NEVER self-wrap. The content-hash prefilter hashes the SEARCH RESULTS (what classify sees), not the polled value — CEL already gated on the polled value changing, so a poll-value hash would never match between evals. State is its own `<name>.semantic.json` (separate writer; never the cursor or the fire-log). `event_source_dry_run` reports the gate but only *evaluates* it when `run_semantic=true`, in an ephemeral temp state dir so the preview stays side-effect-free.'
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
- **`dispatcher.py`** — `publish()` (append + fan-out) and `drain()` (at-least-once delivery, offsets, bounded-retry→DLQ) + a consumer registry. A consumer subscribes by an exact `types` set OR a `type_prefix` (e.g. `ai.workbuddy.source.`).
- **`drain.py`** — the single `event-drain` daemon thread (the backbone's only added thread).
- **`policy.py`** — `policy_check` at the processor boundary (consent enforced *between* dispatch and `Processor.run`, reusing `ConsentCache`).
- **`artifact.py`** — registers `events` as an artifact so the existing `artifact_cleanup` sweep bounds it (offset-aware retention predicate).
- **`protocol.py`** — `Source` / `Processor` / `Condition` ports. Pull `Source`s and `Condition`s are implemented (see *Sources* below); `push` sources (webhook ingress) remain a designed-for extension.

## Not the dashboard event-bus

Distinct from `architecture/event-bus` (`dashboard/events.py`) — that is the *lossy SSE fan-out* for real-time UI. The durable spine *feeds* it as a projection; it is never the spine.

## Producers and consumers

- `event_publish` capability — manual/agent emit (the controlled path).
- thin `CronAdapter` (`producers/cron.py`) — emits `ai.workbuddy.schedule.tick` after `scheduler.tick()`; additive (the scheduler is unmodified), throttled, short-TTL.
- the **source poller** (`sources/poller.py`, fired by the `event-source-poll` cron) — the first real external producer: fetch → diff vs the stored cursor → publish `ai.workbuddy.source.<name>.changed`.
- `notify-demo` consumer (`consumers/notify_demo.py`) — writes a notification on `ai.workbuddy.demo.ping` (the visible end-to-end effect).
- the **source-action consumer** (`consumers/source_action.py`, a `type_prefix` subscriber on `ai.workbuddy.source.`) — reacts to a source change: evaluates the source's condition, then runs its scoped action.

## Sources — user-authored pull watchers

A **source** is a durable `.md` under `.data/event_sources/` that polls some state and fires a notification on a *meaningful change* (a diff, not every fetch). The split is deliberately spine-shaped: the **poller is the producer** (fetch → diff → publish on change) and the **reaction lives in a consumer on the drain** (condition + scoped action), so the reaction inherits at-least-once delivery + DLQ for free.

- **Producer** (`sources/`): `http_poll` fetch → `extract` (JSONPath / CSS / whole-payload hash) → content-hash diff vs a per-source cursor → publish `ai.workbuddy.source.<name>.changed` carrying `{current, prev}`. The cursor advances only after a successful poll (crash-safe; the spine dedupes a re-emit). The first observation is a silent baseline unless the source sets `cursor.from: all`.
- **Condition** — a cheapest-first cascade: a **CEL** predicate (`conditions/cel.py`) over `event.data` / `prev.data` (safe, non-Turing-complete, fail-closed), plus an optional **Tier-3 semantic-LLM gate** (`conditions/semantic_llm.py`) that runs *only after* CEL passes — it web-searches and asks a local model *"is this material?"* (`websearch.classify_evidence`, BACKGROUND priority). The semantic tier is guarded (results content-hash prefilter + post-fire cooldown + N-of-M debounce) and **fail-closed** — any error means no fire.
- **Action** (`processors/`): the scoped sink — only `notify` exists, gated by the source's `allowed_actions` + per-action consent, under `autonomy: notify_only`. A source that exceeds `max_per_hour` auto-suspends itself.
- **Authoring**: `/wb-event-new` (workflow `events/event-new`) elicits → proposes a grounded draft → dry-runs it with zero side effects → confirms → activates. `event_source_create` / `event_source_dry_run` / `event_source_list` / `event_source_toggle` are the ops behind it.

## Still designed-for (gated on a concrete consumer)

A webhook ingress (`push` sources), `auto_execute` autonomy beyond `notify_only`, and an n8n/Node-RED `external_flow` processor are designed-for but not built. Other internal mechanisms (`MessagePoller`, pipelines, the retry queue as the durable outbox) move onto the spine **only when a real second consumer needs their events**, not as a blanket migration.
