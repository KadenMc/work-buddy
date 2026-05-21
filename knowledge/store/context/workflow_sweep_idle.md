---
name: Workflow Sweep Idle
kind: capability
description: Cancel active workflow runs that have had no step progress past the idle threshold. Runs automatically on an interval in the MCP gateway; also callable manually (with dry_run) for observability.
capability_name: workflow_sweep_idle
category: context
parameters:
  idle_threshold_hours:
    type: float
    description: Override the idle threshold in hours. Omit to use the configured default (workflows.run_lifecycle.idle_timeout_hours).
    required: false
  dry_run:
    type: bool
    description: List the runs that would be cancelled without cancelling them.
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.workflow_sweep_idle
schema_version: wb-capability/v1
tags:
- context
- workflow
- sweep
- idle
- lifecycle
aliases:
- sweep idle workflows
- reclaim orphaned workflows
- cancel stale workflow runs
- idle workflow timeout
parents:
- context
---
