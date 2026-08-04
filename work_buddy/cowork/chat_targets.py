"""Frozen document context for explicitly targeted Co-work Chat turns.

The browser first persists an exact action snapshot in Truth. A later
conversation write carries only that snapshot's stable identifier. The two
operations intentionally preserve the global lock order:

    document lifecycle lock -> Truth/Y.Doc -> conversations

An interrupted send can therefore leave an unreferenced immutable snapshot,
but can never leave a conversation turn that points at missing or mismatched
document context. Re-preparing the same canonical capture is idempotent.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from work_buddy.conversations.models import ConversationMessage
from work_buddy.conversations.store import post_user_message
from work_buddy.cowork import conversations
from work_buddy.cowork.lifecycle_lock import document_lifecycle_lock
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CothinkItem,
    create_action_snapshot,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_bytes, utc_now
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore


CHAT_ACTION_CONTEXT_SCHEMA = "wb.conversation.message-context/v1"
CHAT_CAPTURE_SCHEMA = "wb.cowork.action-snapshot/v1"
CHAT_CONTEXT_KIND = "action_snapshot"
_TARGET_SOURCES = frozenset(
    {
        "working_target",
        "current_selection",
        "current_section",
        "whole_document",
    }
)


class CoworkChatTargetError(InvariantViolation):
    """An exact Co-work Chat context request cannot be honored."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoworkChatTargetError(
            f"{label} must be a nonempty string",
            code="invalid_action_snapshot",
            status=400,
        )
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise CoworkChatTargetError(
            f"{label} must be an object",
            code="invalid_action_snapshot",
            status=400,
        )
    return dict(value)


def _decode_base64(value: object, label: str) -> bytes:
    text = _required_text(value, label)
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise CoworkChatTargetError(
            f"{label} must be canonical base64",
            code="invalid_action_snapshot",
            status=400,
        ) from exc


def _read_blob(store: TruthStore, digest: str, label: str) -> bytes:
    if not isinstance(digest, str) or not digest:
        raise CoworkChatTargetError(
            f"{label} has no valid content address",
            code="action_snapshot_unavailable",
        )
    path = store.resolve_blob_path(f"blobs/{digest}")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CoworkChatTargetError(
            f"{label} is unavailable",
            code="action_snapshot_unavailable",
        ) from exc
    if sha256_bytes(value) != digest:
        raise CoworkChatTargetError(
            f"{label} failed integrity validation",
            code="action_snapshot_unavailable",
        )
    return value


