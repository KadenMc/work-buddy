"""Exact frozen-context laws for explicitly targeted Co-work Chat turns."""

from __future__ import annotations

import base64
import os

from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import chat_targets, conversations
from work_buddy.cowork.document_agent import document_agent_consumer
from work_buddy.cowork.execution_identity import cowork_execution_session_id
from work_buddy.cowork.verify import ActionSnapshot
from work_buddy.cowork.verify import store as verify_store
from work_buddy.mcp_server import op_registry
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.identity import sha256_bytes

from .conftest import DOC_BODY, NOW


def _capture(seeded, *, source: str = "working_target"):
    store = seeded["store"]
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    state_vector = b"throwaway-chat-state-vector"
    return {
        "schema": "wb.cowork.action-snapshot/v1",
        "captureId": "throwaway-chat-capture",
        "storeId": store.store_id,
        "documentId": document.id,
        "capturedAt": NOW,
        "editGeneration": 1,
        "ydocGenerationSha256": documents.current_ydoc_generation(
            store,
            document.id,
        ),
        "snapshotBase64": base64.b64encode(
            seeded["snapshot_bytes"]
        ).decode("ascii"),
        "snapshotSha256": seeded["snapshot_sha256"],
        "stateVectorBase64": base64.b64encode(state_vector).decode("ascii"),
        "stateVectorSha256": sha256_bytes(state_vector),
        "structuredHeadSha256": head,
        "projectionMarkdown": DOC_BODY,
        "projectionSha256": sha256_bytes(DOC_BODY.encode("utf-8")),
        "target": {
            "source": source,
            "label": "Throwaway fixture",
            "wordCount": 7,
            "proseMirrorRange": None,
            "selector": {"kind": "document"},
            "targetTextSha256": sha256_bytes(DOC_BODY.encode("utf-8")),
        },
    }


def test_prepare_endpoint_persists_idempotent_frozen_chat_context(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    endpoint = (
        f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
        f"?store_id={seeded['store_id']}"
    )
    first = client.post(endpoint, json={"capture": _capture(seeded)})
    replay = client.post(endpoint, json={"capture": _capture(seeded)})

    assert first.status_code == 201
    assert replay.status_code == 201
    context = first.get_json()["context"]
    assert replay.get_json()["context"]["action_snapshot_id"] == (
        context["action_snapshot_id"]
    )
    assert context["kind"] == "action_snapshot"
    assert context["target_label"] == "Throwaway fixture"
    assert context["document_id"] == seeded["document"].id
    action = verify_store.get_record(
        seeded["store"],
        ActionSnapshot,
        context["action_snapshot_id"],
    )
    assert action is not None
    view = chat_targets.action_snapshot_view(
        seeded["store"],
        document_id=seeded["document"].id,
        action_snapshot_id=action.id,
    )
    assert view["frozen_markdown"] == DOC_BODY
    assert view["target"]["text"] == DOC_BODY


def test_targeted_turn_is_bound_to_running_agent_and_exact_snapshot(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock, ops as cowork_ops

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    monkeypatch.setattr(cowork_ops, "_registry", lambda: seeded["registry"])
    prepared = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
            f"?store_id={seeded['store_id']}"
        ),
        json={"capture": _capture(seeded)},
    )
    assert prepared.status_code == 201
    context = prepared.get_json()["context"]

    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    consumer = document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    generation = "targeted-chat-generation"
    claim = conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        os.getpid(),
    )

    message = chat_targets.post_targeted_chat_message(
        conversation_id=binding.conversation_id,
        content="Please focus on this frozen version.",
        context={
            "kind": "action_snapshot",
            "action_snapshot_id": context["action_snapshot_id"],
            "store_id": seeded["store_id"],
            "document_id": seeded["document"].id,
        },
    )
    assert message.context == context
    received = conversation_store.receive_user_message(
        binding.conversation_id,
        consumer,
        generation,
    )
    assert received["message"]["context"]["action_snapshot_id"] == (
        context["action_snapshot_id"]
    )

    view = cowork_ops.cowork_action_snapshot_get(
        seeded["store_id"],
        seeded["document"].id,
        context["action_snapshot_id"],
        message.message_id,
        agent_session_id=cowork_execution_session_id(generation),
    )
    assert view["ok"] is True
    assert view["frozen_markdown"] == DOC_BODY
    receipt_id = view["consumption_receipt_id"]
    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    reply = send(
        binding.conversation_id,
        "I used the exact frozen target.",
        message_id=f"cowork-reply-{message.message_id}",
        consumer=consumer,
        generation=generation,
        consumption_receipt_id=receipt_id,
    )
    assert reply["created"] is True
    premature = conversation_store.ack_user_message(
        binding.conversation_id,
        consumer,
        generation,
        message.message_id,
        action_snapshot_id=context["action_snapshot_id"],
    )
    assert premature["status"] == "action_snapshot_receipt_required"
    assert conversation_store.ack_user_message(
        binding.conversation_id,
        consumer,
        generation,
        message.message_id,
        action_snapshot_id=context["action_snapshot_id"],
        consumption_receipt_id=receipt_id,
    )["acked"] is True
    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assistant = [
        item for item in bundle["messages"] if item["role"] == "agent"
    ][-1]
    assert assistant["context"]["target_label"] == "Throwaway fixture"
    assert assistant["context"]["consumption"]["receipt_id"] == receipt_id


