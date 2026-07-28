"""Dashboard conversation API laws used by the Co-work chat composer."""

from __future__ import annotations

import os

import pytest

from work_buddy.conversations import store
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

    monkeypatch.setattr(service, "_is_read_only", lambda: False)
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
