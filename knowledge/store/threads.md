---
name: Threads — universal-entity primitive
kind: system
description: The Thread is the FSM-resolution subtype of the WorkItem base. Replaces the older split between PoolEntry (now folded into states) and ActionItem (now folded into sub-Threads). Task is a sibling subtype on the shared WorkItem base — Task(WorkItem), NOT a Thread subclass.
summary: 'v5 collapses v4''s overlapping entities into one primitive: Thread. A Thread has FSM state, an event log, an autonomy policy, optional parent_id (for sub-threads), optional subtype (''task'' for the master-list contract). Types are frozen and schemas migrated. The FSM engine, inference layer, and surfaces are wired and running. v4 data migration is superseded: the legacy pool and Review surface are removed, so new triage producers write directly to v5 Threads.'
dev_notes: |
  Task's read snapshot (``Task._row``) is a read CACHE, never a source of truth —
  the markdown master list (then the store) stays authoritative. Mutation methods
  invalidate the snapshot (``_row = None``) and delegate FIELD-TARGETED writes
  through ``work_item/task_adapter.py`` -> ``mutations.py`` (never a whole-object
  save), preserving the markdown-wins / dual-surface reconcile invariant.
  ``Task.load`` / ``from_store_row`` carry the row (one query); ``live_row()`` and
  ``refresh()`` are the explicit always-fresh re-reads. The ``work_item_events``
  audit log requires the ``db/work_item_events`` path resource registered in
  ``paths.RESOURCES`` — ``emit()`` is best-effort and silently no-ops if missing.
tags:
- threads
- fsm
- resolution
- core
---

## WorkItem inversion — read this first

The original framing below ("Thread is THE universal entity; Task is a Thread
subclass") is **superseded**. A thin **`WorkItem`** base
(``work_buddy/threads/workitem.py``) is now the universal primitive; **`Thread`**
and **`Task`** are its two *sibling* subtypes (neither subclasses the other):

- **`Thread(WorkItem)`** — the FSM-resolution subtype (the 14-state engine,
  inference, autonomy). Everything below about "the Thread" describes *this* subtype.
- **`Task(WorkItem)`** — the master-list-contract subtype. **No FSM.** Persists in
  the ``obsidian/tasks`` ``task_metadata`` store + the markdown master list, **not**
  the ``threads`` table. It is **the live path** the task system runs through:
  mutations (create / toggle / update / delete) route through ``Task`` via the
  work-item write port (``work_buddy/work_item/task_adapter.py``, delegating to
  ``obsidian/tasks/mutations.py``); per-task reads go through ``Task.load(id)`` — a
  **content-carrying** Task whose ``.row`` + field accessors (``state``, ``urgency``,
  ``description``, ``deadline_date``, ``completed_at``, ``note_uuid``, …) read a
  loaded snapshot — and collection reads through ``Task.query(...) -> list[Task]``.
  **Not** the old ``NotImplementedError`` stub and **not** a Thread subclass.

Rationale + plan live in the workflow-induction design dossier (the roadmap's
locked-inversion decision, plus the implementation + cutover design). The sections
below predate the inversion — read "Thread = universal entity" as "Thread = the
FSM-resolution subtype of WorkItem."

## Where this lives in the code

- ``work_buddy/threads/enums.py`` — FSMState, InferenceTarget, ReasoningTier, InvocationContext, ActionKind, Authorship, SurfaceUrgency.
- ``work_buddy/threads/workitem.py`` — the thin **WorkItem** base (id, lineage, attached context, risk profile, lifecycle timestamps; **NO FSM**) that both subtypes share.
- ``work_buddy/threads/models.py`` — Thread(WorkItem), Task(WorkItem), ContextItem, AutonomyPolicy, ResolutionRequest, Proposal.
- ``work_buddy/threads/work_item_events.py`` — the WorkItem base provenance log (durable audit of lifecycle events across subtypes).
- ``work_buddy/work_item/task_adapter.py`` — the Task write port (one-way Task → ``obsidian/tasks/mutations.py``).
- ``work_buddy/threads/events.py`` — ThreadEvent, ALL_KINDS catalog, OptimisticLockConflict.
- ``work_buddy/threads/fsm.py`` — TRANSITION_TABLE (DESIGN.md §7.6), lookup helpers, state-entry side-effect catalog.
- ``work_buddy/threads/store.py`` — SQLite schema for ``threads`` + ``thread_events``; minimum CRUD; optimistic-lock event submission.
- ``work_buddy/llm/queue.py`` — LLM-call priority queue (NOT in threads/ — it's general infra).
- ``work_buddy/llm/budget.py`` — per-Thread budget admission hook.

## Module-boundary rules (load-bearing)

- The LLM-call **priority queue** lives in ``work_buddy/llm/`` and is OWNED by that subsystem, not by Threads. Threads enqueue into it; they do not own queue state. **Do NOT add a queue table or worker to the threads/ package.** See DESIGN.md §9.2.
- The capability/workflow registry lives in ``work_buddy/mcp_server``. Threads dispatch into it via the Action Catalog (a typed lens, not a new registry).
- The conductor lives in ``work_buddy/sidecar``. v5 dispatches workflow-backed actions into it; the conductor itself is unchanged by v5.

## Current architecture

- **Types and storage**: Thread/Task/WorkItem types and schemas are frozen and migrated. The v4-aggregator and pool-aware card primitives are retired along with the legacy clarify pool; v5 is the single source of truth.
- **Engine**: the FSM engine, transition table, inference layer, sidecar workers, bootstrap, and LLM-queue admission hook are wired and running (see threads/fsm and threads/llm-call-queue).
- **Data**: the v4 to v5 dry-run migrator was scaffolded but never run against production data. With the legacy pool and Review surface deleted, there is nothing left to cut over; the migration scripts and aggregator have been removed from the tree. Triage producers (journal, email, inline, chrome) write directly to v5 Threads via the source-pipeline runner.
- **Surfaces (UX)**: the Threads tab is the canonical surface, with recursive URL routing, all 5 card kinds (confirmation / clarification / consent / review / redirect / cleanup-failure), a per-action UI registry with 5 specialized renderers, write-time linearization, search and filters, the Later mechanic, action-context status indicators, and journal + chrome spawn helpers. v4 tabs hide by default behind a 'v4' toggle.
