"""Messaging-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The declaration supplies
the prose, parameter schema, and runtime metadata; the op supplies the callable.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    # Lazy import inside the registration function, matching the
    # lazy-import discipline of the registry's capability builders
    # (see architecture/mcp-import-discipline).
    from work_buddy.messaging import client

    register_op("op.wb.send_message", client.send_message)
    register_op("op.wb.query_messages", client.query_messages)
    register_op("op.wb.read_message", client.read_message)
    register_op("op.wb.reply_to_message", client.reply)
    register_op("op.wb.update_message_status", client.update_status)
    register_op("op.wb.get_thread", client.get_thread)


_register()
