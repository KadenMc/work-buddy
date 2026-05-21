---
name: Workflow Cancel
kind: capability
description: Cancel a running workflow run — drop it from the in-memory active-runs map, mark its on-disk DAG cancelled (kept for audit), and revoke its consent blanket. Idempotent; a completed run is left untouched.
capability_name: workflow_cancel
category: context
parameters:
  workflow_run_id:
    type: str
    description: Run id of the workflow to cancel (e.g. 'wf_7040ee31'). Find it via wb_status or the conductor's active-runs list.
    required: true
  reason:
    type: str
    description: Free-text reason recorded on the cancelled DAG for the audit trail. Defaults to 'user_requested'.
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.workflow_cancel
schema_version: wb-capability/v1
tags:
- context
- workflow
- cancel
- lifecycle
aliases:
- cancel workflow
- abort workflow
- stop workflow run
- kill workflow
- abandon workflow run
parents:
- context
---
