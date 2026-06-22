---
name: Workflow Run Lifecycle
kind: concept
description: 'How in-flight workflow runs are bounded and recovered: cancel, the idle-timeout sweep, and restart recovery of the conductor''s in-memory active-runs map.'
summary: 'The conductor''s in-memory _ACTIVE_RUNS map is kept bounded and recoverable by three mechanisms: cancel_workflow (manual), an idle-timeout sweep thread, and recover_active_runs at gateway startup.'
tags:
- workflows
- conductor
- lifecycle
- cancel
- idle-sweep
- restart-recovery
- active-runs
aliases:
- workflow cancel
- idle sweep
- restart recovery
- active runs map
- orphaned workflow run
- workflow run TTL
parents:
- architecture/workflows
---

The conductor holds in-flight runs in an in-memory map — `_ACTIVE_RUNS` in `work_buddy/mcp_server/conductor.py`, keyed by `workflow_run_id`. A run is added at `start_workflow` and removed on any **terminal state**: successful completion, blocked-by-failure, or explicit cancel. Three mechanisms keep that map bounded and recoverable.

## Terminal states

A run is terminal when no step is available to advance. Two flavors:

- **Complete** (`type: "workflow_complete"`) — every node in completed / skipped / failed AND `dag.is_complete()` is true. Triggers `_build_complete_response`.
- **Blocked** (`type: "workflow_blocked"`) — at least one step failed and its descendants are unreachable. Triggers `_build_blocked_response`, which surfaces honest progress counts (`<done>/<total> steps completed (blocked: <n> failed)`), a `failed_steps` list, and an `error` field naming the first failure.

Both states share lifecycle cleanup: persist the DAG, revoke the `workflow_run` consent grant, pop from `_ACTIVE_RUNS`. The distinction matters to consumers — agents, dispatchers, the sidecar executor — who should treat blocked workflows as failures (retry, escalate) rather than successes. `executor.py`'s DAG-walking dispatch loop exits on both states.

## fail_task cascades

When `fail_task` marks a step FAILED, it re-runs `_update_availability` so pending downstream nodes flip to BLOCKED. This keeps the DAG's per-node status, the rendered Mermaid diagram, and the `summary()` markdown consistent: no node sits in PENDING once an upstream has failed.

## Cancel

`cancel_workflow(run_id, reason)` — capability `workflow_cancel`, slash command `/wb-workflow-cancel` — drops a run from `_ACTIVE_RUNS`, marks its on-disk DAG cancelled (the file is kept, not deleted, for audit), and revokes the workflow consent blanket. It is idempotent: cancelling an already-cancelled run is a no-op, and a run that has already completed is left untouched. A run not in `_ACTIVE_RUNS` is still cancellable — the lookup falls back to the on-disk DAG.

## Idle sweep

`sweep_idle_runs()` — capability `workflow_sweep_idle` — cancels runs with no step progress past the idle threshold (`workflows.run_lifecycle.idle_timeout_hours`, default 24h), with reason `idle_timeout`. An orphaned run — one whose agent stopped calling `wb_advance` — never leaves `_ACTIVE_RUNS` on its own; the sweep reclaims it.

The sweep runs on an interval (`sweep_interval_minutes`, default 60) in a daemon thread inside the MCP gateway process. It must run there, not as a sidecar cron job: `_ACTIVE_RUNS` is in-process state and the sidecar is a separate process that cannot mutate it.

## Restart recovery

`recover_active_runs()` runs once at gateway startup (`main_http`) and reloads incomplete runs from disk back into `_ACTIVE_RUNS`. Without it, a restart silently abandons every in-flight workflow — an agent's next `wb_advance` would get "unknown run". Runs idle past the threshold are expired (marked cancelled) rather than recovered. Gated by `workflows.run_lifecycle.recovery_enabled`.

Recovery interacts with `reconcile_workflow_consent`: once `_ACTIVE_RUNS` is repopulated, a re-registering session finds its recovered run and correctly keeps the consent blanket instead of revoking it as orphaned.

## How idleness is measured

From the freshest `started_at` / `completed_at` across the DAG's nodes — genuine step progress — not the file's `saved_at` (which also advances on non-progress writes). A `WorkflowDAG` persists its `agent_session_id` and a cancellation record (`cancelled` / `cancelled_reason` / `cancelled_at`) so a recovered or cancelled run round-trips intact.

## Thread safety

`_ACTIVE_RUNS` is mutated by gateway request workers and by the sweep thread. Mutations are guarded by `_ACTIVE_RUNS_LOCK`; the sweep and `list_active_runs` iterate a snapshot taken under the lock. The lock is held only for the dict op — never across disk I/O or subprocess calls.
