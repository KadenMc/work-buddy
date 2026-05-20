"""Entity-domain ops.

Each op here is referenced by a capability declaration (a
``kind: "capability"`` knowledge-store unit carrying a matching ``op``
field). The op_registry is the integration boundary between the
declarative knowledge-store capability units (under
``knowledge/store/entities/``) and the Python wrappers in
:mod:`work_buddy.mcp_server.context_wrappers`.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.mcp_server import context_wrappers as cw

    register_op("op.wb.entity_list", cw.entity_list)
    register_op("op.wb.entity_get", cw.entity_get)
    register_op("op.wb.entity_create", cw.entity_create)
    register_op("op.wb.entity_update", cw.entity_update)
    register_op("op.wb.entity_delete", cw.entity_delete)
    register_op("op.wb.entity_set_tags", cw.entity_set_tags)
    register_op("op.wb.entity_add_alias", cw.entity_add_alias)
    register_op("op.wb.entity_remove_alias", cw.entity_remove_alias)
    register_op("op.wb.entity_resolve", cw.entity_resolve)
    register_op("op.wb.entity_add_reference", cw.entity_add_reference)
    register_op("op.wb.entity_list_references", cw.entity_list_references)


_register()
