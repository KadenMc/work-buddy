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
        receipt_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(conversation_action_snapshot_receipts)"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "conversation_consumer_cursors" in tables
    assert "conversation_agent_leases" in tables
    assert "conversation_action_snapshot_receipts" in tables
    assert {
        "fetch_outcome",
        "unavailable_code",
        "fetch_resolved_at",
    } <= receipt_columns


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


def test_targeted_turn_requires_fetched_receipt_for_reply_and_ack(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    consumer = "cowork-document:store-target:doc-target"
    generation = "generation-target"
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
        12345,
    )
    context = {
        "schema": "wb.conversation.message-context/v1",
        "kind": "action_snapshot",
        "action_snapshot_id": "action-snapshot-target",
        "store_id": "store-target",
        "document_id": "doc-target",
        "target_kind": "text_quote",
        "target_label": "Introduction",
        "target_word_count": 24,
        "target_text_sha256": "a" * 64,
        "projection_sha256": "b" * 64,
        "captured_at": "2026-07-28T12:00:00+00:00",
    }
    turn = store.post_user_message(
        conversation.conversation_id,
        "Please focus here.",
        context=context,
    )
    assert turn is not None

    delivered = store.receive_user_message(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert delivered["message"]["context"] == context
    assert store.ack_user_message(
        conversation.conversation_id,
        consumer,
        generation,
        turn.message_id,
    ) == {
        "status": "action_snapshot_receipt_required",
        "acked": False,
        "action_snapshot_id": "action-snapshot-target",
    }
    receipt = store.record_action_snapshot_consumption(
        conversation.conversation_id,
        consumer,
        generation,
        turn.message_id,
        "action-snapshot-target",
    )
    resolution_conn = store.get_connection()
    try:
        receipt = store.resolve_action_snapshot_consumption(
            receipt["receipt_id"],
            "available",
            conn=resolution_conn,
        )
        resolution_conn.commit()
    finally:
        resolution_conn.close()
    reply_id = f"cowork-reply-{turn.message_id}"
    reply_context = store.targeted_reply_context(
        conversation.conversation_id,
        consumer,
        generation,
        reply_id,
        receipt["receipt_id"],
        conn=(conn := store.get_connection()),
    )
    try:
        reply, created = store.send_agent_message_idempotent(
            conversation.conversation_id,
            "Used the frozen target.",
            reply_id,
            conn=conn,
            context=reply_context,
        )
        assert reply is not None and created is True
        store.bind_action_snapshot_reply(
            receipt["receipt_id"],
            reply.message_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    assert store.ack_user_message(
        conversation.conversation_id,
        consumer,
        generation,
        turn.message_id,
        action_snapshot_id="action-snapshot-target",
        consumption_receipt_id=receipt["receipt_id"],
    ) == {
        "status": "acked",
        "acked": True,
        "message_id": turn.message_id,
    }


def test_restart_replay_rejects_a_different_targeted_turn(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    consumer = "cowork-document:store-target:doc-target"
    generation_one = "generation-target-one"
    store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
        12345,
    )
    first_context = {
        "schema": "wb.conversation.message-context/v1",
        "kind": "action_snapshot",
        "action_snapshot_id": "action-snapshot-one",
        "store_id": "store-target",
        "document_id": "doc-target",
        "target_kind": "text_quote",
        "target_label": "Introduction",
        "target_word_count": 24,
        "target_text_sha256": "a" * 64,
        "projection_sha256": "b" * 64,
        "captured_at": "2026-07-28T12:00:00+00:00",
    }
    first_turn = store.post_user_message(
        conversation.conversation_id,
        "First exact turn.",
        context=first_context,
    )
    assert first_turn is not None
    first_receipt = store.record_action_snapshot_consumption(
        conversation.conversation_id,
        consumer,
        generation_one,
        first_turn.message_id,
        "action-snapshot-one",
    )
    conn = store.get_connection()
    try:
        first_receipt = store.resolve_action_snapshot_consumption(
            first_receipt["receipt_id"],
            "available",
            conn=conn,
        )
        reply_id = "stable-but-incorrectly-reused-reply"
        reply_context = store.targeted_reply_context(
            conversation.conversation_id,
            consumer,
            generation_one,
            reply_id,
            first_receipt["receipt_id"],
            conn=conn,
        )
        reply, _created = store.send_agent_message_idempotent(
            conversation.conversation_id,
            "First reply.",
            reply_id,
            conn=conn,
            context=reply_context,
        )
        assert reply is not None
        store.bind_action_snapshot_reply(
            first_receipt["receipt_id"],
            reply_id,
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()
    assert store.ack_user_message(
        conversation.conversation_id,
        consumer,
        generation_one,
        first_turn.message_id,
        consumption_receipt_id=first_receipt["receipt_id"],
    )["acked"] is True

    second_context = {
        **first_context,
        "action_snapshot_id": "action-snapshot-two",
        "target_label": "Conclusion",
        "target_text_sha256": "c" * 64,
    }
    second_turn = store.post_user_message(
        conversation.conversation_id,
        "Second, different exact turn.",
        context=second_context,
    )
    assert second_turn is not None
    assert store.stop_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_one,
    )
    generation_two = "generation-target-two"
    store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_two,
    )
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation_two,
        12346,
    )
    second_receipt = store.record_action_snapshot_consumption(
        conversation.conversation_id,
        consumer,
        generation_two,
        second_turn.message_id,
        "action-snapshot-two",
    )
    conn = store.get_connection()
    try:
        second_receipt = store.resolve_action_snapshot_consumption(
            second_receipt["receipt_id"],
            "available",
            conn=conn,
        )
        with pytest.raises(
            ValueError,
            match="different targeted turn",
        ):
            store.targeted_reply_context(
                conversation.conversation_id,
                consumer,
                generation_two,
                reply_id,
                second_receipt["receipt_id"],
                conn=conn,
            )
    finally:
        conn.close()


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


