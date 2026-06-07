"""WebSearch-domain ops.

Each op here is referenced by a ``kind: capability`` declaration unit under
``knowledge/store/websearch/`` carrying a matching ``op`` field. Mirrors
``email_ops.py``: a module-level ``_register()`` binds the thin capability
callables to ``op.wb.*`` ids. A newly added op requires a full MCP server
restart to enter the tool dispatcher (``mcp_registry_reload`` only hot-patches
code inside existing callables).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.websearch.capabilities import (
        web_fetch,
        web_search,
        web_search_health,
    )

    register_op("op.wb.web_search", web_search)
    register_op("op.wb.web_search_health", web_search_health)
    register_op("op.wb.web_fetch", web_fetch)


_register()
