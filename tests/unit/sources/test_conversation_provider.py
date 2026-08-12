from __future__ import annotations

from work_buddy.conversations import store as conversations
from work_buddy.sources import (
    ActorRef,
    ConversationMessageProvider,
    ProviderRegistry,
    SourceStore,
    conversation_origin,
    resolve_source,
    source_capture_from_origin,
)


def test_conversation_provider_uses_stable_message_identity_not_equal_text(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(conversations, "_DB_PATH", tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "_LEGACY_DB_PATH", tmp_path / "legacy.db")
    with conversations.get_connection() as connection:
        conversations._ensure_schema(connection)
    first_conversation = conversations.create_conversation("First")
    second_conversation = conversations.create_conversation("Second")
    inputter = ActorRef(
        "installation-authority",
        "human-inputter-0001",
        "human",
        "tenant-conversation-source",
    )
    first = conversations.post_user_message(
        first_conversation.conversation_id,
        content="Prefer positive descriptions.",
        message_id="message-first-0001",
        ingress={
            "schema": "wb.conversation-message-ingress/v1",
            "inputter": inputter.to_dict(),
            "session_id_sha256": "1" * 64,
            "gesture_id": "gesture-conversation-source",
            "action": "cowork.chat.message_send",
            "subject_sha256": "2" * 64,
            "context_sha256": "3" * 64,
            "assurance": "enrolled_local_session_gesture",
            "basis": "authenticated_loopback_ui_gesture",
            "threat_model_limit": "single_local_os_user_not_proven",
        },
    )
    second = conversations.add_message(
        second_conversation.conversation_id,
        role="user",
        content="Prefer positive descriptions.",
        message_id="message-second-0002",
    )
    assert first is not None
    assert second is not None

    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-conversation-source"
    principal = ActorRef("installation-authority", "truth-service", "service", tenant)
    provider = ConversationMessageProvider(principal, "a" * 64)
    registry = ProviderRegistry()
    registry.register(provider)

    first_ref = source_capture_from_origin(
        sources,
        registry,
        provider_id=provider.provider_id,
        origin_ref=conversation_origin(
            conversation_id=first.conversation_id,
            message_id=first.message_id,
        ),
        principal=principal,
        purpose="truth_evidence",
        tenant_scope_id=tenant,
        originating_surface="work-buddy-conversation",
    )
    first_again = source_capture_from_origin(
        sources,
        registry,
        provider_id=provider.provider_id,
        origin_ref=conversation_origin(
            conversation_id=first.conversation_id,
            message_id=first.message_id,
        ),
        principal=principal,
        purpose="truth_evidence",
        tenant_scope_id=tenant,
        originating_surface="work-buddy-conversation",
    )
    second_ref = source_capture_from_origin(
        sources,
        registry,
        provider_id=provider.provider_id,
        origin_ref=conversation_origin(
            conversation_id=second.conversation_id,
            message_id=second.message_id,
        ),
        principal=principal,
        purpose="truth_evidence",
        tenant_scope_id=tenant,
        originating_surface="work-buddy-conversation",
    )

    assert first_ref == first_again
    assert first_ref != second_ref
    resolved = resolve_source(
        sources,
        source_ref=first_ref,
        principal=principal,
        purpose="truth_evidence",
    )
    assert resolved.content == b"Prefer positive descriptions."
    with sources.connect() as connection:
        authors = connection.execute(
            "SELECT attribution_state, actor_ref_json, basis FROM source_attributions "
            "WHERE authority_id = ? AND source_item_id = ? AND role = 'author'",
            (first_ref.authority_id, first_ref.item_id),
        ).fetchall()
    assert [(row[0], row[1]) for row in authors] == [("unknown", None)]
    assert authors[0]["basis"] == "conversation_role_user_is_not_authorship"
    with sources.connect() as connection:
        inputters = connection.execute(
            "SELECT attribution_state, actor_ref_json, basis FROM source_attributions "
            "WHERE authority_id = ? AND source_item_id = ? AND role = 'inputter'",
            (first_ref.authority_id, first_ref.item_id),
        ).fetchall()
    assert len(inputters) == 1
    assert inputters[0]["attribution_state"] == "identified"
    assert inputters[0]["actor_ref_json"] == inputter.canonical_id
    assert inputters[0]["basis"] == "authenticated_loopback_ui_gesture"
