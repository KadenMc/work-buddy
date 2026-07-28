"""Focused tests for the persisted Co-work document-agent lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import document_agent


@pytest.fixture
def document_agent_store(tmp_path, monkeypatch):
    database = tmp_path / "throwaway-document-agent.db"
    monkeypatch.setattr(conversation_store, "_DB_PATH", database)
    conn = conversation_store.get_connection()
    try:
        conversation_store._ensure_schema(conn)
    finally:
        conn.close()
    return database


def _conversation():
    return conversation_store.create_conversation(
        title="Throwaway Co-work chat",
        source="cowork_document",
        metadata={
            "cowork_store_id": "store-a",
            "cowork_document_id": "doc-a",
        },
    )


def test_prompt_binds_authority_delivery_and_durable_anchor_recovery() -> None:
    prompt = document_agent.build_document_agent_prompt(
        store_id="store-a",
        document_id="doc-a",
        conversation_id="conversation-a",
        consumer="cowork-document:store-a:doc-a",
        generation="generation-a",
        producer_model="configured-model",
        conversation_history=[
            {
                "message_id": "cowork-reply-user-old",
                "role": "agent",
                "content": "Already handled.",
                "message_type": "text",
                "status": "sent",
            }
        ],
        feedback=document_agent.FeedbackPromptContext(
            text="Needs support.",
            exact="Selected sentence.",
            prefix="Before ",
            suffix=" After",
            message_id="user-new",
        ),
    )

    for binding in (
        "store_id: store-a",
        "document_id: doc-a",
        "conversation_id: conversation-a",
        "inbox_generation: generation-a",
        "producer_model: configured-model",
    ):
        assert binding in prompt
    assert "cowork_doc_get again after every receive" in prompt
    assert "exact message_id" in prompt
    assert "Never infer this" in prompt
    assert "association by matching text" in prompt
    assert "restart context only" in prompt
    assert "never act on it directly" in prompt
    assert 'message_id="cowork-reply-<that user' in prompt
    assert "created=true or replayed=true" in prompt
    assert "conversation_ack" in prompt
    assert "lease_lost" in prompt
    assert "Never write the Markdown file or Yjs state directly" in prompt
    assert "Never use conversation_ask for an open-ended/freeform prompt" in prompt
    assert "producer_model='configured-model'" in prompt
    assert "consumer='cowork-document:store-a:doc-a'" in prompt
    assert "generation='generation-a'" in prompt
    assert '"message_id": "user-new"' in prompt
    assert '"exact": "Selected sentence."' in prompt


def test_ensure_spawns_once_and_reuses_live_persisted_generation(
    document_agent_store,
) -> None:
    conversation = _conversation()
    spawns: list[dict[str, Any]] = []
    registrations: list[tuple[str, int]] = []

    def _spawn(**kwargs):
        spawns.append(dict(kwargs))
        return {"status": "ok", "pid": 43210}

    first = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43210,
        register_agent=lambda conversation_id, pid: registrations.append(
            (conversation_id, pid)
        ),
        spawn_agent=_spawn,
    )
    second = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43210,
        register_agent=lambda *_args: pytest.fail("live driver must not re-register"),
        spawn_agent=lambda **_kwargs: pytest.fail("live driver must not respawn"),
    )

    assert first == document_agent.DocumentAgentStatus(
        status="running",
        alive=True,
        started=True,
        error=None,
    )
    assert second.status == "running"
    assert second.alive is True
    assert second.started is False
    assert len(spawns) == 1
    assert spawns[0]["consumer"] == "cowork-document:store-a:doc-a"
    assert spawns[0]["generation"]
    assert registrations == [(conversation.conversation_id, 43210)]


def test_concurrent_ensure_has_one_spawn_owner(document_agent_store) -> None:
    conversation = _conversation()
    spawns: list[str] = []

    def _spawn(**kwargs):
        spawns.append(str(kwargs["generation"]))
        return {"status": "ok", "pid": 43333}

    def _ensure():
        return document_agent.ensure_document_agent(
            store_id="store-a",
            document_id="doc-a",
            conversation_id=conversation.conversation_id,
            process_alive=lambda pid: pid == 43333,
            register_agent=lambda *_args: None,
            spawn_agent=_spawn,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _index: _ensure(), range(2)))

    assert len(spawns) == 1
    assert all(status.status == "running" for status in statuses)
    assert sorted(status.started for status in statuses) == [False, True]


def test_starting_generation_can_receive_before_parent_activation(
    document_agent_store,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    assert send is not None
    conversation = _conversation()
    turn = conversation_store.post_user_message(
        conversation.conversation_id,
        "Turn available during process startup.",
    )
    assert turn is not None
    observed: dict[str, Any] = {}

    def _spawn(**kwargs):
        observed["delivery"] = conversation_store.receive_user_message(
            kwargs["conversation_id"],
            kwargs["consumer"],
            kwargs["generation"],
        )
        observed["lease_during_spawn"] = conversation_store.get_agent_lease(
            kwargs["conversation_id"],
            kwargs["consumer"],
        )
        observed["send"] = send(
            kwargs["conversation_id"],
            "Handled during startup.",
            f"cowork-reply-{turn.message_id}",
            kwargs["consumer"],
            kwargs["generation"],
        )
        return {"status": "ok", "pid": 43444}

    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43444,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert observed["delivery"]["status"] == "message"
    assert observed["delivery"]["message"]["message_id"] == turn.message_id
    assert observed["lease_during_spawn"]["status"] == "starting"
    assert observed["send"]["created"] is True
    assert status.status == "running"
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    activated = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert activated is not None
    assert activated["status"] == "running"


def test_starting_generation_empty_long_poll_stays_owned_until_activation(
    document_agent_store,
) -> None:
    conversation = _conversation()
    observed: dict[str, Any] = {}

    def _spawn(**kwargs):
        observed["delivery"] = conversation_store.receive_user_message(
            kwargs["conversation_id"],
            kwargs["consumer"],
            kwargs["generation"],
            timeout_seconds=0.03,
            poll_interval_seconds=0.01,
        )
        return {"status": "ok", "pid": 43445}

    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43445,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert observed["delivery"]["status"] == "timeout"
    assert status.status == "running"


def test_spawn_failure_is_persisted_and_raw_error_is_not_exposed(
    document_agent_store,
) -> None:
    conversation = _conversation()
    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        spawn_agent=lambda **_kwargs: {
            "status": "error",
            "error": "C:\\secret\\internal-command --api-key raw-secret",
        },
    )
    assert status.status == "spawn_failed"
    assert status.alive is False
    assert status.started is False
    assert status.error == "Chat couldn’t start. Try again."
    assert "secret" not in status.error

    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    persisted = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert persisted is not None
    assert persisted["status"] == "spawn_failed"
    assert persisted["error"] == status.error


def test_dead_pid_retry_rotates_generation_immediately(
    document_agent_store,
) -> None:
    conversation = _conversation()
    spawns: list[dict[str, Any]] = []

    def _spawn(**kwargs):
        spawns.append(dict(kwargs))
        return {"status": "ok", "pid": 50000 + len(spawns)}

    first = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 50001,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert first.status == "running"

    second = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 50002,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert second.status == "running"
    assert len(spawns) == 2
    assert spawns[0]["generation"] != spawns[1]["generation"]


def test_live_pid_has_headroom_for_long_in_flight_work(
    document_agent_store,
) -> None:
    conversation = _conversation()
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    generation = "generation-long-work"
    claim = conversation_store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        60123,
    )
    lease = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert lease is not None
    heartbeat = datetime.fromisoformat(str(lease["heartbeat_at"]))

    working = document_agent.inspect_document_agent(
        conversation.conversation_id,
        consumer=consumer,
        process_alive=lambda _pid: True,
        now=heartbeat + timedelta(minutes=10),
    )
    stale = document_agent.inspect_document_agent(
        conversation.conversation_id,
        consumer=consumer,
        process_alive=lambda _pid: True,
        now=heartbeat + timedelta(minutes=16),
    )
    assert working.status == "running"
    assert working.alive is True
    assert stale.status == "stopped"
    assert stale.alive is False


def test_spawn_session_uses_explicit_model_and_authorized_executor(
    document_agent_store,
    monkeypatch,
) -> None:
    observed: dict[str, Any] = {}

    def _authorized(**kwargs):
        observed.update(kwargs)
        return {"status": "ok", "pid": 70123}

    from work_buddy.sidecar.dispatch import executor

    monkeypatch.setattr(
        executor,
        "spawn_headless_agent_detached_authorized",
        _authorized,
    )
    result = document_agent.spawn_document_agent_session(
        store_id="store-a",
        document_id="doc-a",
        conversation_id="conversation-a",
        consumer="cowork-document:store-a:doc-a",
        generation="generation-a",
        producer_model="configured-model",
        history_loader=lambda _conversation_id: {"messages": []},
    )
    assert result == {"status": "ok", "pid": 70123}
    assert observed["name"].startswith("cowork-doc-a-conversation-a")
    assert "producer_model: configured-model" in observed["prompt"]
    assert observed["max_budget_usd"] == 2.0
