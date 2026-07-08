---
name: LLM-call priority queue (lives in work_buddy/llm/, not threads/)
kind: concept
description: General infrastructure for dispatching LLM work by priority. Threads enqueue; the queue dispatches. Reusable by any client (scheduled jobs, agents, batch ops). MUST NOT be reimplemented inside the Thread package.
summary: When a Thread enters awaiting_inference, the FSM enqueues a request (caller_id, target, priority, payload, tier_hint) into the LLM-call queue and waits. The queue owns scheduling, concurrency limits, and dispatching to runtimes. Per-caller budgets enforced at enqueue via pluggable admission hooks. The Thread system is a publisher only.
tags:
- threads
- llm
- queue
- infrastructure
parents:
- threads
- threads
---

## Why the queue lives outside threads/

From DESIGN.md §9.2: the queue is general infrastructure. Anything that wants to dispatch LLM work — other agents, scheduled jobs, batch operations, sidecar workers in unrelated subsystems — should go through it. Building it inside the Thread system would tie general infrastructure to one client and force every other client to either reinvent or reach across module boundaries.

## Where it lives

- ``work_buddy/llm/queue.py`` — schema (``llm_call_queue`` table), CRUD, status transitions, atomic dequeue, admission hooks.
- ``work_buddy/llm/budget.py`` — per-caller budget store + cost source registry + admission hook.
- ``work_buddy/llm/tiers.py`` — ``ModelTier`` (incl. v5 ``AGENT_HEADLESS`` and ``USER``).

## Status transitions

pending → in_flight → done | failed | cancelled
pending → rejected (admission denied; audit row recorded)

## Admission hooks

``register_admission_hook(fn)`` — fn returns ``AdmissionDecision``; first ``admit=False`` rejects. The budget hook (``work_buddy.llm.budget.budget_admission_hook``) is the canonical example; it is registered during sidecar bootstrap (``work_buddy.threads.bootstrap.bootstrap_threads``) with per-Thread caps drawn from autonomy_policy.budget_usd.

## Dispatcher

The sidecar runs a background poller (``work_buddy.threads.inference_worker.run_poller``, started as a daemon thread in ``work_buddy/sidecar/daemon.py``) that drains the queue via ``process_one_pending`` / ``queue.dequeue`` and dispatches to the LLM runner for the five structured-call tiers (LOCAL_TOOL_CALLING through FRONTIER_BEST). The two Thread-specific tiers, ``AGENT_HEADLESS`` (multi-turn agent subprocess) and ``USER`` (FSM short-circuit to a clarification state), are registered as stub bindings in ``work_buddy/llm/tiers.py`` (``backend: agent_subprocess`` / ``backend: user_clarification``) but have no subprocess spawner or FSM short-circuit wired yet: a request at either tier falls through the tier map to the default structured-call path instead of the intended dispatch.
