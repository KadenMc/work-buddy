"""LLM-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy.llm.call import llm_call
    from work_buddy.llm.submit import llm_submit
    from work_buddy.llm.with_tools import llm_with_tools
    from work_buddy.mcp_server.registry import (
        _claude_code_usage_scan,
        _escalation_recent,
        _llm_costs_query,
    )

    register_op("op.wb.llm_call", llm_call)
    register_op("op.wb.llm_submit", llm_submit)
    register_op("op.wb.llm_with_tools", llm_with_tools)
    register_op("op.wb.claude_code_usage_scan", _claude_code_usage_scan)
    register_op("op.wb.llm_costs_query", _llm_costs_query)
    register_op("op.wb.escalation_recent", _escalation_recent)


_register()
