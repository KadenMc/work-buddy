"""Knowledge-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.knowledge import editor, query, validate
    from work_buddy.knowledge.vault_editor import mint_personal_unit
    from work_buddy.mcp_server.registry import _dev_mode_toggle

    register_op("op.wb.agent_docs", query.agent_docs)
    register_op("op.wb.agent_docs_rebuild", query.agent_docs_rebuild)
    register_op("op.wb.docs_query", query.docs_query)
    register_op("op.wb.docs_get", query.docs_get)
    register_op("op.wb.docs_index", query.docs_index_build)
    register_op("op.wb.knowledge", query.knowledge)
    register_op("op.wb.knowledge_personal", query.knowledge_personal)
    register_op("op.wb.knowledge_index_rebuild", query.knowledge_index_rebuild)
    register_op("op.wb.knowledge_index_status", query.knowledge_index_status)
    register_op("op.wb.docs_validate", validate.docs_validate)
    register_op("op.wb.docs_create", editor.docs_create)
    register_op("op.wb.docs_update", editor.docs_update)
    register_op("op.wb.docs_delete", editor.docs_delete)
    register_op("op.wb.docs_move", editor.docs_move)
    register_op("op.wb.workflow_create", editor.workflow_create)
    register_op("op.wb.workflow_update", editor.workflow_update)
    register_op("op.wb.knowledge_mint", mint_personal_unit)
    register_op("op.wb.dev_mode_toggle", _dev_mode_toggle)


_register()
