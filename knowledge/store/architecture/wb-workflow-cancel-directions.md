---
name: Workflow Cancel Directions
kind: directions
description: How to cancel a workflow run — finding the run id, the reason argument, and how cancel relates to the automatic idle sweep.
trigger: User or agent wants to cancel, abort, or clean up a workflow run that is stuck, abandoned mid-run, or no longer wanted.
command: wb-workflow-cancel
capabilities:
- context/workflow_cancel
tags:
- workflow
- cancel
- lifecycle
- directions
- slash-command
aliases:
- wb-workflow-cancel
- cancel workflow
- abort workflow run
- stop a workflow
parents:
- architecture/workflows
---

Cancel a running workflow run — drop it from the conductor's in-memory active-runs map, mark its on-disk DAG cancelled (kept for audit), and revoke its workflow consent blanket.

## When to use

- An agent abandoned a workflow mid-run (stopped calling `wb_advance`) and the stale run is cluttering `wb_status` / the active-runs list.
- A workflow is stuck or broke mid-run (e.g. the Obsidian bridge failed) and you want to clear it so a fresh run can start.
- The user changed their mind about a workflow that is still in progress.

## How

1. Find the run id. In-flight runs are surfaced by `wb_status` and by the conductor's active-runs list as `workflow_run_id` values like `wf_7040ee31`.
2. Cancel it:

   ```
   mcp__work-buddy__wb_run("workflow_cancel", {"workflow_run_id": "wf_...", "reason": "..."})
   ```

`reason` is optional free text recorded for the audit trail; it defaults to `user_requested`.

## Notes

- **Idempotent.** Cancelling an already-cancelled run is a no-op; a run that has already completed is left untouched (nothing to cancel).
- The run is **not deleted** — its DAG file on disk is marked cancelled, so the run's history stays auditable.
- Orphaned runs are also reclaimed without you: an in-process sweep auto-cancels any run idle past the configured threshold (default 24h, reason `idle_timeout`). Manual cancel is for when you don't want to wait, or want a specific reason on the record.
