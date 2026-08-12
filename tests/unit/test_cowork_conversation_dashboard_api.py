"""Dashboard conversation API laws used by the Co-work chat composer."""

from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from work_buddy.conversations import store
from work_buddy.conversations.models import ConversationMessage
from work_buddy.cowork.document_agent import document_agent_consumer


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    database = tmp_path / "throwaway-dashboard-conversations.db"
    monkeypatch.setattr(store, "_DB_PATH", database)
    conn = store.get_connection()
    try:
        store._ensure_schema(conn)
    finally:
        conn.close()

    from work_buddy.dashboard import service
    from work_buddy.dashboard import local_identity_api
    from work_buddy.cowork import conversation_source_dependencies

    monkeypatch.setattr(service, "_is_read_only", lambda: False)
    monkeypatch.setattr(
        conversation_source_dependencies,
        "_DB_PATH",
        tmp_path / "throwaway-conversation-dependencies.db",
    )
    monkeypatch.setattr(
        local_identity_api,
        "require_human_authority_request",
        lambda **_kwargs: SimpleNamespace(
            principal=SimpleNamespace(actor=SimpleNamespace(canonical_id="reviewer")),
            to_input_ingress=lambda: {
                "schema": "wb.conversation-message-ingress/v1",
                "inputter": {
                    "schema": "wb.actor-ref/v1",
                    "issuer_authority_id": "issuer-dashboard-tests",
                    "subject": "human-dashboard-tests",
                    "kind": "human",
                    "tenant_scope_id": "tenant-dashboard-tests",
                },
                "session_id_sha256": "1" * 64,
                "gesture_id": "gesture-dashboard-tests",
                "action": "cowork.chat.message_send",
                "subject_sha256": "2" * 64,
                "context_sha256": "3" * 64,
                "assurance": "enrolled_local_session_gesture",
                "basis": "authenticated_loopback_ui_gesture",
                "threat_model_limit": "single_local_os_user_not_proven",
            },
        ),
    )
    service.app.config.update(TESTING=True)
    with service.app.test_client() as client:
        yield client


def _conversation(*, source: str = "cowork_document", suffix: str = "a"):
    return store.create_conversation(
        title="Throwaway dashboard conversation",
        source=source,
        metadata=(
            {
                "cowork_store_id": f"store-{suffix}",
                "cowork_document_id": f"doc-{suffix}",
            }
            if source == "cowork_document"
            else {}
        ),
    )


def test_cowork_composer_turn_does_not_consume_pending_question(
    dashboard_client,
) -> None:
    conversation = _conversation()
    pending = store.add_message(
        conversation.conversation_id,
        "agent",
        "Choose explicitly.",
        message_type="question",
        response_type="boolean",
    )
    assert pending is not None

    sent = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "This is a separate composer thought."},
    )
    assert sent.status_code == 200
    assert sent.get_json()["sent"] is True
    still_pending = store.get_pending_question(conversation.conversation_id)
    assert still_pending is not None
    assert still_pending.message_id == pending.message_id


