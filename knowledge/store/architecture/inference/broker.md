---
name: Local Inference Broker
kind: reference
description: Admission-control + priority scheduling + per-call metrics for every local-inference call. Work-buddy is the scheduler of record for LM Studio / LM Link traffic, not LM Studio itself.
summary: 'Process-global broker (``work_buddy.inference.get_broker()``) wraps every outbound local-inference call. Per-profile max_concurrent + max_queued. Three priority classes (INTERACTIVE, WORKFLOW, BACKGROUND) with priority-aware admission. Distinct error kinds: QueueFull, QueueWaitTimeout, InferenceTimeout. Metrics ring buffer (queued_at, admitted_at, started_http_at, first_token_at, finished_at + latency splits) for the dashboard.'
entry_points:
- work_buddy.inference.broker
- work_buddy.inference
tags:
- broker
- admission-control
- priority
- lmstudio
- lm-link
- slot-limit
- metrics
- inference
- local-inference
aliases:
- LocalInferenceBroker
- inference broker
- local inference broker
- broker
- slot broker
- priority queue
parents:
- architecture/inference
- architecture/inference
---

## Why this exists

LM Studio has an internal queue and concurrency slots (``Max Concurrent Predictions``, default 4), but its public API does NOT expose current slot occupancy. A naive caller hitting ``/v1/chat/completions`` or ``/v1/embeddings`` can sit inside LM Studio's hidden queue until the caller's timeout fires — and work-buddy would see "LM Studio is slow" with no way to tell whether the slow bit was queue-wait or actual inference. Worse: a background bulk encode can starve an interactive dashboard search because both hit the same server and the server has no notion of *our* priorities.

The broker fixes that by making **work-buddy** the scheduler of record for local inference. Every outbound call (embedding provider, both LLM backends) routes through ``broker.slot(...)`` before the HTTP call is made.

## Public API

```python
from work_buddy.inference import get_broker, Priority

broker = get_broker()  # process-global singleton

with broker.slot(
    profile="lmstudio:text-embedding-snowflake-arctic-embed-m-v1.5",
    priority=Priority.BACKGROUND,
    queue_wait_s=15.0,
    inference_s=20.0,
) as ticket:
    ticket.mark_started_http()
    vecs = httpx_post(...)
```

On ``__exit__``, the ticket releases the slot and the call's metrics land in the ring buffer. Metrics are readable via ``broker.snapshot_metrics()``; current per-profile occupancy via ``broker.profile_status()``.

``work_buddy.inference.parse_priority(value)`` maps a case-insensitive name string (``"interactive"`` / ``"workflow"`` / ``"background"``) — or a ``Priority`` / ``None`` passthrough — onto the enum. It exists for the MCP boundary, where capability params (``llm_call`` / ``llm_submit``) arrive as JSON strings and must map onto the enum before reaching the broker.

## Priority classes

Three classes, fixed-priority admission across + FIFO within:

- ``INTERACTIVE`` (0) — user-facing / UI-driven requests (dashboard search, agent response). Must not sit behind background work.
- ``WORKFLOW`` (1) — agent-initiated work tied to a user task but not UI-facing. Default for LLM backend calls.
- ``BACKGROUND`` (2) — cron jobs, bulk index rebuilds. Default for embedding provider calls. Yields to everything else.

Lower numeric = higher priority. A queued INTERACTIVE ticket admits ahead of a queued BACKGROUND ticket on the same profile when a slot frees up.

## Split timeouts

