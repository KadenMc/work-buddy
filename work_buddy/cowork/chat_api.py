"""Dashboard adapter for freezing context before a targeted Co-work Chat turn."""

from __future__ import annotations

from collections.abc import Mapping

from flask import Blueprint, jsonify, request

from work_buddy.cowork.api import (
    _actor_for_request,
    _document_surface_or_403,
    _emit,
    _fail,
    _open_store,
    _reject_read_only,
    _resolve_document,
)
from work_buddy.cowork.chat_targets import (
    CoworkChatTargetError,
    prepare_chat_action_snapshot,
)
from work_buddy.cowork.lifecycle_lock import document_lifecycle_lock
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth import documents
from work_buddy.truth.contracts import InvariantViolation


chat_blueprint = Blueprint("cowork_chat_context", __name__)


@chat_blueprint.post(
    "/api/truth/doc/<document_id>/chat/action-snapshots"
)
def api_prepare_chat_action_snapshot(document_id: str):
    """Persist one exact immutable context before the conversation write."""

    blocked = _reject_read_only()
    if blocked:
        return blocked
    store_id = (request.args.get("store_id") or "").strip()
    if not store_id:
        return _fail("store_id is required", 400)
    body = request.get_json(silent=True)
    if not isinstance(body, Mapping):
        return _fail("request body must be a JSON object", 400)
    capture = body.get("capture")
    if not isinstance(capture, Mapping):
        return _fail("capture is required", 400)

    try:
        # Acquire the lifecycle boundary before opening Truth. This endpoint
        # never opens conversations; the later /respond reference validation
        # repeats the same order before appending the user turn.
        with document_lifecycle_lock(store_id, document_id):
            try:
                store = _open_store(store_id)
            except Exception:
                return _fail("That folder is not reachable by Co-work.", 404)
            gate = _document_surface_or_403(store)
            if gate:
                return gate
            document, doc_error = _resolve_document(store, document_id)
            if doc_error:
                return doc_error
            if documents.current_lifecycle(store, document.id) != "active":
                return _fail(
                    "Targeted chat cannot start on a retired document.",
                    409,
                )
            if not document_surface_allowed(store, document):
                return _fail(
                    "This document is not available in Co-work for this folder.",
                    403,
                )
            result = prepare_chat_action_snapshot(
                store,
                document_id=document.id,
                capture=capture,
                actor=_actor_for_request(),
            )
    except CoworkChatTargetError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code,
                }
            ),
            exc.status,
        )
    except InvariantViolation as exc:
        return _fail(str(exc), 409)
    except Exception:
        return _fail("Co-work could not freeze this document context.", 500)

    _emit(
        "truth.doc_chat_action_snapshot_created",
        store.store_id,
        {
            "document_id": document.id,
            "action_snapshot_id": result["action_snapshot_id"],
        },
        event_id=(
            "cowork-chat-action-snapshot:"
            f"{result['action_snapshot_id']}"
        ),
    )
    return jsonify({"ok": True, "context": result}), 201


__all__ = ["chat_blueprint"]
