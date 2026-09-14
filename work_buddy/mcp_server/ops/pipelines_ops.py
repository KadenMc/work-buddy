"""Pipeline-domain ops.

Each op here is referenced by a skill declaration (a ``kind: "skill"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.pipelines.source_registry import run_source_pipeline

    register_op("op.wb.run_source_pipeline", run_source_pipeline)


_register()
