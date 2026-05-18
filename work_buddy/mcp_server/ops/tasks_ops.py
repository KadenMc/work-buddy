"""Task-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The declaration supplies
the prose, parameter schema, and runtime metadata; the op supplies the callable.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    # Imported lazily inside the registration function, matching the
    # lazy-import discipline of the registry's ``_*_capabilities`` builders
    # (see architecture/mcp-import-discipline).
    from work_buddy.obsidian.tasks.mutations import read_task

    register_op("op.wb.task_read", read_task)


_register()
