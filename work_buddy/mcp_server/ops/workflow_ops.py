"""Workflow run lifecycle ops — cancel a run, sweep idle runs.

Each op here is referenced by a skill declaration (a ``kind:
"skill"`` knowledge-store unit carrying a matching ``op`` field).
Cancellation is exposed through :class:`work_buddy.workflows.service.WorkflowService`;
the engine-local idle sweep remains in :mod:`work_buddy.mcp_server.conductor`,
which owns the in-memory active-runs map. Registering them as ops exposes that
lifecycle control over the MCP gateway (``wb_run``) and to the user-facing
``/wb-workflow-cancel`` slash command.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.mcp_server.conductor import sweep_idle_runs
    from work_buddy.workflows.service import get_workflow_service

    register_op("op.wb.workflow_cancel", get_workflow_service().cancel)
    register_op("op.wb.workflow_sweep_idle", sweep_idle_runs)


_register()