``queue_wait_s`` = max time to wait for a slot. ``inference_s`` = max time the HTTP call itself may take (enforced by the caller's ``httpx.Client(timeout=...)``; broker records the budget for observability). Two distinct failure modes with distinct error classes:

- ``QueueFull`` — per-priority queue is at capacity. Raised synchronously, caller should back off.
- ``QueueWaitTimeout`` — admitted-wait budget exhausted. Distinct from ``InferenceTimeout`` so operators can tell "sat in our queue" apart from "model is slow."

## Profile naming convention

Each call site uses a prefix so slot limits stay independent:

- ``lmstudio:<model_id>`` — embedding provider (``work_buddy.embedding.providers.lmstudio.encode``).
- ``lmstudio_native:<model>`` — LM Studio native-chat tool-call loop (``work_buddy.llm.backends.lmstudio_native.call_lmstudio_native``).
- ``openai_compat:<model>`` — OpenAI-compatible chat-completions (``work_buddy.llm.backends.openai_compat.call_openai_compat``).

Same model id on the same physical LM Studio instance gets up to three independent logical profiles — so an active embedding bulk-encode can't starve a chat call (and vice versa), even though they talk to the same server.

## Per-profile config

``inference.profiles.<name>`` in config.yaml:

```yaml
inference:
  profiles:
    lmstudio:text-embedding-snowflake-arctic-embed-m-v1.5:
      max_concurrent: 1     # LM Studio client-side admission cap
      max_queued: 16        # per-priority queue depth
      default_queue_wait_s: 15
      default_inference_s: 60
```

Unregistered profiles auto-register on first use with conservative defaults (max_concurrent=1, max_queued=32). Default ``max_concurrent=1`` is the safe starting point because LM Studio's real capacity is config-dependent and not discoverable from the API; bump once measured.

## Metrics

``SlotMetrics`` rows in a 1000-entry ring buffer, one per slot admission. Fields: ``id, profile, priority, queued_at, admitted_at, started_http_at, first_token_at, finished_at, status, error_kind, error_detail``, plus computed splits ``queue_wait_ms, service_time_ms, total_latency_ms``. ``status`` is one of ``queued / running / ok / queue_full / queue_wait_timeout / inference_timeout / error``. Read via ``broker.snapshot_metrics(limit=...)``.

Completed calls are also persisted out-of-band to a SQLite store (``work_buddy/inference/metrics_store.py``, the ``broker-metrics`` artifact, 7-day per-record TTL) by a flusher daemon in the embedding service, so per-call latency survives a process restart that wipes the in-memory ring. The dashboard reads that store to join scheduler latency (queue-wait / service) onto local rows of the cross-provider Inference-activity feed by ``call_id`` — see ``architecture/inference/provenance``.

## Important limitation: process-global singleton

The broker is one instance **per Python process**. The MCP gateway, the embedding service, and the dashboard each have their own broker — they do NOT share state. Reading ``get_broker().snapshot_metrics()`` from process A returns only calls originating in A.

Practical consequence: the broker exposes no HTTP state endpoint; per-call metrics reach the dashboard only via the persisted ``metrics_store`` (the embedding-service flusher), scoped to the embedding-service broker — the high-traffic local-inference path. Cross-process aggregation (merging the MCP-gateway broker's view) is not supported.

## Key files

- ``work_buddy/inference/broker.py`` — the broker itself, ``SlotMetrics``, ``ProfileConfig``, error classes.
- ``work_buddy/inference/__init__.py`` — public API re-exports (``get_broker``, ``Priority``, etc.).
- ``work_buddy/inference/metrics_store.py`` — SQLite persistence for completed calls (the ``broker-metrics`` entry-TTL artifact); an embedding-service flusher daemon drains the ring into it so dashboard history survives restarts.
- ``work_buddy/embedding/providers/lmstudio.py`` — first consumer; wraps bulk encode in ``broker.slot``.
- ``work_buddy/llm/backends/lmstudio_native.py`` — LLM native-chat consumer.
- ``work_buddy/llm/backends/openai_compat.py`` — OpenAI-compat consumer.
- ``tests/unit/test_local_inference_broker.py`` — 12 tests covering admission, priority, queue capacity, timeouts, metrics, reconfigure.
- ``tests/unit/test_llm_backends_broker_wiring.py`` — 5 tests pinning the profile-prefix contract across backends.
