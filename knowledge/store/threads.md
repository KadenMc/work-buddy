---
name: Threads — universal-entity primitive
kind: system
description: The Thread is the universal entity for 'context that may need an action'. Replaces the older split between PoolEntry (now folded into states) and ActionItem (now folded into sub-Threads). Task survives as a subclass.
summary: 'v5 collapses v4''s overlapping entities into one primitive: Thread. A Thread has FSM state, an event log, an autonomy policy, optional parent_id (for sub-threads), optional subtype (''task'' for the master-list contract). Stage 1: types frozen, schemas migrated, scaffolding in place. Stage 2 wires the engine. Stage 3 migrates v4 data. Stage 4 redesigns surfaces.'
tags:
- threads
- fsm
- resolution
- core
---

## Where this lives in the code

- ``work_buddy/threads/enums.py`` — FSMState, InferenceTarget, ReasoningTier, InvocationContext, ActionKind, Authorship, SurfaceUrgency.
- ``work_buddy/threads/models.py`` — Thread, Task(Thread), ContextItem, AutonomyPolicy, ResolutionRequest, Proposal.
- ``work_buddy/threads/events.py`` — ThreadEvent, ALL_KINDS catalog, OptimisticLockConflict.
- ``work_buddy/threads/fsm.py`` — TRANSITION_TABLE (DESIGN.md §7.6), lookup helpers, state-entry side-effect catalog.
- ``work_buddy/threads/store.py`` — SQLite schema for ``threads`` + ``thread_events``; minimum CRUD; optimistic-lock event submission.
- ``work_buddy/llm/queue.py`` — LLM-call priority queue (NOT in threads/ — it's general infra).
- ``work_buddy/llm/budget.py`` — per-Thread budget admission hook.

## Module-boundary rules (load-bearing)

- The LLM-call **priority queue** lives in ``work_buddy/llm/`` and is OWNED by that subsystem, not by Threads. Threads enqueue into it; they do not own queue state. **Do NOT add a queue table or worker to the threads/ package.** See DESIGN.md §9.2.
- The capability/workflow registry lives in ``work_buddy/mcp_server``. Threads dispatch into it via the Action Catalog (a typed lens, not a new registry).
- The conductor lives in ``work_buddy/sidecar``. v5 dispatches workflow-backed actions into it; the conductor itself is unchanged by v5.

## Stage status (v5 implementation)

- **Stage 1 — Foundation**: complete. Types frozen, schemas migrated, queue scaffolded. The v4-aggregator and pool-aware card primitives that were originally part of Stage 1 were retired alongside the legacy clarify pool — v5 is now the single source of truth.
- **Stage 2 — Engine**: complete. FSM engine + transition table + inference layer + sidecar workers + bootstrap + LLM-queue admission hook.
- **Stage 3 — Cutover**: superseded. The v4 → v5 dry-run migrator was scaffolded but never run against production data; with the legacy pool / Review surface deleted there is nothing left to cut over. The migration scripts and aggregator have been removed from the tree. New triage producers (journal, email, inline, chrome) write directly to v5 Threads via the source-pipeline runner.
- **Stage 4 — Surfaces (UX)**: complete. Threads tab is the canonical v5 surface with recursive URL routing, all 5 card kinds (confirmation / clarification / consent / review / redirect / cleanup-failure), per-action UI registry with 5 specialized renderers, write-time linearization, search + filters, Later mechanic, action-context status indicators, journal + chrome spawn helpers. v4 tabs hide by default with a 'v4' toggle.
