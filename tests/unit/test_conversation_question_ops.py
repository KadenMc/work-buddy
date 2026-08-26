"""Exact question waits and form-assistance conversation tool boundaries."""

from __future__ import annotations

import time

import pytest

from work_buddy.conversations import store
from work_buddy.mcp_server import op_registry


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "conversations.db")
    with store.get_connection() as conn:
        store._ensure_schema(conn)
    op_registry.load_builtin_ops()
    return lambda name: op_registry.get_op(f"op.wb.{name}")


@pytest.mark.parametrize("operation_name", ["conversation_ask", "conversation_poll"])
def test_blocking_wait_stays_on_exact_question_when_new_question_arrives(
    ops, monkeypatch, operation_name,
):
    conversation = store.create_conversation("Exact question test")
    cid = conversation.conversation_id
    if operation_name == "conversation_poll":
        store.add_message(
            cid, "agent", "Original question?", message_id="original-question",
            message_type="question", response_type="boolean",
        )
    sleeps = 0

    def answer_then_ask_again(_seconds):
        nonlocal sleeps
        sleeps += 1
        assert sleeps == 1
        store.respond_to_message_with_user_message(cid, "original-question", "true")
        store.add_message(
            cid, "agent", "Newer question?", message_id="newer-question",
            message_type="question", response_type="boolean",
        )

    monkeypatch.setattr(time, "sleep", answer_then_ask_again)
    kwargs = {
        "conversation_id": cid, "timeout_seconds": 1,
        "message_id": "original-question",
    }
    if operation_name == "conversation_ask":
        kwargs.update(question="Original question?", response_type="boolean")
    result = ops(operation_name)(**kwargs)
    assert result["status"] == "answered"
    assert result["message_id"] == "original-question"
    assert result["response"] == "true"
    assert store.get_pending_question(cid).message_id == "newer-question"


def test_poll_can_discover_current_question_but_exact_lookup_is_scoped(ops):
    one = store.create_conversation("One")
    two = store.create_conversation("Two")
    question = store.add_message(
        one.conversation_id, "agent", "Choose?",
        message_type="question", response_type="boolean",
    )
    poll = ops("conversation_poll")
    assert poll(one.conversation_id)["message_id"] == question.message_id
    rejected = poll(two.conversation_id, message_id=question.message_id)
    assert rejected["status"] == "invalid_request"
    assert "question" not in rejected


@pytest.mark.parametrize("operation_name", [
    "conversation_send", "conversation_ask", "conversation_poll",
    "conversation_receive", "conversation_ack",
])
@pytest.mark.parametrize("session_id", [None, "ordinary-agent", "other-generation-cowork"])
def test_generic_caller_cannot_bypass_form_session_through_conversation_tools(
    ops, operation_name, session_id,
):
    conversation = store.create_conversation(
        "Bound form", source="assisted-draft:task-quick-add",
        metadata={"assistedDraft": {"sessionId": "bound-session"}},
    )
    kwargs = {"conversation_id": conversation.conversation_id, "agent_session_id": session_id}
    if operation_name == "conversation_send":
        kwargs.update(message="Must not land", message_id="forbidden-message")
    elif operation_name == "conversation_ask":
        kwargs.update(question="Must not land?", response_type="boolean")
    elif operation_name in {"conversation_receive", "conversation_ack"}:
        kwargs.update(consumer="form-assistance", generation="foreign-generation")
        if operation_name == "conversation_ack":
            kwargs["message_id"] = "forbidden-message"
    result = ops(operation_name)(**kwargs)
    assert result["status"] == "lease_lost"
    assert store.get_conversation_with_messages(conversation.conversation_id)["messages"] == []


@pytest.mark.parametrize("operation_name", ["conversation_ask", "conversation_poll"])
def test_form_question_answer_is_accounted_by_the_operation_that_returns_it(
    ops, monkeypatch, operation_name,
):
    from work_buddy.dashboard.assistance import service

    conversation = store.create_conversation("Form question", source="assisted-draft:test")
    cid = conversation.conversation_id
    consumer, generation = "form-assistance", "generation-question"
    producer = {
        "schema_version": 1, "provider_id": "codex", "model_id": "model-test",
        "provider_label": "Codex", "model_label": "Test model",
    }
    assert store.claim_agent_lease(cid, consumer, generation, execution=producer)
    assert store.activate_agent_lease(cid, consumer, generation, 990)
    scoped = []
    accounted = []
    monkeypatch.setattr(service, "assert_worker_scope", lambda **kwargs: scoped.append(kwargs))
    def unexpected_republication(**_kwargs):
        pytest.fail("Retrying an existing question must not publish it against a newer turn")

    monkeypatch.setattr(service, "bind_worker_output", unexpected_republication)
    monkeypatch.setattr(service, "account_worker_payload", lambda **kwargs: accounted.append(kwargs))
    question = store.add_message(
        cid, "agent", "Continue?", message_id="accounted-question",
        message_type="question", response_type="boolean", producer=producer,
    )
    store.respond_to_message_with_user_message(cid, question.message_id, "true")
    store.add_message(
        cid, "agent", "Newer question?", message_id="newer-pending-question",
        message_type="question", response_type="boolean", producer=producer,
    )
    kwargs = {
        "conversation_id": cid, "message_id": question.message_id,
        "consumer": consumer, "generation": generation,
        "agent_session_id": f"{generation}-assisted-draft",
    }
    if operation_name == "conversation_ask":
        kwargs.update(question="Continue?", response_type="boolean")
    result = ops(operation_name)(**kwargs)
    assert result["status"] == "answered"
    assert scoped
    assert len(accounted) == 1
    assert accounted[0]["tool_call_id"] == operation_name
    assert accounted[0]["payload"]["response"] == "true"
    assert accounted[0]["payload"]["message_id"] == question.message_id
    assert store.get_pending_question(cid).message_id == "newer-pending-question"
    if operation_name == "conversation_ask":
        changed = ops(operation_name)(**{**kwargs, "question": "Changed retry wording?"})
        assert changed["status"] == "invalid_request"
        assert len(accounted) == 1
