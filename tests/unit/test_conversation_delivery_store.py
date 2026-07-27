"""Durability and exact-reply laws for the conversation inbox."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from work_buddy.conversations import store


@pytest.fixture
def isolated_conversations(tmp_path, monkeypatch):
    database = tmp_path / "throwaway-conversations.db"
    monkeypatch.setattr(store, "_DB_PATH", database)
    conn = store.get_connection()
    try:
        store._ensure_schema(conn)
    finally:
        conn.close()
    return database


def _conversation(source: str = "cowork_document"):
    return store.create_conversation(
        title="Throwaway conversation",
        source=source,
    )


def test_fresh_schema_contains_delivery_cursor_and_agent_lease(
    isolated_conversations,
) -> None:
    conn = store.get_connection()
    try:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "conversation_consumer_cursors" in tables
    assert "conversation_agent_leases" in tables


def test_ordinary_turn_does_not_answer_pending_and_exact_reply_is_scoped(
    isolated_conversations,
) -> None:
    one = _conversation()
    two = _conversation()
    question_one = store.add_message(
        one.conversation_id,
        "agent",
        "Approve this exact choice?",
        message_type="question",
        response_type="boolean",
    )
    question_two = store.add_message(
        two.conversation_id,
        "agent",
        "Question in another conversation",
        message_type="question",
        response_type="boolean",
    )
    assert question_one is not None and question_two is not None

    ordinary = store.post_user_message(one.conversation_id, "A separate thought.")
    assert ordinary is not None
    pending = store.get_pending_question(one.conversation_id)
    assert pending is not None
    assert pending.message_id == question_one.message_id

    assert (
        store.respond_to_message_with_user_message(
            one.conversation_id,
            question_two.message_id,
            "true",
        )
        is None
    )
    answered = store.respond_to_message_with_user_message(
        one.conversation_id,
        question_one.message_id,
        "true",
    )
    assert answered is not None
    assert answered.role == "user"
    assert answered.content == "true"
    assert (
        store.respond_to_message_with_user_message(
            one.conversation_id,
            question_one.message_id,
            "false",
        )
        is None
    )


def test_concurrent_exact_replies_have_one_display_message_winner(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    question = store.add_message(
        conversation.conversation_id,
        "agent",
        "One exact answer only",
        message_type="question",
        response_type="boolean",
    )
    assert question is not None

    def _respond(value: str):
        return store.respond_to_message_with_user_message(
            conversation.conversation_id,
            question.message_id,
            value,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_respond, ("true", "false")))
    winners = [message for message in outcomes if message is not None]
    assert len(winners) == 1
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    user_messages = [
        item for item in bundle["messages"] if item["role"] == "user"
    ]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] in {"true", "false"}


def test_receive_redelivers_until_exact_ordered_ack_and_generation_rotation(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    consumer = "cowork-document:store-a:doc-a"
    generation_one = "generation-one"
    claimed = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    assert claimed is not None and claimed["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
        12345,
    )
    first = store.post_user_message(
        conversation.conversation_id,
        "First turn",
        message_id="user-0001",
    )
    second = store.post_user_message(
        conversation.conversation_id,
        "Second turn",
        message_id="user-0002",
    )
    assert first is not None and second is not None

    delivered = store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    redelivered = store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    assert delivered["status"] == "message"
    assert delivered["message"]["message_id"] == first.message_id
    assert redelivered["message"]["message_id"] == first.message_id

    out_of_order = store.ack_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
        second.message_id,
    )
    assert out_of_order == {
        "status": "out_of_order",
        "acked": False,
        "next_message_id": first.message_id,
    }
    assert store.ack_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
        first.message_id,
    )["acked"] is True
    next_turn = store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    assert next_turn["message"]["message_id"] == second.message_id

    assert store.stop_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    generation_two = "generation-two"
    rotated = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_two,
    )
    assert rotated is not None and rotated["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_two,
        12346,
    )
    assert store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
    ) == {"status": "lease_lost"}
    restarted = store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation_two,
    )
    assert restarted["message"]["message_id"] == second.message_id


def test_close_revokes_lease_and_receive_loses_generation(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    consumer = "cowork-document:store-close:doc-close"
    generation = "generation-close"
    claimed = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claimed is not None and claimed["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        23456,
    )

    assert store.close_conversation(conversation.conversation_id) is True
    lease = store.get_agent_lease(conversation.conversation_id, consumer)
    assert lease is not None
    assert lease["status"] == "stopped"
    assert store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation,
    ) == {"status": "lease_lost"}


def test_caller_keyed_agent_send_is_concurrent_first_writer_wins(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    message_id = "cowork-reply-user-0001"

    def _send(content: str):
        return store.send_agent_message_idempotent(
            conversation.conversation_id,
            content,
            message_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_send, ("First wording", "Regenerated wording")))

    assert sorted(created for _message, created in outcomes) == [False, True]
    assert all(message is not None for message, _created in outcomes)
    conn = store.get_connection()
    try:
        rows = conn.execute(
            "SELECT conversation_id, role, content FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == conversation.conversation_id
    assert rows[0]["role"] == "agent"
    assert rows[0]["content"] in {"First wording", "Regenerated wording"}
    assert all(
        message.content == rows[0]["content"]
        for message, _created in outcomes
        if message is not None
    )


def test_caller_keyed_agent_send_rejects_cross_conversation_or_role_collision(
    isolated_conversations,
) -> None:
    one = _conversation()
    two = _conversation()
    key = "cowork-reply-collision"
    sent, created = store.send_agent_message_idempotent(
        one.conversation_id,
        "Original",
        key,
    )
    assert sent is not None and created is True
    with pytest.raises(sqlite3.IntegrityError, match="conversation or role boundary"):
        store.send_agent_message_idempotent(two.conversation_id, "Other", key)

    user_key = "cowork-reply-user-role"
    assert store.post_user_message(
        one.conversation_id,
        "User collision",
        message_id=user_key,
    )
    with pytest.raises(sqlite3.IntegrityError, match="conversation or role boundary"):
        store.send_agent_message_idempotent(one.conversation_id, "Agent", user_key)


def test_conversation_send_reports_created_then_replayed(
    isolated_conversations,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    assert send is not None
    conversation = _conversation()
    first = send(
        conversation.conversation_id,
        "First durable wording",
        "cowork-reply-user-created-replayed",
    )
    replay = send(
        conversation.conversation_id,
        "Different regenerated wording",
        "cowork-reply-user-created-replayed",
    )
    assert first["created"] is True
    assert first["replayed"] is False
    assert replay["created"] is False
    assert replay["replayed"] is True
    assert replay["message_id"] == first["message_id"]
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    matching = [
        item
        for item in bundle["messages"]
        if item["message_id"] == first["message_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["content"] == "First durable wording"


def test_stale_generation_cannot_send_or_ask_after_rotation(
    isolated_conversations,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    ask = op_registry.get_op("op.wb.conversation_ask")
    assert send is not None and ask is not None
    conversation = _conversation()
    consumer = "cowork-document:store-fence:doc-fence"
    old_generation = "generation-old"
    claim = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        old_generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        old_generation,
        91001,
    )
    assert store.stop_agent_lease(
        conversation.conversation_id,
        consumer,
        old_generation,
    )
    new_generation = "generation-new"
    claim = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        new_generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        new_generation,
        91002,
    )

    stale_send = send(
        conversation.conversation_id,
        "This stale reply must not land.",
        "cowork-reply-stale",
        consumer,
        old_generation,
    )
    stale_ask = ask(
        conversation.conversation_id,
        "This stale question must not land.",
        "boolean",
        None,
        None,
        consumer,
        old_generation,
    )
    assert stale_send["status"] == "lease_lost"
    assert stale_ask["status"] == "lease_lost"
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    assert bundle["messages"] == []

    current = send(
        conversation.conversation_id,
        "Current generation reply.",
        "cowork-reply-current",
        consumer,
        new_generation,
    )
    assert current["created"] is True
