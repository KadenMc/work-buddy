"""Contract-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy import contracts

    register_op("op.wb.contracts_summary", contracts.contracts_summary)
    register_op("op.wb.contract_health", contracts.contract_health_check)
    register_op("op.wb.active_contracts", contracts.active_contracts)
    register_op("op.wb.overdue_contracts", contracts.overdue_contracts)
    register_op("op.wb.stale_contracts", contracts.stale_contracts)


_register()
