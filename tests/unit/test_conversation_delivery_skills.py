"""Gateway declarations for durable conversation delivery."""

from __future__ import annotations

import pytest

from work_buddy.knowledge.skill_loader import load_declared_skills
from work_buddy.mcp_server import op_registry


@pytest.fixture
def declared_skills():
    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    skills, issues = load_declared_skills()
    yield {item.name: item for item in skills}, issues
    op_registry.clear_ops()


def test_durable_delivery_ops_are_discoverable_with_their_runtime_schema(
    declared_skills,
) -> None:
    skills, issues = declared_skills
    relevant_issues = [
        issue
        for issue in issues
        if issue["path"]
        in {
            "conversations/conversation_send",
            "conversations/conversation_ask",
            "conversations/conversation_poll",
            "conversations/conversation_receive",
            "conversations/conversation_ack",
            "cowork/cowork_doc_propose_edit",
            "cowork/cowork_doc_comment",
        }
    ]
    assert relevant_issues == []

    send = skills["conversation_send"]
    assert set(send.parameters) == {
        "conversation_id",
        "message",
        "message_id",
        "consumer",
        "generation",
        "consumption_receipt_id",
    }
    assert all(
        send.parameters[name].get("required", False) is False
        for name in (
            "message_id",
            "consumer",
            "generation",
            "consumption_receipt_id",
        )
    )

    ask = skills["conversation_ask"]
    assert {"consumer", "generation", "message_id"} <= set(ask.parameters)
    assert all(
        ask.parameters[name].get("required", False) is False
        for name in ("consumer", "generation", "message_id")
    )

    poll = skills["conversation_poll"]
    assert set(poll.parameters) == {
        "conversation_id", "timeout_seconds", "consumer", "generation",
        "message_id",
    }
    assert all(
        poll.parameters[name].get("required", False) is False
        for name in ("timeout_seconds", "consumer", "generation", "message_id")
    )

    receive = skills["conversation_receive"]
    assert set(receive.parameters) == {
        "conversation_id",
        "consumer",
        "generation",
        "timeout_seconds",
    }
    assert receive.parameters["timeout_seconds"].get("required", False) is False

    acknowledge = skills["conversation_ack"]
    assert set(acknowledge.parameters) == {
        "conversation_id",
        "consumer",
        "generation",
        "message_id",
        "action_snapshot_id",
        "consumption_receipt_id",
    }
    assert all(
        acknowledge.parameters[name].get("required") is True
        for name in ("conversation_id", "consumer", "generation", "message_id")
    )
    assert acknowledge.parameters["action_snapshot_id"].get(
        "required", False
    ) is False
    assert acknowledge.parameters["consumption_receipt_id"].get(
        "required", False
    ) is False

    assert callable(
        op_registry.get_op("op.wb.conversation_receive")
    )
    assert callable(
        op_registry.get_op("op.wb.conversation_ack")
    )

    for name in (
        "cowork_doc_propose_edit",
        "cowork_doc_comment",
        "cowork_doc_expression_mark",
    ):
        skill = skills[name]
        assert {"conversation_id", "consumer", "generation"} <= set(
            skill.parameters
        )
        assert all(
            skill.parameters[field].get("required", False) is False
            for field in ("conversation_id", "consumer", "generation")
        )
