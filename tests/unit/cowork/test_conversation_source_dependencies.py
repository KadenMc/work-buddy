from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.backups.source_foundation_restore import (
    SourceFoundationRestorePending,
    write_restore_fence,
)
from work_buddy.conversations import store as conversation_store
from work_buddy.cowork.conversation_source_dependencies import (
    _connect,
    conversation_dependencies_for_document,
    record_conversation_source_dependency,
    redact_document_conversation_dependencies,
)
from work_buddy.cowork.conversations import (
    ensure_document_conversation,
    find_document_conversation,
    post_feedback_message,
)


def _isolated_conversation_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        conversation_store,
        "_DB_PATH",
        tmp_path / "conversations.db",
    )
    with conversation_store.get_connection() as conn:
        conversation_store._ensure_schema(conn)


def test_quoted_assistant_output_is_scrubbed_with_document_source(
    tmp_path,
    monkeypatch,
):
    _isolated_conversation_store(tmp_path, monkeypatch)
    dependency_db = tmp_path / "conversation-dependencies.db"
    binding = ensure_document_conversation(
        document_id="document-quoted-output",
        store_id="store-quoted-output",
    )
    frozen = (
        "A sufficiently long exact sentence from the managed document is "
        "quoted here for verification."
    )
    content = f'The document says: "{frozen}"'
    message = conversation_store.add_message(
        binding.conversation_id,
        "agent",
        content,
        message_id="assistant-quoted-output",
    )
    assert message is not None
    record_conversation_source_dependency(
        store_id=binding.store_id,
        document_id=binding.document_id,
        conversation_id=binding.conversation_id,
        message_id=message.message_id,
        role="agent",
        content=message.content,
        frozen_markdown=frozen,
        input_manifest_sha256="a" * 64,
        path=dependency_db,
    )

    result = redact_document_conversation_dependencies(
        store_id=binding.store_id,
        document_id=binding.document_id,
        path=dependency_db,
    )

    assert result["complete"] is True
    assert result["scrubbed_message_ids"] == (message.message_id,)
    persisted = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert persisted is not None
    persisted_content = persisted["messages"][0]["content"]
    assert frozen not in persisted_content
    assert persisted_content.startswith("[Redacted")


def test_unclassified_or_semantic_history_blocks_cleanup_completion(
    tmp_path,
    monkeypatch,
):
    _isolated_conversation_store(tmp_path, monkeypatch)
    dependency_db = tmp_path / "conversation-dependencies.db"
    binding = ensure_document_conversation(
        document_id="document-semantic-output",
        store_id="store-semantic-output",
    )
    message = conversation_store.post_user_message(
        binding.conversation_id,
        "Please keep the central argument but make it clearer.",
        message_id="user-semantic-output",
    )
    assert message is not None

    result = redact_document_conversation_dependencies(
        store_id=binding.store_id,
        document_id=binding.document_id,
        path=dependency_db,
    )

    assert result["complete"] is False
    assert result["review_required_message_ids"] == (message.message_id,)
    dependency = conversation_dependencies_for_document(
        binding.store_id,
        binding.document_id,
        path=dependency_db,
    )[0]
    assert dependency.relationship == "semantic_derivative"
    assert dependency.state == "review_required"


def test_restore_fence_keeps_dependency_authority_read_only_and_blocks_scrub(
    tmp_path,
    monkeypatch,
):
    _isolated_conversation_store(tmp_path, monkeypatch)
    dependency_db = tmp_path / "conversation-dependencies.db"
    marker = tmp_path / "restore" / "source_foundation_restore_pending.json"
    monkeypatch.setattr(
        "work_buddy.backups.source_foundation_restore.restore_fence_path",
        lambda: marker,
    )
    binding = ensure_document_conversation(
        document_id="document-fenced-output",
        store_id="store-fenced-output",
    )
    frozen = "A long enough exact document passage remains present during restore."
    message = conversation_store.add_message(
        binding.conversation_id,
        "agent",
        frozen,
        message_id="assistant-fenced-output",
    )
    assert message is not None
    record_conversation_source_dependency(
        store_id=binding.store_id,
        document_id=binding.document_id,
        conversation_id=binding.conversation_id,
        message_id=message.message_id,
        role="agent",
        content=message.content,
        frozen_markdown=frozen,
        path=dependency_db,
    )
    write_restore_fence({"snapshot_id": "cowork-conversation-fence"}, path=marker)

    assert conversation_dependencies_for_document(
        binding.store_id,
        binding.document_id,
        path=dependency_db,
    )[0].state == "active"
    with _connect(dependency_db) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(SourceFoundationRestorePending) as record_error:
        record_conversation_source_dependency(
            store_id=binding.store_id,
            document_id=binding.document_id,
            conversation_id=binding.conversation_id,
            message_id="new-fenced-message",
            role="user",
            content="This write must be blocked.",
            path=dependency_db,
        )
    assert record_error.value.operation == (
        "cowork_conversation_source_dependencies.record"
    )
    with pytest.raises(SourceFoundationRestorePending) as redact_error:
        redact_document_conversation_dependencies(
            store_id=binding.store_id,
            document_id=binding.document_id,
            path=dependency_db,
        )
    assert redact_error.value.operation == (
        "cowork_conversation_source_dependencies.redact"
    )
    persisted = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert persisted is not None
    assert persisted["messages"][0]["content"] == frozen


def test_restore_fence_blocks_document_conversation_before_message_or_binding_write(
    tmp_path,
    monkeypatch,
):
    _isolated_conversation_store(tmp_path, monkeypatch)
    marker = tmp_path / "restore" / "source_foundation_restore_pending.json"
    monkeypatch.setattr(
        "work_buddy.backups.source_foundation_restore.restore_fence_path",
        lambda: marker,
    )
    existing = ensure_document_conversation(
        document_id="document-existing-before-fence",
        store_id="store-fenced-conversation",
    )
    write_restore_fence({"snapshot_id": "cowork-conversation-write-fence"}, path=marker)

    with pytest.raises(SourceFoundationRestorePending) as message_error:
        post_feedback_message(
            conversation_id=existing.conversation_id,
            text="This message must not be persisted.",
        )
    assert message_error.value.operation == (
        "cowork.document_conversation.post_feedback"
    )
    persisted = conversation_store.get_conversation_with_messages(
        existing.conversation_id
    )
    assert persisted is not None and persisted["messages"] == []

    with pytest.raises(SourceFoundationRestorePending) as binding_error:
        ensure_document_conversation(
            document_id="document-created-during-fence",
            store_id="store-fenced-conversation",
        )
    assert binding_error.value.operation == (
        "cowork.document_conversation.create"
    )
    assert find_document_conversation(
        document_id="document-created-during-fence",
        store_id="store-fenced-conversation",
    ) is None