def test_caller_stable_message_id_replays_once_and_conflicts_on_new_payload(
    dashboard_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.dashboard import service

    conversation = _conversation()
    monkeypatch.setattr(service, "_wake_persisted_cowork_turn", lambda _id: None)
    endpoint = f"/api/conversations/{conversation.conversation_id}/respond"
    authored = {
        "value": "Persist this exact turn once.",
        "message_id": "chat-user-stable-retry",
    }

    first = dashboard_client.post(endpoint, json=authored)
    replay = dashboard_client.post(endpoint, json=authored)

    assert first.status_code == replay.status_code == 200
    assert first.get_json()["message_id"] == "chat-user-stable-retry"
    assert replay.get_json()["message_id"] == "chat-user-stable-retry"
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    user_turns = [
        message for message in bundle["messages"] if message["role"] == "user"
    ]
    assert [message["message_id"] for message in user_turns] == [
        "chat-user-stable-retry"
    ]

    conflict = dashboard_client.post(
        endpoint,
        json={
            "value": "A different turn must not reuse that identity.",
            "message_id": "chat-user-stable-retry",
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "message_id_conflict"


def test_exact_question_response_replays_after_acknowledgement_loss(
    dashboard_client,
) -> None:
    conversation = _conversation(source="house_agent")
    question = store.add_message(
        conversation.conversation_id,
        "agent",
        "Proceed?",
        message_type="question",
        response_type="boolean",
    )
    assert question is not None
    endpoint = f"/api/conversations/{conversation.conversation_id}/respond"
    authored = {
        "value": "yes",
        "in_reply_to": question.message_id,
        "message_id": "chat-user-question-retry",
    }

    first = dashboard_client.post(endpoint, json=authored)
    replay = dashboard_client.post(endpoint, json=authored)

    assert first.status_code == replay.status_code == 200
    assert replay.get_json()["message_id"] == "chat-user-question-retry"
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    assert [
        message["message_id"]
        for message in bundle["messages"]
        if message["role"] == "user"
    ] == ["chat-user-question-retry"]


@pytest.mark.parametrize("message_id", ["", "   ", 42])
def test_message_id_must_be_a_nonempty_string(
    dashboard_client,
    message_id: object,
) -> None:
    conversation = _conversation(source="house_agent")
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "Hello", "message_id": message_id},
    )
    assert response.status_code == 400
    assert "message_id" in response.get_json()["error"]


def test_exact_reply_is_conversation_scoped_and_single_winner(
    dashboard_client,
) -> None:
    one = _conversation(suffix="one")
    two = _conversation(suffix="two")
    question_one = store.add_message(
        one.conversation_id,
        "agent",
        "Question one",
        message_type="question",
        response_type="choice",
    )
    question_two = store.add_message(
        two.conversation_id,
        "agent",
        "Question two",
        message_type="question",
        response_type="choice",
    )
    assert question_one is not None and question_two is not None

    wrong = dashboard_client.post(
        f"/api/conversations/{one.conversation_id}/respond",
        json={"value": "yes", "in_reply_to": question_two.message_id},
    )
    assert wrong.status_code == 409
    assert wrong.get_json()["code"] == "question_unavailable"

    answered = dashboard_client.post(
        f"/api/conversations/{one.conversation_id}/respond",
        json={"value": "yes", "in_reply_to": question_one.message_id},
    )
    assert answered.status_code == 200
    payload = answered.get_json()
    assert payload["responded"] is True
    assert payload["in_reply_to"] == question_one.message_id
    assert payload["message_id"] != question_one.message_id

    replay = dashboard_client.post(
        f"/api/conversations/{one.conversation_id}/respond",
        json={"value": "no", "in_reply_to": question_one.message_id},
    )
    assert replay.status_code == 409


def test_non_cowork_conversation_keeps_latest_pending_fallback(
    dashboard_client,
) -> None:
    conversation = _conversation(source="house_agent")
    question = store.add_message(
        conversation.conversation_id,
        "agent",
        "Question",
        message_type="question",
        response_type="freeform",
    )
    assert question is not None
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "Answer"},
    )
    assert response.status_code == 200
    assert response.get_json()["responded"] is True
    assert store.get_pending_question(conversation.conversation_id) is None


def test_targeted_turn_uses_exact_cowork_context_dispatch(
    dashboard_client,
    monkeypatch,
) -> None:
    from work_buddy.cowork import chat_targets
    from work_buddy.dashboard import service

    conversation = _conversation()
    received: dict[str, object] = {}
    wake_calls: list[str] = []
    durable_context = {
        "kind": "action_snapshot",
        "action_snapshot_id": "action-targeted",
        "store_id": "store-a",
        "document_id": "doc-a",
    }

    def _post(**kwargs):
        received.update(kwargs)
        return ConversationMessage(
            message_id="targeted-user-message",
            conversation_id=conversation.conversation_id,
            role="user",
            content=str(kwargs["content"]),
            context=durable_context,
        )

    monkeypatch.setattr(chat_targets, "post_targeted_chat_message", _post)
    monkeypatch.setattr(
        service,
        "_wake_persisted_cowork_turn",
        lambda conversation_id: wake_calls.append(conversation_id),
    )
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={
            "value": "Use the exact working passage.",
            "message_id": "targeted-user-stable-id",
            "context": durable_context,
        },
    )

    assert response.status_code == 200
    assert received == {
        "conversation_id": conversation.conversation_id,
        "content": "Use the exact working passage.",
        "context": durable_context,
        "message_id": "targeted-user-stable-id",
        "ingress": received["ingress"],
    }
    assert wake_calls == [conversation.conversation_id]
    assert response.get_json() == {
        "sent": True,
        "message_id": "targeted-user-message",
        "context": durable_context,
    }


def test_cowork_send_wakes_after_persistence_inside_user_boundary(
    dashboard_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import consent
    from work_buddy.cowork import document_agent

    conversation = _conversation()
    events: list[str] = []

    @contextmanager
    def _user_boundary(source: str):
        assert source == "dashboard.cowork.chat_turn"
        events.append("boundary-enter")
        try:
            yield
        finally:
            events.append("boundary-exit")

    def _ensure(conversation_id: str):
        bundle = store.get_conversation_with_messages(conversation_id)
        assert bundle is not None
        assert bundle["messages"][-1]["content"] == "Wake from this turn."
        events.append("ensure")
        return None

    monkeypatch.setattr(consent, "user_initiated", _user_boundary)
    monkeypatch.setattr(document_agent, "ensure_bound_document_agent", _ensure)

    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "Wake from this turn."},
    )

    assert response.status_code == 200
    assert events == ["boundary-enter", "ensure", "boundary-exit"]
    projected = dashboard_client.get(
        f"/api/conversations/{conversation.conversation_id}"
    )
    assert projected.status_code == 200
    projected_conversation = projected.get_json()["conversation"]
    assert projected_conversation["agent_alive"] is False
    assert projected_conversation["agent_status"] == "stopped"


