"""work_buddy.threads — universal-entity primitive.

A Thread is the universal entity for "context that may need an
action." Subsumes the v4 PoolEntry (now folded into Thread states)
and ActionItem (folded into sub-Threads with parent_id). Task
survives as a subclass.

Modules in this package
-----------------------

- ``enums``    — FSMState, InferenceTarget, ReasoningTier,
                  InvocationContext, ActionKind, Authorship,
                  SurfaceUrgency.
- ``models``   — Thread, Task(Thread), ContextItem, ResolutionRequest,
                  AutonomyPolicy, Proposal.
- ``events``   — ThreadEvent, event-kind catalog, OptimisticLockConflict.
- ``fsm``      — TRANSITION_TABLE plus lookup helpers (data only;
                  engine wiring lives in ``engine``).

Module boundaries (load-bearing)
--------------------------------

- The LLM-call **priority queue** lives in ``work_buddy/llm/`` and is
  owned by that subsystem, not by Threads. Threads enqueue into it;
  they do not own queue state. **Do NOT add a queue table or worker
  to this package.**
- The **capability/workflow registry** lives in
  ``work_buddy/mcp_server``. Threads dispatch into the registries via
  the Action Catalog (a typed lens, not a new registry).
- The **conductor** lives in ``work_buddy/sidecar``. Threads dispatch
  workflow-backed actions into it; the conductor itself is unchanged.
"""
