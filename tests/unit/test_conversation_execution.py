"""Conversation-scoped execution persistence and producer provenance."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from work_buddy.conversations import store
from work_buddy.conversations.execution import (
    ConversationExecutionConflict,
    ConversationExecutionCorrupt,
    EXECUTION_METADATA_KEY,
    projected_execution,
    set_execution,
)


@pytest.fixture
def isolated_conversations(tmp_path, monkeypatch):
    database = tmp_path / "conversation-execution.db"
    monkeypatch.setattr(store, "_DB_PATH", database)
    conn = store.get_connection()
    try:
        store._ensure_schema(conn)
    finally:
        conn.close()
    return database


def _selection(
    provider_id: str = "claude-code",
    model_id: str = "sonnet",
) -> dict[str, str]:
    provider_label = "Claude Code" if provider_id == "claude-code" else "Codex"
    model_label = "Sonnet" if model_id == "sonnet" else "GPT-5.6 Sol"
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "provider_label": provider_label,
        "model_label": model_label,
    }


def test_projected_default_is_read_only_and_pin_preserves_binding_metadata(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation(
        "Document chat",
        source="cowork_document",
        metadata={
            "cowork_document_id": "doc-a",
            "cowork_store_id": "store-a",
        },
    )
    projected = projected_execution(conversation.conversation_id, _selection())
    assert projected.persisted is False
    assert projected.revision is None
    unchanged = store.get_conversation(conversation.conversation_id)
    assert unchanged is not None
    assert EXECUTION_METADATA_KEY not in unchanged.metadata

    pinned = set_execution(
        conversation.conversation_id,
        _selection(),
        expected_revision=None,
    )
    assert pinned.persisted is True
    assert pinned.revision
    saved = store.get_conversation(conversation.conversation_id)
    assert saved is not None
    assert saved.metadata["cowork_document_id"] == "doc-a"
    assert saved.metadata["cowork_store_id"] == "store-a"
    assert (
        saved.metadata[EXECUTION_METADATA_KEY]["revision"]
        == pinned.revision
    )


def test_execution_compare_and_swap_rejects_stale_picker(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation("Document chat")
    first = set_execution(
        conversation.conversation_id,
        _selection(),
        expected_revision=None,
    )
    changed = set_execution(
        conversation.conversation_id,
        _selection("codex", "gpt-5.6-sol"),
        expected_revision=first.revision,
    )
    assert changed.revision != first.revision
    with pytest.raises(
        ConversationExecutionConflict,
        match="execution_selection_changed",
    ):
        set_execution(
            conversation.conversation_id,
            _selection(),
            expected_revision=first.revision,
        )


def test_pinned_execution_never_resolves_an_invalid_global_default(isolated_conversations):
    conversation = store.create_conversation("Pinned chat")
    pinned = set_execution(conversation.conversation_id, _selection(), expected_revision=None)

    def unavailable_default():
        raise RuntimeError("invalid global default")

    assert projected_execution(conversation.conversation_id, unavailable_default) == pinned
    assert projected_execution(conversation.conversation_id, {}) == pinned
    unbound = store.create_conversation("New chat")
    with pytest.raises(RuntimeError, match="invalid global default"):
        projected_execution(unbound.conversation_id, unavailable_default)
    with pytest.raises(RuntimeError, match="invalid global default"):
        projected_execution(None, unavailable_default)


def test_unbound_execution_resolves_latest_default_without_repinning_chats(isolated_conversations):
    first = store.create_conversation("First chat")
    second = store.create_conversation("Second chat")
    selected = _selection()
    projected = projected_execution(first.conversation_id, lambda: selected)
    pinned = set_execution(first.conversation_id, projected.to_dict(), expected_revision=None)
    selected = _selection("codex", "gpt-5.6-sol")
    assert projected_execution(first.conversation_id, lambda: selected) == pinned
    assert projected_execution(second.conversation_id, lambda: selected).provider_id == "codex"


def test_execution_same_pair_retry_is_idempotent_after_response_loss(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation("Document chat")
    first = set_execution(
        conversation.conversation_id,
        _selection(),
        expected_revision=None,
    )
    changed = set_execution(
        conversation.conversation_id,
        _selection("codex", "gpt-5.6-sol"),
        expected_revision=first.revision,
    )
    relabeled = _selection("codex", "gpt-5.6-sol")
    relabeled["provider_label"] = "Codex (renamed)"
    relabeled["model_label"] = "GPT-5.6 Sol (renamed)"

    replayed = set_execution(
        conversation.conversation_id,
        relabeled,
        expected_revision=first.revision,
    )

    assert replayed == changed


@pytest.mark.parametrize(
    "saved_execution",
    [
        {"schema_version": 999},
        {"schema_version": True, **_selection()},
        _selection(),
    ],
)
def test_corrupt_saved_execution_fails_closed_instead_of_defaulting(
    isolated_conversations,
    saved_execution: dict[str, object],
) -> None:
    conversation = store.create_conversation(
        "Document chat",
        metadata={EXECUTION_METADATA_KEY: saved_execution},
    )

    with pytest.raises(ConversationExecutionCorrupt):
        projected_execution(conversation.conversation_id, _selection())
    with pytest.raises(ConversationExecutionCorrupt):
        set_execution(
            conversation.conversation_id,
            _selection(),
            expected_revision=None,
        )


def test_malformed_conversation_metadata_is_not_overwritten(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation(
        "Document chat",
        metadata={
            "cowork_document_id": "doc-a",
            "cowork_store_id": "store-a",
        },
    )
    malformed = '{"cowork_document_id":"doc-a"'
    conn = store.get_connection()
    try:
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            (malformed, conversation.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ConversationExecutionCorrupt):
        set_execution(
            conversation.conversation_id,
            _selection(),
            expected_revision=None,
        )

    conn = store.get_connection()
    try:
        row = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (conversation.conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["metadata"] == malformed


def test_projected_execution_rejects_malformed_outer_metadata(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation("Document chat")
    conn = store.get_connection()
    try:
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            ('{"broken"', conversation.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        ConversationExecutionCorrupt,
        match="Saved conversation metadata is invalid",
    ):
        projected_execution(conversation.conversation_id, _selection())


def test_additive_execution_schema_migrates_a_legacy_conversation_db(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "legacy-conversations.db"
    legacy = sqlite3.connect(database)
    try:
        legacy.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title           TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'open',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                source          TEXT NOT NULL DEFAULT '',
                metadata        TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE messages (
                message_id      TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'agent',
                content         TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                message_type    TEXT NOT NULL DEFAULT 'text',
                response_type   TEXT NOT NULL DEFAULT 'none',
                choices         TEXT,
                response        TEXT,
                status          TEXT NOT NULL DEFAULT 'sent',
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
            );
            CREATE TABLE conversation_agent_leases (
                conversation_id TEXT NOT NULL,
                consumer        TEXT NOT NULL,
                generation      TEXT NOT NULL,
                status          TEXT NOT NULL,
                pid             INTEGER,
                started_at      TEXT NOT NULL,
                heartbeat_at    TEXT,
                updated_at      TEXT NOT NULL,
                error           TEXT,
                PRIMARY KEY (conversation_id, consumer),
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(conversation_id)
            );
            """
        )
        now = "2026-07-28T12:00:00+00:00"
        legacy.execute(
            """INSERT INTO conversations
                   (conversation_id, title, status, created_at, updated_at,
                    source, metadata)
               VALUES (?, ?, 'open', ?, ?, ?, ?)""",
            (
                "legacy-conversation",
                "Legacy document chat",
                now,
                now,
                "cowork_document",
                json.dumps(
                    {
                        "cowork_document_id": "legacy-doc",
                        "cowork_store_id": "legacy-store",
                    }
                ),
            ),
        )
        legacy.execute(
            """INSERT INTO messages
                   (message_id, conversation_id, role, content, created_at)
               VALUES (?, ?, 'agent', ?, ?)""",
            (
                "legacy-message",
                "legacy-conversation",
                "A message from before execution profiles.",
                now,
            ),
        )
        legacy.execute(
            """INSERT INTO conversation_agent_leases
                   (conversation_id, consumer, generation, status, pid,
                    started_at, heartbeat_at, updated_at, error)
               VALUES (?, ?, ?, 'running', ?, ?, ?, ?, NULL)""",
            (
                "legacy-conversation",
                "cowork-document:legacy-store:legacy-doc",
                "legacy-generation",
                43210,
                now,
                now,
                now,
            ),
        )
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setattr(store, "_DB_PATH", database)
    migrated = store.get_connection()
    try:
        store._ensure_schema(migrated)
        message_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(messages)").fetchall()
        }
        lease_columns = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(conversation_agent_leases)"
            ).fetchall()
        }
    finally:
        migrated.close()

    assert "producer" in message_columns
    assert "execution_json" in lease_columns
    bundle = store.get_conversation_with_messages("legacy-conversation")
    assert bundle is not None
    assert bundle["messages"][0]["producer"] is None
    lease = store.get_agent_lease(
        "legacy-conversation",
        "cowork-document:legacy-store:legacy-doc",
    )
    assert lease is not None
    assert lease["execution"] is None


