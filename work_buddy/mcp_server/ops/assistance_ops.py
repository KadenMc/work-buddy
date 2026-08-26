"""The two least-authority form tools used by hosted assistance workers."""

from __future__ import annotations

from typing import Any

from work_buddy.conversations.store import ConversationLeaseLost
from work_buddy.dashboard.assistance.contracts import AssistanceError
from work_buddy.dashboard.assistance.service import get_assistance_broker
from work_buddy.mcp_server.op_registry import register_op


def assisted_draft_context_get(
    assistant_session_id: str,
    conversation_id: str,
    consumer: str,
    generation: str,
    message_id: str,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    try:
        return get_assistance_broker().context_get(
            assistant_session_id=assistant_session_id,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
            message_id=message_id,
            agent_session_id=agent_session_id,
        )
    except ConversationLeaseLost:
        return {"status": "lease_lost"}
    except AssistanceError as exc:
        return {"status": exc.code, "error": str(exc)}


def assisted_draft_propose_patch(
    assistant_session_id: str,
    conversation_id: str,
    consumer: str,
    generation: str,
    message_id: str,
    consumption_receipt_id: str,
    proposal_id: str,
    operations: list[dict[str, Any]],
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    try:
        return get_assistance_broker().propose_patch(
            assistant_session_id=assistant_session_id,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
            message_id=message_id,
            consumption_receipt_id=consumption_receipt_id,
            proposal_id=proposal_id,
            operations=operations,
            agent_session_id=agent_session_id,
        )
    except ConversationLeaseLost:
        return {"status": "lease_lost"}
    except AssistanceError as exc:
        return {"status": exc.code, "error": str(exc)}


register_op("op.wb.assisted_draft_context_get", assisted_draft_context_get)
register_op("op.wb.assisted_draft_propose_patch", assisted_draft_propose_patch)
