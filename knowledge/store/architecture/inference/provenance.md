---
name: Inference Provenance
kind: system
description: First-class provenance for every inference call across work-buddy — a stable call_id + plain-text description per local/cloud completion and embedding, written beside the cost ledger and surfaced as the Settings › Inference activity feed.
summary: 'Every model call (local LM Studio + cloud Anthropic/Google, completions + embeddings) records a provenance row — stable ``call_id``, plain-text ``description`` (``<call site>: <detail>``), kind/model/provider/execution_mode/status, usage, end-to-end latency, and task/trace cross-refs. Captured at three sites (cost.log_call, run_task error path, the bulk embedding encoders), appended to per-session ``inference_calls.jsonl``, and surfaced newest-first via ``GET /api/inference-activity`` with a call_id join onto broker scheduler latency. Sits beside the cost ledger, which stays authoritative for $.'
entry_points:
- work_buddy.llm.provenance
- work_buddy.inference.call_context
tags:
- provenance
- inference
- call-id
- observability
- cost-ledger
- embeddings
- completions
- activity-feed
- dashboard
aliases:
- inference provenance
- call provenance
- inference activity
- inference_calls
- record_inference_call
- inference activity feed
parents:
- architecture/inference
dev_notes: |-
  **Cross-process without threading.** ``bind_call_id`` (entered by the ``run_task`` decorator + the embedding capture) sets a ContextVar ``call_id`` *and* a start timestamp; downstream same-thread code reads them: ``broker.slot`` uses ``current_call_id()`` as its ``SlotMetrics.id`` (so a local call's scheduler-latency row joins its provenance row), and ``record_inference_call`` reads ``current_call_id()`` / ``current_detail()`` / ``current_call_elapsed_ms()`` when not passed explicitly. ``detail`` is set by ``LLMRunner.call(detail=...)`` / ``inference_detail(...)`` around the call.

  **Latency for all providers.** ``current_call_elapsed_ms()`` (now − bind_call_id start) gives an end-to-end latency even for cloud calls, which carry no broker timing. Local rows additionally get queue-wait / service splits via the ``call_id`` join against ``metrics_store``.

  **Why emit from cost.log_call.** It's the one chokepoint every completion hits, so one emit there covers cache/Anthropic/local/with_tools. Errors don't reach log_call, so the ``_with_call_id`` decorator emits those separately. ContextVars don't cross the ``llm_submit`` async-replay boundary (acceptable).

  **Coverage gap.** Embedding ``detail`` is the IR source for ``_build_vectors_for_projection`` builds and ``vault index`` for vault builds; other ``_encode_bulk_direct`` callers fall back to a document count. Cross-session feed read mirrors ``dashboard/costs.py``'s session scan.
---

## What it is

Every model call work-buddy makes records a provenance row: a stable ``call_id``, a plain-text ``description`` (``<call site>: <detail>``), ``kind`` (completion | embedding), ``model``, ``provider``, ``execution_mode`` (local | cloud), ``status``, token/item usage, end-to-end ``latency_ms``, and cross-refs (``task_id``, ``trace_id``). It spans both providers (local LM Studio + cloud Anthropic/Google) and both completions and embeddings. It sits **beside** the cost ledger (``llm_costs.jsonl``) — cost stays authoritative for $; provenance answers *what is calling models, and why*.

## Capture

Three sites call ``work_buddy.llm.provenance.record_inference_call`` (best-effort; a failure never affects the real call):

- **Completions** — emitted from ``cost.log_call``, which every completion path funnels through (cache hit, Anthropic, local backends, ``llm_with_tools``). Universal coverage without threading through each backend.
- **Failures / escalations** — ``run_task``'s ``_with_call_id`` decorator emits an ``error`` row for any failed attempt (each rung of a tier-escalation chain), stamped with the shared ``trace_id`` so the feed can group them.
- **Embeddings** — the IR + vault bulk encoders (``ir/dense.py`` and ``vault_index/dense.py`` via ``_encode_bulk_direct``) emit one row per batch.

The **call site** is derived automatically from the caller chain (zero per-site effort); a **detail** one-liner is appended only where readily available (a tab title, the IR source being embedded), set by the caller via ``inference_detail(...)``.

## Storage + surface

Records append to per-session ``agents/<session>/inference_calls.jsonl`` (the cost-log routing pattern; cross-session scan for the feed). The dashboard's **Inference activity** feed reads them via ``GET /api/inference-activity`` (background-cached), newest-first, and joins broker scheduler latency (queue-wait / service) onto local rows by ``call_id``. It live-updates via the ``inference.call_logged`` SSE event.

## Key files

- ``work_buddy/llm/provenance.py`` — ``record_inference_call`` (per-session JSONL writer) + ``inference.call_logged`` publish.
- ``work_buddy/inference/call_context.py`` — ambient ``call_id`` + ``detail`` + start-time ContextVars (``bind_call_id`` / ``inference_detail`` / ``current_call_elapsed_ms``).
- ``work_buddy/llm/cost.py`` (``log_call`` emit), ``work_buddy/llm/runner.py`` (``_with_call_id`` decorator), ``work_buddy/ir/dense.py`` + ``work_buddy/vault_index/dense.py`` (embedding capture).
- ``work_buddy/dashboard/api.py`` (``get_inference_activity`` read-model + broker join), ``work_buddy/dashboard/frontend/scripts/tabs/inference.py`` (the feed).
