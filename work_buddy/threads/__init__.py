"""work_buddy.threads — v5 universal-entity primitive.

A Thread is the universal entity for "context that may need an
action." Replaces v4's PoolEntry (folded into Thread states) and
ActionItem (folded into sub-Threads with parent_id). Task survives as
a subclass.

See ``data/designs/gtd/reimagined/DESIGN.md`` (the design document)
and ``data/designs/gtd/reimagined/IMPLEMENTATION-PLAN.md`` (the
staging plan) for full context.

This package builds out across Stages 1–4 of v5 implementation:

- Stage 1 (foundation): types frozen, schemas migrated, scaffolding
  in place. Nothing user-visible. **(in progress)**
- Stage 2 (engine): FSM live, inference live, sidecar workers, queue
  real clients, autonomy composition, sub-threads, Resolution
  Surface universal.
- Stage 3 (cutover): existing v4 entities migrate to Threads; old
  code paths removed.
- Stage 4 (vertical slices + settle): Now feed, Tasks tab, first
  end-to-end use cases.

Modules in this package
-----------------------

- ``enums``    — FSMState, InferenceTarget, ReasoningTier,
                  InvocationContext, ActionKind, Authorship,
                  SurfaceUrgency.
- ``models``   — Thread, Task(Thread), ContextItem, ResolutionRequest,
                  AutonomyPolicy, Proposal.
- ``events``   — ThreadEvent, event-kind catalog, OptimisticLockConflict.
- ``fsm``      — TRANSITION_TABLE (DESIGN.md §7.6) plus lookup
                  helpers. Stage 1 ships data only; the engine
                  wiring lands in Stage 2.

Module boundaries (load-bearing)
--------------------------------

- The LLM-call **priority queue** lives in ``work_buddy/llm/`` and is
  owned by that subsystem, not by Threads. Threads enqueue into it;
  they do not own queue state. **Do NOT add a queue table or worker
  to this package.** See DESIGN.md §9.2.
- The **capability/workflow registry** lives in
  ``work_buddy/mcp_server``. Threads dispatch into the registries via
  the Action Catalog (a typed lens, not a new registry).
- The **conductor** lives in ``work_buddy/sidecar``. Threads dispatch
  workflow-backed actions into it; the conductor itself is unchanged
  by v5.
"""
