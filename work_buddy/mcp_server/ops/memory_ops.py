"""Memory-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.memory.ingest import retain_personal_note
    from work_buddy.memory.query import (
        memory_read,
        prune_memories,
        reflect_on_query,
    )

    register_op("op.wb.memory_read", memory_read)
    register_op("op.wb.memory_write", retain_personal_note)
    register_op("op.wb.memory_reflect", reflect_on_query)
    register_op("op.wb.memory_prune", prune_memories)


_register()
