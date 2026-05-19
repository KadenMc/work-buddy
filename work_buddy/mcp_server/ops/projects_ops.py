"""Project-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.mcp_server import context_wrappers as cw

    register_op("op.wb.project_list", cw.project_list)
    register_op("op.wb.project_get", cw.project_get)
    register_op("op.wb.project_observe", cw.project_observe)
    register_op("op.wb.project_update", cw.project_update)
    register_op("op.wb.project_create", cw.project_create)
    register_op("op.wb.project_memory", cw.project_memory)
    register_op("op.wb.project_discover", cw.project_discover)
    register_op("op.wb.project_delete", cw.project_delete)
    register_op("op.wb.project_add_folder", cw.project_add_folder)
    register_op("op.wb.project_remove_folder", cw.project_remove_folder)
    register_op("op.wb.project_set_folder_archived", cw.project_set_folder_archived)
    register_op("op.wb.project_add_alias", cw.project_add_alias)
    register_op("op.wb.project_remove_alias", cw.project_remove_alias)
    register_op("op.wb.project_confirm_description", cw.project_confirm_description)
    register_op("op.wb.project_revisions_list", cw.project_revisions_list)
    register_op("op.wb.project_state_at", cw.project_state_at)


_register()
