---
name: Thread FSM (resolution phase)
kind: concept
description: 14-state FSM that runs from inciting event to terminal. Transitions wired in Stage 2; data structures (state catalog, transition table) land in Stage 1.
summary: 'States: proposed, awaiting_inference, inferring_*, awaiting_*_confirmation, awaiting_*_clarification, awaiting_confirmation (action gate), awaiting_review (opt-in), awaiting_redirect, executing, monitoring, done, dismissed, handed_off. Cycles only via human-initiated redirect (any wait state → awaiting_inference). No automated retry loops.'
tags:
- threads
- fsm
- resolution
parents:
- threads
- threads
dev_notes: |-
  ## Action parameter binding (EXECUTING)

  `execution_runner._bind_runtime_parameters` fills parameters that depend on thread runtime state before dispatch:

  - `tab_ids` — for the `chrome_tab_*` actions, collected from the thread's context items.
  - `thread_id` — injected for any action whose **declaration** includes a `thread_id` parameter, gated on `is_action`. The op callable is a `**kwargs` wrapper whose signature can't be introspected, so the declared parameter schema is the authoritative source. A thread-scoped action (`journal_*`, `email_*`, `chrome_route_*`, the universal `thread_*`) therefore needs no execution_runner change — declaring `thread_id` is sufficient for the host thread to be bound at dispatch. The `is_action` gate excludes non-action capabilities (e.g. the messaging tools) that declare an unrelated `thread_id`.
---

## State catalog

5 categories:

1. **Queued for inference** — `awaiting_inference`. Thread enqueues into the LLM-call priority queue (work_buddy/llm/queue.py); does NOT own the queue.
2. **Inference running** — `inferring_intent` | `inferring_context` | `inferring_action`. Worker has dequeued; lifecycle managed by inference worker.
3. **Resolution wait** — `awaiting_*_confirmation` (3 affordances), `awaiting_*_clarification` (2 affordances), `awaiting_confirmation` (consent gate, 3 affordances), `awaiting_review` (opt-in post-execution, 3 affordances), `awaiting_redirect` (after failure, 2 affordances).
4. **Execution** — `executing`. The ``execution_runner`` state-entry handler reads the latest non-cleared ``action_inferred`` event, binds runtime parameters, invokes the named capability via the MCP registry, and fires ``EXECUTION_DONE`` (success) or ``EXECUTION_FAILED`` (which routes to AWAITING_REDIRECT). Two events land in the Thread log: ``execution_started``, ``execution_finished``. Per-step detail lives on the run record (workflow run, capability call log, agent session).
5. **Terminal** — `done` (succeeded), `dismissed` (user closed without acting / timeout), `handed_off` (work migrated to a human or external system; agent has no further role), `monitoring` (parent-of-decomposed; transitions to done when all children terminal).

## Transition table

See ``work_buddy.threads.fsm.TRANSITION_TABLE`` for the canonical mapping.

## State-entry side effects

State transitions are paired with side-effect handlers registered at
bootstrap (``work_buddy.threads.bootstrap.bootstrap_threads``):

- AWAITING_INFERENCE → enqueue inference work (``inference_worker``).
- Wait states → publish a ResolutionRequest card (``resolution_surface``).
- Terminal states → ``cascade_terminal_to_parent`` advances the parent.
- CLEANING_UP → run the registered cleanup adapter (``cleanup_runner``).
- EXECUTING → dispatch the chosen action capability (``execution_runner``).

Every active (non-wait, non-terminal) state must have a registered
handler. A state the FSM transitions INTO with no handler attached
strands the thread — it enters but never leaves.

## Cycles

The graph contains cycles ONLY via human-initiated redirect (any wait state → awaiting_inference). The forward inference path is acyclic. There is NO automated retry loop and NO cycle-prevention machinery (revisit caps, fingerprint detection). Per-Thread cost is bounded by the autonomy policy's budget axis, enforced at the LLM-call queue layer.

## awaiting_review is opt-in

Most actions go straight from executing → done. A Thread enters `awaiting_review` ONLY when the executed action's template declared `requires_post_review: true` OR the autonomy policy demands review (e.g., irreversibility above threshold).