def test_missing_frozen_context_returns_receipt_bound_unavailable_path(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock, ops as cowork_ops

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    monkeypatch.setattr(cowork_ops, "_registry", lambda: seeded["registry"])
    context = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
            f"?store_id={seeded['store_id']}"
        ),
        json={"capture": _capture(seeded)},
    ).get_json()["context"]
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    consumer = document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    generation = "unavailable-target-generation"
    conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
    )
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        os.getpid(),
    )
    turn = chat_targets.post_targeted_chat_message(
        conversation_id=binding.conversation_id,
        content="Use this exact frozen target.",
        context={
            "kind": "action_snapshot",
            "action_snapshot_id": context["action_snapshot_id"],
            "store_id": seeded["store_id"],
            "document_id": seeded["document"].id,
        },
    )
    action = verify_store.get_record(
        seeded["store"],
        ActionSnapshot,
        context["action_snapshot_id"],
    )
    assert action is not None
    seeded["store"].resolve_blob_path(
        f"blobs/{action.projection_blob_sha256}"
    ).unlink()

    unavailable = cowork_ops.cowork_action_snapshot_get(
        seeded["store_id"],
        seeded["document"].id,
        context["action_snapshot_id"],
        turn.message_id,
        agent_session_id=cowork_execution_session_id(generation),
    )
    assert unavailable["ok"] is False
    assert unavailable["status"] == "action_snapshot_unavailable"
    assert unavailable["fetch_outcome"] == "unavailable"
    receipt_id = unavailable["consumption_receipt_id"]
    unavailable_replay = cowork_ops.cowork_action_snapshot_get(
        seeded["store_id"],
        seeded["document"].id,
        context["action_snapshot_id"],
        turn.message_id,
        agent_session_id=cowork_execution_session_id(generation),
    )
    assert unavailable_replay["consumption_receipt_id"] == receipt_id
    assert unavailable_replay["fetch_outcome"] == "unavailable"

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    reply_id = f"cowork-reply-{turn.message_id}"
    reply = send(
        binding.conversation_id,
        "I could not open the exact frozen context, so I made no proposal.",
        message_id=reply_id,
        consumer=consumer,
        generation=generation,
        consumption_receipt_id=receipt_id,
    )
    assert reply["created"] is True
    assert conversation_store.ack_user_message(
        binding.conversation_id,
        consumer,
        generation,
        turn.message_id,
        consumption_receipt_id=receipt_id,
    )["acked"] is True

    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assistant = [
        item for item in bundle["messages"] if item["role"] == "agent"
    ][-1]
    consumption = assistant["context"]["consumption"]
    assert consumption["receipt_id"] == receipt_id
    assert consumption["user_message_id"] == turn.message_id
    assert consumption["fetched_at"]
    assert consumption["fetch_outcome"] == "unavailable"
    assert consumption["unavailable_code"] == "action_snapshot_unavailable"


