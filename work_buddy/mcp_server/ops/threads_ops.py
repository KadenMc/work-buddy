"""Thread-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.threads.universal_actions import (
        thread_defer,
        thread_dismiss,
        thread_rename,
    )

    register_op("op.wb.thread_dismiss", thread_dismiss)
    register_op("op.wb.thread_defer", thread_defer)
    register_op("op.wb.thread_rename", thread_rename)


_register()