def _read_stored_json(
    value: object,
    label: str,
    expected_type: type[dict[str, Any]] | type[list[Any]],
) -> dict[str, Any] | list[Any]:
    """Decode one immutable action-snapshot field fail-closed."""

    try:
        decoded = json.loads(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CoworkChatTargetError(
            f"{label} failed integrity validation",
            code="action_snapshot_unavailable",
        ) from exc
    if not isinstance(decoded, expected_type):
        raise CoworkChatTargetError(
            f"{label} failed integrity validation",
            code="action_snapshot_unavailable",
        )
    return decoded


def _action_record(
    store: TruthStore,
    *,
    document_id: str,
    action_snapshot_id: str,
) -> ActionSnapshot:
    action = verify_store.get_record(
        store,
        ActionSnapshot,
        _required_text(action_snapshot_id, "action_snapshot_id"),
    )
    if action is None or action.document_id != document_id:
        raise CoworkChatTargetError(
            "That frozen document context is unavailable for this document.",
            code="action_snapshot_unavailable",
            status=404,
        )
    return action


def _target_metadata(capture_target: Mapping[str, Any]) -> dict[str, Any]:
    source = capture_target.get("source")
    if source not in _TARGET_SOURCES:
        raise CoworkChatTargetError(
            "target.source is not supported",
            code="invalid_action_snapshot",
            status=400,
        )
    label = _required_text(capture_target.get("label"), "target.label")
    word_count = capture_target.get("wordCount")
    if (
        isinstance(word_count, bool)
        or not isinstance(word_count, int)
        or word_count < 0
    ):
        raise CoworkChatTargetError(
            "target.wordCount must be a nonnegative integer",
            code="invalid_action_snapshot",
            status=400,
        )
    return {
        "source": source,
        "label": label,
        "word_count": word_count,
    }


def prepare_chat_action_snapshot(
    store: TruthStore,
    *,
    document_id: str,
    capture: Mapping[str, Any],
    actor: Actor,
) -> dict[str, Any]:
    """Validate and persist one exact browser capture for a later Chat turn.

    The caller must hold the document lifecycle lock. ``create_action_snapshot``
    then owns the nested Y.Doc/Truth synchronization.
    """

    if capture.get("schema") != CHAT_CAPTURE_SCHEMA:
        raise CoworkChatTargetError(
            "unsupported action snapshot schema",
            code="invalid_action_snapshot",
            status=400,
        )
    if capture.get("storeId") != store.store_id:
        raise CoworkChatTargetError(
            "action snapshot belongs to another store",
            code="invalid_action_snapshot",
            status=400,
        )
    if capture.get("documentId") != document_id:
        raise CoworkChatTargetError(
            "action snapshot belongs to another document",
            code="invalid_action_snapshot",
            status=400,
        )
    snapshot = _decode_base64(capture.get("snapshotBase64"), "snapshotBase64")
    state_vector = _decode_base64(
        capture.get("stateVectorBase64"),
        "stateVectorBase64",
    )
    snapshot_sha256 = _required_text(
        capture.get("snapshotSha256"),
        "snapshotSha256",
    )
    if sha256_bytes(snapshot) != snapshot_sha256:
        raise CoworkChatTargetError(
            "snapshotBase64 does not match snapshotSha256",
            code="invalid_action_snapshot",
            status=400,
        )
    state_vector_sha256 = _required_text(
        capture.get("stateVectorSha256"),
        "stateVectorSha256",
    )
    if sha256_bytes(state_vector) != state_vector_sha256:
        raise CoworkChatTargetError(
            "stateVectorBase64 does not match stateVectorSha256",
            code="invalid_action_snapshot",
            status=400,
        )
    projection = _required_text(
        capture.get("projectionMarkdown"),
        "projectionMarkdown",
    )
    target = _mapping(capture.get("target"), "target")
    selector = _mapping(target.get("selector"), "target.selector")
    target_metadata = _target_metadata(target)
    expected_target_sha256 = _required_text(
        target.get("targetTextSha256"),
        "target.targetTextSha256",
    )
    try:
        action = create_action_snapshot(
            store,
            document_id=document_id,
            projection=projection,
            expected_snapshot_sha256=snapshot_sha256,
            expected_structured_head_sha256=_required_text(
                capture.get("structuredHeadSha256"),
                "structuredHeadSha256",
            ),
            expected_ydoc_generation_sha256=_required_text(
                capture.get("ydocGenerationSha256"),
                "ydocGenerationSha256",
            ),
            expected_projection_sha256=_required_text(
                capture.get("projectionSha256"),
                "projectionSha256",
            ),
            projection_receipt_id=_optional_text(
                capture.get("projectionReceiptId"),
                "projectionReceiptId",
            ),
            target=selector,
            context_boundary={
                "kind": "complete_frozen_document",
                "purpose": "document_agent_turn",
                "focus": target_metadata,
            },
            egress_boundary={
                "class": "bound_document_agent",
                "content": "complete_permitted_frozen_document",
            },
            actor=actor,
            at=str(capture.get("capturedAt") or utc_now()),
        )
    except CoworkChatTargetError:
        raise
    except InvariantViolation as exc:
        raise CoworkChatTargetError(
            str(exc),
            code="action_snapshot_changed",
        ) from exc
    if action.target_text_sha256 != expected_target_sha256:
        raise CoworkChatTargetError(
            "canonical target text does not match targetTextSha256",
            code="invalid_action_snapshot",
            status=400,
        )
    return action_snapshot_reference(store, action)


def action_snapshot_reference(
    store: TruthStore,
    action: ActionSnapshot,
) -> dict[str, Any]:
    """Return the safe, transcript-visible provenance for one action snapshot."""

    try:
        boundary = json.loads(action.context_boundary_json)
    except (TypeError, ValueError):
        boundary = {}
    focus = boundary.get("focus") if isinstance(boundary, Mapping) else None
    label = (
        focus.get("label")
        if isinstance(focus, Mapping) and isinstance(focus.get("label"), str)
        else "Whole document"
        if action.target_kind == "document"
        else "Document passage"
    )
    word_count = (
        focus.get("word_count")
        if isinstance(focus, Mapping)
        and isinstance(focus.get("word_count"), int)
        else None
    )
    return {
        "schema": CHAT_ACTION_CONTEXT_SCHEMA,
        "kind": CHAT_CONTEXT_KIND,
        "action_snapshot_id": action.id,
        "store_id": store.store_id,
        "document_id": action.document_id,
        "target_kind": action.target_kind,
        "target_label": label,
        "target_word_count": word_count,
        "target_text_sha256": action.target_text_sha256,
        "projection_sha256": action.projection_sha256,
        "captured_at": action.created_at,
    }


def action_snapshot_view(
    store: TruthStore,
    *,
    document_id: str,
    action_snapshot_id: str,
) -> dict[str, Any]:
    """Read the complete frozen document and its bounded focus with integrity."""

    action = _action_record(
        store,
        document_id=document_id,
        action_snapshot_id=action_snapshot_id,
    )
    projection_bytes = _read_blob(
        store,
        action.projection_blob_sha256,
        "frozen Markdown projection",
    )
    target_bytes = _read_blob(
        store,
        action.target_blob_sha256,
        "frozen action target",
    )
    try:
        projection = projection_bytes.decode("utf-8")
        target_text = target_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoworkChatTargetError(
            "frozen action context is not UTF-8",
            code="action_snapshot_unavailable",
        ) from exc
    target_selector = _read_stored_json(
        action.target_selector_json,
        "frozen target selector",
        dict,
    )
    context_boundary = _read_stored_json(
        action.context_boundary_json,
        "frozen context boundary",
        dict,
    )
    allowed_change_ranges = _read_stored_json(
        action.allowed_change_ranges_json,
        "frozen allowed-change ranges",
        list,
    )
    return {
        "ok": True,
        "action_snapshot_id": action.id,
        "document_id": action.document_id,
        "document_version_id": action.document_version_id,
        "frozen_markdown": projection,
        "projection_sha256": action.projection_sha256,
        "structured_head_sha256": action.structured_head_sha256,
        "ydoc_snapshot_sha256": action.ydoc_snapshot_sha256,
        "ydoc_generation_sha256": action.ydoc_generation_sha256,
        "target": {
            "kind": action.target_kind,
            "selector": target_selector,
            "text": target_text,
            "text_sha256": action.target_text_sha256,
            "context_boundary": context_boundary,
            "allowed_change_ranges": allowed_change_ranges,
        },
    }


def post_targeted_chat_message(
    *,
    conversation_id: str,
    content: str,
    context: Mapping[str, Any],
    message_id: str | None = None,
) -> ConversationMessage:
    """Append a targeted turn even when its bound agent must be restarted."""

    store_id = _required_text(context.get("store_id"), "context.store_id")
    document_id = _required_text(
        context.get("document_id"),
        "context.document_id",
    )
    action_snapshot_id = _required_text(
        context.get("action_snapshot_id"),
        "context.action_snapshot_id",
    )
    if context.get("kind") != CHAT_CONTEXT_KIND:
        raise CoworkChatTargetError(
            "unsupported conversation context",
            code="invalid_chat_context",
            status=400,
        )

    with document_lifecycle_lock(store_id, document_id):
        try:
            store = TruthStoreRegistry().open_store(store_id)
        except Exception as exc:
            raise CoworkChatTargetError(
                "That folder is not reachable by Co-work.",
                code="store_unavailable",
                status=404,
            ) from exc
        if not store.profile.document_surface.enabled:
            raise CoworkChatTargetError(
                "Co-work documents are not enabled for this folder.",
                code="document_unavailable",
                status=403,
            )
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise CoworkChatTargetError(
                "Targeted chat is unavailable for a retired document.",
                code="document_retired",
            )
        if not document_surface_allowed(store, document):
            raise CoworkChatTargetError(
                "This document is not available in Co-work for this folder.",
                code="document_unavailable",
                status=403,
            )

        # Truth is fully read before the conversations database is opened.
        action = _action_record(
            store,
            document_id=document.id,
            action_snapshot_id=action_snapshot_id,
        )
        durable_context = action_snapshot_reference(store, action)

        bound_conversation = conversations.find_document_conversation(
            document_id=document.id,
            store_id=store.store_id,
        )
        if bound_conversation != conversation_id:
            raise CoworkChatTargetError(
                "That chat is not bound to this document.",
                code="conversation_binding_mismatch",
                status=403,
            )
        message = post_user_message(
            conversation_id,
            content,
            message_id=message_id,
            context=durable_context,
        )
        if message is None:
            raise CoworkChatTargetError(
                "Conversation not found or closed.",
                code="conversation_unavailable",
                status=404,
            )
        return message


def post_cothink_discussion_message(
    *,
    store_id: str,
    document_id: str,
    item_id: str,
    canonical_sha256: str,
) -> tuple[str, ConversationMessage]:
    """Route one exact non-evidential Co-think item into document Chat.

    Discussing does not change the item's lifecycle status. The resulting user
    turn names both the immutable item/hash and its frozen action snapshot, so
    the document agent must fetch that action and produce a receipt-bound reply
    just like any explicitly targeted Chat turn.
    """

    with document_lifecycle_lock(store_id, document_id):
        try:
            store = TruthStoreRegistry().open_store(store_id)
        except Exception as exc:
            raise CoworkChatTargetError(
                "That folder is not reachable by Co-work.",
                code="store_unavailable",
                status=404,
            ) from exc
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise CoworkChatTargetError(
                "Co-think discussion is unavailable for a retired document.",
                code="document_retired",
            )
        if not document_surface_allowed(store, document):
            raise CoworkChatTargetError(
                "This document is not available in Co-work for this folder.",
                code="document_unavailable",
                status=403,
            )
        item = verify_store.get_record(store, CothinkItem, item_id)
        if item is None or item.canonical_sha256 != canonical_sha256:
            raise CoworkChatTargetError(
                "That Co-think item changed or is unavailable.",
                code="cothink_item_changed",
            )
        action = _action_record(
            store,
            document_id=document.id,
            action_snapshot_id=item.action_snapshot_id,
        )
        try:
            payload = json.loads(item.payload_json)
        except (TypeError, ValueError):
            payload = {}
        perspective = str(
            payload.get("text") if isinstance(payload, Mapping) else ""
        ).strip()
        if not perspective:
            raise CoworkChatTargetError(
                "That Co-think item has no discussion content.",
                code="cothink_item_unavailable",
            )
        binding = conversations.ensure_document_conversation(
            document_id=document.id,
            store_id=store.store_id,
        )
        durable_context = {
            **action_snapshot_reference(store, action),
            "discussion": {
                "kind": "cothink_item",
                "item_id": item.id,
                "canonical_sha256": item.canonical_sha256,
                "content": perspective,
                "rationale": item.rationale,
                "non_evidential": True,
            },
        }
        message = post_user_message(
            binding.conversation_id,
            f"Let’s discuss this Co-think perspective:\n\n{perspective}",
            context=durable_context,
        )
        if message is None:
            raise CoworkChatTargetError(
                "Conversation not found or closed.",
                code="conversation_unavailable",
                status=404,
            )
        return binding.conversation_id, message


__all__ = [
    "CHAT_ACTION_CONTEXT_SCHEMA",
    "CHAT_CAPTURE_SCHEMA",
    "CHAT_CONTEXT_KIND",
    "CoworkChatTargetError",
    "action_snapshot_reference",
    "action_snapshot_view",
    "post_targeted_chat_message",
    "post_cothink_discussion_message",
    "prepare_chat_action_snapshot",
]