def test_lease_snapshots_execution_and_does_not_reuse_a_different_target(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    claude = _selection()
    first = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        "generation-claude",
        execution=claude,
    )
    assert first is not None and first["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        "generation-claude",
        701,
    )
    codex = _selection("codex", "gpt-5.6-sol")
    rotated = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        "generation-codex",
        execution=codex,
    )
    assert rotated is not None and rotated["claimed"] is True
    assert rotated["execution"] == codex
    persisted = store.get_agent_lease(conversation.conversation_id, consumer)
    assert persisted is not None
    assert persisted["generation"] == "generation-codex"
    assert persisted["execution"] == codex


def test_agent_message_producer_comes_from_exact_lease(
    isolated_conversations,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    assert send is not None
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    generation = "generation-codex"
    execution = {
        "schema_version": 1,
        **_selection("codex", "gpt-5.6-sol"),
    }
    lease = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        execution=execution,
    )
    assert lease is not None and lease["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        702,
    )
    result = send(
        conversation.conversation_id,
        "Generated through Codex.",
        "reply-codex-1",
        consumer,
        generation,
    )
    assert result["created"] is True

    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    [message] = bundle["messages"]
    assert message["producer"] == execution
    conn = store.get_connection()
    try:
        raw = conn.execute(
            "SELECT producer FROM messages WHERE message_id = ?",
            ("reply-codex-1",),
        ).fetchone()
    finally:
        conn.close()
    assert raw is not None
    assert json.loads(raw["producer"]) == execution


