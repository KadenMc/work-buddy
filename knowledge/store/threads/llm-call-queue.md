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

``register_admission_hook(fn)`` — fn returns ``AdmissionDecision``; first ``admit=False`` rejects. The budget hook (``work_buddy.llm.budget.budget_admission_hook``) is the canonical example; Stage 2 registers it during sidecar bootstrap with per-Thread caps drawn from autonomy_policy.budget_usd.

## Stage 1 status

Scaffolding only. Schema lives, CRUD works, hooks compose, but no dispatcher loop runs. Stage 2 wires the dispatcher (sidecar worker that pulls pending entries and routes to the appropriate runtime: structured LLM call, AGENT_HEADLESS subprocess, or USER-tier short-circuit to a clarification state).