def test_targeted_reply_replays_across_generation_restart_with_new_receipt(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock, ops as cowork_ops

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    monkeypatch.setattr(cowork_ops, "_registry", lambda: seeded["registry"])
    context = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
            f"?store_id={seeded['store_id']}"
        ),
        json={"capture": _capture(seeded)},
    ).get_json()["context"]
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    consumer = document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    generation_one = "target-replay-generation-one"
    conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation_one,
    )
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation_one,
        os.getpid(),
    )
    turn = chat_targets.post_targeted_chat_message(
        conversation_id=binding.conversation_id,
        content="Use this exact frozen target.",
        context={
            "kind": "action_snapshot",
            "action_snapshot_id": context["action_snapshot_id"],
            "store_id": seeded["store_id"],
            "document_id": seeded["document"].id,
        },
    )
    first_view = cowork_ops.cowork_action_snapshot_get(
        seeded["store_id"],
        seeded["document"].id,
        context["action_snapshot_id"],
        turn.message_id,
        agent_session_id=cowork_execution_session_id(generation_one),
    )
    first_receipt = first_view["consumption_receipt_id"]
    reply_id = f"cowork-reply-{turn.message_id}"
    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    first_reply = send(
        binding.conversation_id,
        "The durable first-generation reply.",
        message_id=reply_id,
        consumer=consumer,
        generation=generation_one,
        consumption_receipt_id=first_receipt,
    )
    assert first_reply["created"] is True

    assert conversation_store.stop_agent_lease(
        binding.conversation_id,
        consumer,
        generation_one,
    )
    generation_two = "target-replay-generation-two"
    conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation_two,
    )
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation_two,
        os.getpid(),
    )
    redelivered = conversation_store.receive_user_message(
        binding.conversation_id,
        consumer,
        generation_two,
    )
    assert redelivered["message"]["message_id"] == turn.message_id
    second_view = cowork_ops.cowork_action_snapshot_get(
        seeded["store_id"],
        seeded["document"].id,
        context["action_snapshot_id"],
        turn.message_id,
        agent_session_id=cowork_execution_session_id(generation_two),
    )
    second_receipt = second_view["consumption_receipt_id"]
    assert second_receipt != first_receipt

    replay = send(
        binding.conversation_id,
        "This wording must not replace the durable first reply.",
        message_id=reply_id,
        consumer=consumer,
        generation=generation_two,
        consumption_receipt_id=second_receipt,
    )
    assert replay["created"] is False
    assert replay["replayed"] is True
    assert conversation_store.ack_user_message(
        binding.conversation_id,
        consumer,
        generation_two,
        turn.message_id,
        consumption_receipt_id=first_receipt,
    )["status"] == "action_snapshot_receipt_mismatch"
    assert conversation_store.ack_user_message(
        binding.conversation_id,
        consumer,
        generation_two,
        turn.message_id,
        consumption_receipt_id=second_receipt,
    )["acked"] is True

    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assistant = [
        item for item in bundle["messages"] if item["role"] == "agent"
    ]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "The durable first-generation reply."
    assert (
        assistant[0]["context"]["consumption"]["receipt_id"] == first_receipt
    )
    conn = conversation_store.get_connection()
    try:
        receipts = conn.execute(
            """SELECT receipt_id, generation, reply_message_id, acked_at
               FROM conversation_action_snapshot_receipts
               WHERE user_message_id = ? ORDER BY generation""",
            (turn.message_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [
        (row["receipt_id"], row["generation"], row["reply_message_id"])
        for row in receipts
    ] == [
        (first_receipt, generation_one, reply_id),
        (second_receipt, generation_two, reply_id),
    ]
    assert receipts[0]["acked_at"] is None
    assert receipts[1]["acked_at"] is not None


def test_targeted_turn_is_saved_when_document_agent_is_stopped(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    prepared = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
            f"?store_id={seeded['store_id']}"
        ),
        json={"capture": _capture(seeded)},
    ).get_json()["context"]
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )

    posted = chat_targets.post_targeted_chat_message(
        conversation_id=binding.conversation_id,
        content="Save this until Chat restarts.",
        context={
            "kind": "action_snapshot",
            "action_snapshot_id": prepared["action_snapshot_id"],
            "store_id": seeded["store_id"],
            "document_id": seeded["document"].id,
        },
    )

    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert bundle is not None
    assert [item["message_id"] for item in bundle["messages"]] == [
        posted.message_id
    ]
    assert bundle["messages"][0]["context"]["action_snapshot_id"] == (
        prepared["action_snapshot_id"]
    )


def test_targeted_reply_cannot_echo_context_without_fetching_it(
    client,
    seeded,
    tmp_path,
    monkeypatch,
) -> None:
    from work_buddy.cowork import lifecycle_lock

    monkeypatch.setattr(
        lifecycle_lock,
        "data_dir",
        lambda _suffix: tmp_path / "runtime-locks",
    )
    monkeypatch.setattr(
        chat_targets,
        "TruthStoreRegistry",
        lambda: seeded["registry"],
    )
    context = client.post(
        (
            f"/api/truth/doc/{seeded['document'].id}/chat/action-snapshots"
            f"?store_id={seeded['store_id']}"
        ),
        json={"capture": _capture(seeded, source="current_selection")},
    ).get_json()["context"]
    binding = conversations.ensure_document_conversation(
        document_id=seeded["document"].id,
        store_id=seeded["store_id"],
    )
    consumer = document_agent_consumer(
        seeded["store_id"],
        seeded["document"].id,
    )
    generation = "unfetched-target-generation"
    conversation_store.claim_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
    )
    assert conversation_store.activate_agent_lease(
        binding.conversation_id,
        consumer,
        generation,
        os.getpid(),
    )
    message = chat_targets.post_targeted_chat_message(
        conversation_id=binding.conversation_id,
        content="Use this exact selection.",
        context={
            "kind": "action_snapshot",
            "action_snapshot_id": context["action_snapshot_id"],
            "store_id": seeded["store_id"],
            "document_id": seeded["document"].id,
        },
    )

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    rejected = send(
        binding.conversation_id,
        "I claim I used it.",
        message_id=f"cowork-reply-{message.message_id}",
        consumer=consumer,
        generation=generation,
    )
    assert rejected["status"] == "invalid_request"
    assert "consumption receipt" in rejected["error"]
    bundle = conversation_store.get_conversation_with_messages(
        binding.conversation_id
    )
    assert [item["role"] for item in bundle["messages"]] == ["user"]