@pytest.mark.parametrize("operation_name", ["conversation_send", "conversation_ask"])
@pytest.mark.parametrize(
    "corrupt_execution_json",
    [
        "not-json",
        json.dumps(_selection()),
        json.dumps({"schema_version": 2, **_selection()}),
        json.dumps({"schema_version": True, **_selection()}),
    ],
)
def test_cowork_agent_message_fails_closed_without_valid_lease_provenance(
    isolated_conversations,
    operation_name: str,
    corrupt_execution_json: str,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    operation = op_registry.get_op(f"op.wb.{operation_name}")
    assert operation is not None
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    generation = "generation-corrupt-provenance"
    claimed = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        execution={"schema_version": 1, **_selection()},
    )
    assert claimed is not None and claimed["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        702,
    )
    conn = store.get_connection()
    try:
        conn.execute(
            """UPDATE conversation_agent_leases
               SET execution_json = ?
               WHERE conversation_id = ? AND consumer = ? AND generation = ?""",
            (
                corrupt_execution_json,
                conversation.conversation_id,
                consumer,
                generation,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    common = {
        "conversation_id": conversation.conversation_id,
        "consumer": consumer,
        "generation": generation,
        "agent_session_id": f"{generation}-cowork",
    }
    result = (
        operation(message="Must not land.", **common)
        if operation_name == "conversation_send"
        else operation(question="Must not land?", **common)
    )

    assert result["status"] == "lease_lost"
    bundle = store.get_conversation_with_messages(conversation.conversation_id)
    assert bundle is not None
    assert bundle["messages"] == []


def test_cowork_execution_session_cannot_omit_or_swap_its_write_fence(
    isolated_conversations,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    assert send is not None
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    generation = "generation-bound"
    lease = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        execution={
            "schema_version": 1,
            **_selection(),
        },
    )
    assert lease is not None
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        703,
    )
    session_id = f"{generation}-cowork"

    omitted = send(
        conversation.conversation_id,
        "Must not land.",
        agent_session_id=session_id,
    )
    swapped = send(
        conversation.conversation_id,
        "Must not land either.",
        consumer=consumer,
        generation="different-generation",
        agent_session_id=session_id,
    )

    assert omitted["status"] == "invalid_request"
    assert swapped["status"] == "lease_lost"
    bundle = store.get_conversation_with_messages(
        conversation.conversation_id
    )
    assert bundle is not None
    assert bundle["messages"] == []


def test_cowork_execution_session_cannot_read_or_ack_another_lease(
    isolated_conversations,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    poll = op_registry.get_op("op.wb.conversation_poll")
    receive = op_registry.get_op("op.wb.conversation_receive")
    acknowledge = op_registry.get_op("op.wb.conversation_ack")
    assert poll is not None
    assert receive is not None
    assert acknowledge is not None

    own = store.create_conversation("Own document chat")
    other = store.create_conversation("Other document chat")
    own_consumer = "cowork-document:store-a:doc-a"
    other_consumer = "cowork-document:store-b:doc-b"
    own_generation = "generation-own"
    other_generation = "generation-other"
    for conversation, consumer, generation, pid in (
        (own, own_consumer, own_generation, 704),
        (other, other_consumer, other_generation, 705),
    ):
        claimed = store.claim_agent_lease(
            conversation.conversation_id,
            consumer,
            generation,
            execution={"schema_version": 1, **_selection()},
        )
        assert claimed is not None and claimed["claimed"] is True
        assert store.activate_agent_lease(
            conversation.conversation_id,
            consumer,
            generation,
            pid,
        )

    session_id = f"{own_generation}-cowork"
    for result in (
        poll(
            other.conversation_id,
            consumer=other_consumer,
            generation=other_generation,
            agent_session_id=session_id,
        ),
        receive(
            other.conversation_id,
            other_consumer,
            other_generation,
            agent_session_id=session_id,
        ),
        acknowledge(
            other.conversation_id,
            other_consumer,
            other_generation,
            "unknown-message",
            agent_session_id=session_id,
        ),
        poll(
            other.conversation_id,
            consumer=own_consumer,
            generation=own_generation,
            agent_session_id=session_id,
        ),
    ):
        assert result["status"] == "lease_lost"


@pytest.mark.parametrize(
    "operation_name",
    ["conversation_ask", "conversation_poll"],
)
def test_cowork_long_wait_stops_reading_after_generation_fence(
    isolated_conversations,
    monkeypatch,
    operation_name: str,
) -> None:
    import time

    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    operation = op_registry.get_op(f"op.wb.{operation_name}")
    assert operation is not None
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    generation = "generation-long-wait"
    claimed = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        execution={"schema_version": 1, **_selection()},
    )
    assert claimed is not None and claimed["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        706,
    )
    if operation_name == "conversation_poll":
        assert store.add_message(
            conversation.conversation_id,
            "agent",
            "Choose one?",
            message_type="question",
            response_type="choice",
            choices=[{"key": "a", "label": "A"}],
        ) is not None

    sleeps = 0

    def fence_on_wait(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            assert store.stop_agent_lease(
                conversation.conversation_id,
                consumer,
                generation,
            )
            pending = store.get_pending_question(conversation.conversation_id)
            assert pending is not None
            assert store.respond_to_message(
                pending.message_id,
                "Arrived after the old driver was fenced.",
            ) is not None

    monkeypatch.setattr(time, "sleep", fence_on_wait)
    common = {
        "conversation_id": conversation.conversation_id,
        "consumer": consumer,
        "generation": generation,
        "timeout_seconds": 1,
    }
    result = (
        operation(question="Continue?", **common)
        if operation_name == "conversation_ask"
        else operation(**common)
    )

    assert result["status"] == "lease_lost"
    assert "response" not in result
    assert sleeps == 1


def test_receive_cannot_observe_turn_committed_after_generation_fence(
    isolated_conversations,
) -> None:
    conversation = store.create_conversation("Document chat")
    consumer = "cowork-document:store-a:doc-a"
    generation = "generation-receive-race"
    claimed = store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        execution={"schema_version": 1, **_selection()},
    )
    assert claimed is not None and claimed["claimed"] is True
    assert store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        707,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        receiving = pool.submit(
            store.receive_user_message,
            conversation.conversation_id,
            consumer,
            generation,
            timeout_seconds=0.5,
            poll_interval_seconds=0.01,
        )
        assert store.stop_agent_lease(
            conversation.conversation_id,
            consumer,
            generation,
        )
        assert store.add_message(
            conversation.conversation_id,
            "user",
            "Arrived after the old driver was fenced.",
        ) is not None
        result = receiving.result(timeout=2)

    assert result["status"] == "lease_lost"