def test_cowork_send_stays_successful_when_wake_fails(
    dashboard_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.cowork import document_agent

    conversation = _conversation()

    def _fail(_conversation_id: str):
        raise RuntimeError("throwaway wake failure")

    monkeypatch.setattr(document_agent, "ensure_bound_document_agent", _fail)
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "Keep this durable."},
    )

    assert response.status_code == 200
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    assert bundle["messages"][-1]["content"] == "Keep this durable."
    projected = dashboard_client.get(
        f"/api/conversations/{conversation.conversation_id}"
    )
    assert projected.status_code == 200
    projected_conversation = projected.get_json()["conversation"]
    assert projected_conversation["agent_alive"] is False
    assert projected_conversation["agent_status"] == "stopped"


def test_bound_cowork_conversation_without_a_turn_stays_not_started(
    dashboard_client,
) -> None:
    conversation = _conversation()

    response = dashboard_client.get(
        f"/api/conversations/{conversation.conversation_id}"
    )

    assert response.status_code == 200
    projected = response.get_json()["conversation"]
    assert projected["agent_alive"] is None
    assert projected["agent_status"] == "not_started"


def test_targeted_context_cannot_answer_a_structured_question(
    dashboard_client,
) -> None:
    conversation = _conversation()
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={
            "value": "yes",
            "in_reply_to": "question-id",
            "context": {
                "kind": "action_snapshot",
                "action_snapshot_id": "action-targeted",
            },
        },
    )
    assert response.status_code == 400
    assert "cannot be attached" in response.get_json()["error"]


def test_cowork_conversation_get_projects_persisted_lease_liveness(
    dashboard_client,
) -> None:
    conversation = _conversation()
    consumer = document_agent_consumer("store-a", "doc-a")
    generation = "dashboard-generation"
    claim = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        os.getpid(),
    )

    response = dashboard_client.get(
        f"/api/conversations/{conversation.conversation_id}"
    )
    assert response.status_code == 200
    payload = response.get_json()["conversation"]
    assert payload["agent_alive"] is True
    assert payload["agent_status"] == "running"
    assert payload["agent_error"] is None


def test_restore_fence_blocks_before_human_gesture_and_message_mutation(
    dashboard_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.backups import source_foundation_restore
    from work_buddy.dashboard import local_identity_api

    conversation = _conversation()
    fence = tmp_path / "restore-pending.json"
    source_foundation_restore.write_restore_fence(
        {"snapshot_id": "snapshot-security-test"},
        path=fence,
    )
    monkeypatch.setattr(source_foundation_restore, "restore_fence_path", lambda: fence)
    authority_calls = 0

    def _must_not_consume(**_kwargs):
        nonlocal authority_calls
        authority_calls += 1
        raise AssertionError("restore fence must precede gesture consumption")

    monkeypatch.setattr(
        local_identity_api,
        "require_human_authority_request",
        _must_not_consume,
    )
    response = dashboard_client.post(
        f"/api/conversations/{conversation.conversation_id}/respond",
        json={"value": "Must remain unpersisted while restore is pending."},
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "source_foundation_restore_pending"
    assert authority_calls == 0
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    assert bundle["messages"] == []


def test_stable_message_retry_preserves_original_ingress_gesture(
    dashboard_client,
) -> None:
    del dashboard_client
    conversation = _conversation()
    ingress = {
        "schema": "wb.conversation-message-ingress/v1",
        "inputter": {"subject": "human-dashboard-tests"},
        "session_id_sha256": "1" * 64,
        "gesture_id": "gesture-original",
        "action": "cowork.chat.message_send",
        "subject_sha256": "2" * 64,
        "context_sha256": "3" * 64,
    }
    first = store.post_user_message(
        conversation.conversation_id,
        "Idempotent authored turn.",
        message_id="stable-ingress-turn",
        ingress=ingress,
    )
    replay = store.post_user_message(
        conversation.conversation_id,
        "Idempotent authored turn.",
        message_id="stable-ingress-turn",
        ingress={
            **ingress,
            "session_id_sha256": "4" * 64,
            "gesture_id": "gesture-retry",
        },
    )

    assert first is not None and replay is not None
    assert replay.message_id == first.message_id
    assert replay.ingress == ingress
