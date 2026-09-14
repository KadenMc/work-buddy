"""work_buddy.threads — FSM-resolution subtype of the WorkItem primitive.

``WorkItem`` is the common primitive. ``Thread`` and ``Task`` are sibling
subtypes: Thread owns the resolution FSM, while Task owns the task-list
contract without an FSM.

Modules in this package
-----------------------

- ``enums``    — Thread FSM enums plus a compatibility re-export of the
                  canonical ``work_buddy.workflows.context.InvocationContext``.
- ``models``   — Thread(WorkItem), Task(WorkItem), ContextItem, ResolutionRequest,
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
- The **Skill/Workflow registry** and execution conductor live in
  ``work_buddy/mcp_server``. Transport-neutral Workflow admission and
  lifecycle routing live in ``work_buddy/workflows``. Threads expose
  registered behavior through the Action Catalog; an adapter that starts a
  Workflow must cross ``WorkflowService`` rather than owning a second registry
  or execution engine.
"""
