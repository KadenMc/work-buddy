---
name: Resolution phase (vs. execution)
kind: concept
description: Resolution = the cyclic, human-in-loop FSM where the system decides what to do. Execution = whichever runtime owns the dispatched action; the threads FSM dispatches but does not itself host the action runtime.
summary: 'Pre-dispatch: per-Thread FSM is cyclic; human-in-the-loop is a state. Inference + clarification + confirmation happen here. Post-dispatch: existing capability call infrastructure, workflow conductor, or agent loop — none introduced by v5. Two events (execution_started, execution_finished) mirror back; per-step detail lives on the run record.'
tags:
- threads
- resolution
- execution
parents:
- threads
- threads
---

## Why a state machine for resolution

- **Cycles**: user redirect requires backward transition.
- **Human-in-the-loop is state, not interrupt**: the Thread sits until input arrives.
- **Re-entry from execution**: failure re-enters the FSM at awaiting_redirect.

## Resolution Request

When a Thread enters any awaiting_* state, it produces a typed ``ResolutionRequest`` (see ``work_buddy.threads.models.ResolutionRequest``). This flows through the existing consent subsystem (``work_buddy/consent.py``), which becomes the FSM's I/O layer for human input. The Resolution Surface card primitive (``script_resolution_surface_v5.py``) renders these.

## Card kinds (DESIGN.md §15.1)

- ``confirmation`` — agent has guess; 3 affordances (Accept, Edit, Redirect).
- ``clarification`` — agent has nothing; 2 affordances (Provide, Skip).
- ``consent`` — action fully determined, gating execution; 3 affordances (Approve, Edit parameters, Reject).
- ``review`` — post-execution review; 3 affordances (Mark done, Redirect, Drop).
- ``redirect`` — post-failure; 2 affordances (Provide, Skip).

## Edit vs Redirect

- **Edit**: in-place modification; advances forward.
- **Redirect**: changes the agent's premise; loops back to ``awaiting_inference`` with the redirect payload as forced input.
