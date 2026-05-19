"""Pipeline-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.pipelines.capability import run_source_pipeline

    register_op("op.wb.run_source_pipeline", run_source_pipeline)


_register()
