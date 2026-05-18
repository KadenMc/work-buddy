---
name: Thread event log (canonical state)
kind: concept
description: Every state-affecting operation produces an event. The current-state cache exists for query convenience but events are authoritative.
summary: Append-only thread_events table with optimistic locking via parent_event_id. Cross-Thread atomic operations share a migration_id. Workflow execution mirrors only execution_started / execution_finished into the Thread log; per-step detail lives on the run record. The Thread log is centrally about RESOLUTION; execution detail is sparingly mirrored.
tags:
- threads
- events
- audit
parents:
- threads
- threads
---

## Schema

``thread_events`` table (see ``work_buddy.threads.store``):
- ``id`` AUTOINCREMENT PK
- ``thread_id`` FK → threads(thread_id) ON DELETE CASCADE
- ``kind`` (one of ``ALL_KINDS`` from ``work_buddy.threads.events``)
- ``actor`` (agent | user | sidecar | fsm_engine | conductor | inciting)
- ``inference_tier`` (ReasoningTier value or NULL)
- ``timestamp``
- ``data_json``
- ``parent_event_id`` (optimistic-lock target)
- ``migration_id`` (cross-Thread linked events)

## Event kinds (catalog)

Lifecycle, inference, confirmation, clarification, redirect, consent, execution, migration, decomposition, budget — see ``work_buddy.threads.events`` for the full list and constants. ``validate_kind()`` enforces at submit.

## Optimistic locking

Each event carries the latest ``parent_event_id`` the actor saw before deciding. The store rejects an insert whose ``parent_event_id`` doesn't match the most recent landed event for that thread (raises ``OptimisticLockConflict``). First-event submissions (``parent_event_id=None``) bypass the check. Two concurrent submissions race; one succeeds, one retries.

## Cross-Thread migrations

When a context item moves between Threads, both Threads emit events sharing a ``migration_id``: ``context_removed`` on the source, ``context_added`` on the destination. ``get_linked_events(migration_id)`` retrieves both.
