"""Workflow run lifecycle ops — cancel a run, sweep idle runs.

Each op here is referenced by a capability declaration (a ``kind:
"capability"`` knowledge-store unit carrying a matching ``op`` field).
The callables live in :mod:`work_buddy.mcp_server.conductor`, which owns
the in-memory active-runs map these operate on — registering them as ops
exposes that lifecycle control over the MCP gateway (``wb_run``) and to
the user-facing ``/wb-workflow-cancel`` slash command.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.mcp_server.conductor import cancel_workflow, sweep_idle_runs

    register_op("op.wb.workflow_cancel", cancel_workflow)
    register_op("op.wb.workflow_sweep_idle", sweep_idle_runs)


_register()
