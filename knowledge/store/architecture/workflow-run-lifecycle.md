---
name: Workflow Run Lifecycle
kind: concept
description: 'WorkflowService lifecycle operations, persisted definition/run identity, cancellation, idle expiry, and gateway restart recovery.'
summary: 'WorkflowService exposes run lifecycle operations while the conductor owns the persisted DAG and in-memory active-run map. Each run records stable workflow_id, recorded workflow_revision, canonical workflow_name, and unique workflow_run_id; cancel, idle sweep, and startup recovery keep in-flight state bounded and recoverable.'
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
dev_notes: Direct conductor lifecycle calls are engine-internal or maintenance seams. Driving adapters use WorkflowService for invoke, advance, cancel, status, and Step-result retrieval; sweep_idle_runs and recover_active_runs remain conductor-owned because they maintain the in-process active-run map.
---

Driving adapters use `WorkflowService` for Workflow lifecycle operations. After admission, `WorkflowService.invoke` delegates run creation to the conductor, which stores the in-flight `WorkflowDAG` in `_ACTIVE_RUNS` in `work_buddy/mcp_server/conductor.py`, keyed by `workflow_run_id`. `WorkflowService.advance`, `cancel`, `status`, and `get_step_result` delegate to the corresponding conductor engine operations. A run leaves the active map on successful completion, blocked-by-failure, or explicit cancellation.

## Definition, revision, and run identity

A persisted run keeps four fields separate:

- `workflow_id` — stable logical definition identity;
- `workflow_revision` — deterministic hash marker for the authored definition, directly bound Directions snapshot, and compiled child-Workflow target identities used to start this run (not an archived copy of that content);
- `workflow_run_id` — identity of this individual execution;
- `workflow_name` — canonical agent-invocable address captured for display and compatibility.

Lifecycle responses, active-run listings, cancellation responses, and Step-result retrieval include these identity fields when present. Current persistence writes them explicitly. Older files that contain only the composite `name: "<workflow>:<run_id>"` shape still load by deriving the canonical name and run ID from that value.

When the conductor needs current definition metadata for a persisted DAG, it resolves the saved stable ID first and falls back to the saved canonical name if that ID no longer resolves. This preserves readability of prior runs without conflating their definition, revision, and execution identities.

## Terminal states

A run is terminal when no step is available to advance. Two flavors:

- **Complete** (`type: "workflow_complete"`) — every node in completed / skipped / failed AND `dag.is_complete()` is true. Triggers `_build_complete_response`.
- **Blocked** (`type: "workflow_blocked"`) — at least one step failed and its descendants are unreachable. Triggers `_build_blocked_response`, which surfaces honest progress counts (`<done>/<total> steps completed (blocked: <n> failed)`), a `failed_steps` list, and an `error` field naming the first failure.

Both states share lifecycle cleanup: persist the DAG, revoke the `workflow_run` consent grant, pop from `_ACTIVE_RUNS`. The distinction matters to consumers — agents, dispatchers, the sidecar executor — who should treat blocked workflows as failures (retry, escalate) rather than successes. `executor.py`'s DAG-walking dispatch loop exits on both states.

## fail_task cascades

When `fail_task` marks a step FAILED, it re-runs `_update_availability` so pending downstream nodes flip to BLOCKED. This keeps the DAG's per-node status, the rendered Mermaid diagram, and the `summary()` markdown consistent: no node sits in PENDING once an upstream has failed.

## Cancel

`WorkflowService.cancel(run_id, reason)` is the public lifecycle boundary. Its conductor implementation drops an active run from `_ACTIVE_RUNS`, marks the on-disk DAG cancelled (the file is kept for audit), and revokes the Workflow consent grant. The `workflow_cancel` Skill and `/wb-workflow-cancel` surface route through this service method. Cancellation is idempotent: cancelling an already-cancelled run is a no-op, and a completed run is left untouched. A run absent from `_ACTIVE_RUNS` is still cancellable through the on-disk DAG.

## Idle sweep

`sweep_idle_runs()` — skill `workflow_sweep_idle` — cancels runs with no step progress past the idle threshold (`workflows.run_lifecycle.idle_timeout_hours`, default 24h), with reason `idle_timeout`. An orphaned run — one whose agent stopped calling `wb_advance` — never leaves `_ACTIVE_RUNS` on its own; the sweep reclaims it.

The sweep runs on an interval (`sweep_interval_minutes`, default 60) in a daemon thread inside the MCP gateway process. It must run there, not as a sidecar cron job: `_ACTIVE_RUNS` is in-process state and the sidecar is a separate process that cannot mutate it.

## Restart recovery

`recover_active_runs()` runs once at gateway startup (`main_http`) and reloads incomplete runs from disk back into `_ACTIVE_RUNS`. Without it, a restart silently abandons every in-flight workflow — an agent's next `wb_advance` would get "unknown run". Runs idle past the threshold are expired (marked cancelled) rather than recovered. Gated by `workflows.run_lifecycle.recovery_enabled`.

Recovery interacts with `reconcile_workflow_consent`: once `_ACTIVE_RUNS` is repopulated, a re-registering session finds its recovered run and correctly keeps the consent blanket instead of revoking it as orphaned.

## How idleness is measured

From the freshest `started_at` / `completed_at` across the DAG's nodes — genuine step progress — not the file's `saved_at` (which also advances on non-progress writes). A `WorkflowDAG` persists its `agent_session_id` and a cancellation record (`cancelled` / `cancelled_reason` / `cancelled_at`) so a recovered or cancelled run round-trips intact.

## Thread safety

`_ACTIVE_RUNS` is mutated by gateway request workers and by the sweep thread. Mutations are guarded by `_ACTIVE_RUNS_LOCK`; the sweep and `list_active_runs` iterate a snapshot taken under the lock. The lock is held only for the dict op — never across disk I/O or subprocess calls.