@pytest.mark.parametrize(
    "changed",
    [
        {"response_type": "boolean"},
        {"choices": [{"key": "b", "label": "Changed choice"}]},
    ],
)
def test_keyed_question_retry_cannot_change_its_answer_schema(
    isolated_conversations, changed,
) -> None:
    conversation = _conversation()
    kwargs = {
        "conversation_id": conversation.conversation_id,
        "role": "agent",
        "content": "Which one?",
        "message_id": "exact-question-schema",
        "message_type": "question",
        "response_type": "choice",
        "choices": [{"key": "a", "label": "Original choice"}],
    }
    original = store.add_message(**kwargs)
    assert store.add_message(**kwargs).message_id == original.message_id
    with pytest.raises(sqlite3.IntegrityError, match="different message content"):
        store.add_message(**{**kwargs, **changed})
    saved = store.get_message(conversation.conversation_id, original.message_id)
    assert saved.response_type == "choice"
    assert saved.choices == kwargs["choices"]
    assert store.get_message(_conversation().conversation_id, original.message_id) is None


@pytest.mark.parametrize(
    "operation", ["create", "add", "post", "respond", "ack", "agent-send", "agent-replay"],
)
def test_composed_conversation_writes_preserve_the_callers_transaction(
    isolated_conversations, operation,
) -> None:
    conversation = _conversation()
    question = store.add_message(
        conversation.conversation_id, "agent", "Proceed?",
        message_type="question", response_type="boolean",
    )
    user = store.post_user_message(conversation.conversation_id, "Existing turn")
    consumer, generation = "test-composition", "test-generation"
    assert store.claim_agent_lease(conversation.conversation_id, consumer, generation)
    assert store.activate_agent_lease(
        conversation.conversation_id, consumer, generation, 991,
    )
    if operation == "agent-replay":
        store.send_agent_message_idempotent(
            conversation.conversation_id, "Original reply", "existing-agent-reply",
        )
    conn = store.get_connection()
    created_id = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if operation == "create":
            created_id = store.create_conversation("Rolled back", conn=conn).conversation_id
        elif operation == "add":
            store.add_message(
                conversation.conversation_id, "agent", "Rolled back",
                message_id="transaction-test-message", conn=conn,
            )
        elif operation == "post":
            store.post_user_message(
                conversation.conversation_id, "Rolled back",
                message_id="transaction-test-message", conn=conn,
            )
        elif operation == "respond":
            store.respond_to_message_with_user_message(
                conversation.conversation_id, question.message_id, "true",
                user_message_id="transaction-test-message", conn=conn,
            )
        elif operation in {"agent-send", "agent-replay"}:
            # The sentinel also catches an accidental commit on an idempotent
            # replay, where the message row itself already existed beforehand.
            store.add_message(
                conversation.conversation_id, "agent", "Rolled back",
                message_id="transaction-test-message", conn=conn,
            )
            reply, created = store.send_agent_message_idempotent(
                conversation.conversation_id, "Retry wording",
                "existing-agent-reply" if operation == "agent-replay" else "new-agent-reply",
                conn=conn,
            )
            assert created is (operation == "agent-send")
            assert reply.content == ("Original reply" if operation == "agent-replay" else "Retry wording")
        else:
            assert store.ack_user_message(
                conversation.conversation_id, consumer, generation,
                user.message_id, conn=conn,
            )["acked"]
        assert conn.in_transaction
        conn.rollback()
    finally:
        conn.close()

    assert store.get_message(conversation.conversation_id, "transaction-test-message") is None
    assert store.get_message(conversation.conversation_id, "new-agent-reply") is None
    assert store.get_message(conversation.conversation_id, question.message_id).status == "pending"
    if created_id is not None:
        assert store.get_conversation(created_id) is None
    assert store.receive_user_message(
        conversation.conversation_id, consumer, generation,
    )["message"]["message_id"] == user.message_id


def test_question_answers_persist_exact_native_lineage_and_replay_identity(
    isolated_conversations,
) -> None:
    conversation = _conversation()
    question = store.add_message(
        conversation.conversation_id, "agent", "Continue?",
        message_type="question", response_type="boolean",
    )
    answer = store.respond_to_message_with_user_message(
        conversation.conversation_id, question.message_id, "true",
        user_message_id="linked-answer", context={"surface": "test"},
    )
    assert answer.context == {"surface": "test", "in_reply_to": question.message_id}
    assert store.respond_to_message_with_user_message(
        conversation.conversation_id, question.message_id, "true",
        user_message_id="linked-answer", context={"surface": "test"},
    ).message_id == answer.message_id
    newer = store.add_message(
        conversation.conversation_id, "agent", "Another question?",
        message_type="question", response_type="boolean",
    )
    with pytest.raises(store.UserMessageIdConflictError):
        store.respond_to_message_with_user_message(
            conversation.conversation_id, newer.message_id, "true",
            user_message_id="linked-answer", context={"surface": "test"},
        )
    with pytest.raises(ValueError, match="exact question"):
        store.respond_to_message_with_user_message(
            conversation.conversation_id, newer.message_id, "true",
            context={"in_reply_to": question.message_id},
        )
    assert store.get_pending_question(conversation.conversation_id).message_id == newer.message_id


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
