"""Activity-ledger ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.mcp_server.activity_ledger import (
        query_activity,
        query_session_summary,
    )

    register_op("op.wb.session_activity", query_activity)
    register_op("op.wb.session_summary", query_session_summary)


_register()
