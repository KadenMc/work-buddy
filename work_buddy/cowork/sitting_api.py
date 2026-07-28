"""HTTP adapter for two-phase, client-snapshot Co-work sittings."""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.cowork import (
    conversations,
    document_agent,
    lifecycle_lock,
    sitting_lifecycle,
)
from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation


sitting_blueprint = Blueprint("cowork_sitting_lifecycle", __name__)
logger = logging.getLogger(__name__)
_DELIVERY_FAILED_MESSAGE = "Couldn’t add this to chat. Try again."
_DELIVERY_UNAVAILABLE_MESSAGE = "Chat is unavailable right now. Try again."
_DOCUMENT_RETIRED_MESSAGE = "This document is no longer available in Co-work."


def _spawn_failure_status() -> document_agent.DocumentAgentStatus:
    return document_agent.DocumentAgentStatus(
        status="spawn_failed",
        alive=False,
        started=False,
        error="Chat couldn’t start. Try again.",
    )


def _failed_delivery(
    descriptor: dict[str, Any],
    *,
    reason: str,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    return {
        **descriptor,
        "delivered": False,
        "conversation_id": conversation_id,
        "message_id": None,
        "reason": reason,
    }


def _reconcile_routing_deliveries(
    store,
    document_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Replay durable routing ids and report their actual delivery/agent state.

    The caller owns the document lifecycle lock and ``user_initiated`` context.
    Sitting decisions are already durable, so a routing or spawn failure is
    represented in the response rather than raised as an HTTP failure.
    """

    raw_deliveries = receipt.get("routing_deliveries")
    if not isinstance(raw_deliveries, list) or not raw_deliveries:
        return dict(receipt)
    descriptors = [
        dict(item) for item in raw_deliveries if isinstance(item, dict)
    ]
    try:
        active = documents.current_lifecycle(store, document_id) == "active"
    except Exception:
        logger.exception(
            "Could not inspect lifecycle before Co-work routing delivery: "
            "store=%s document=%s",
            store.store_id,
            document_id,
        )
        enriched = [
            _failed_delivery(item, reason=_DELIVERY_UNAVAILABLE_MESSAGE)
            for item in descriptors
        ]
        return {**receipt, "routing_deliveries": enriched}
    if not active:
        enriched = [
            _failed_delivery(item, reason=_DOCUMENT_RETIRED_MESSAGE)
            for item in descriptors
        ]
        return {**receipt, "routing_deliveries": enriched}

    enriched: list[dict[str, Any]] = []
    for descriptor in descriptors:
        try:
            status = conversations.deliver_decision(
                document_id=document_id,
                store_id=store.store_id,
                verb=descriptor["verb"],
                proposal_id=descriptor["proposal_id"],
                note=descriptor.get("note"),
                delivery_id=descriptor.get("delivery_id"),
            )
        except Exception:
            logger.exception(
                "Could not deliver committed Co-work routing decision: "
                "store=%s document=%s delivery=%s",
                store.store_id,
                document_id,
                descriptor.get("delivery_id"),
            )
            enriched.append(
                _failed_delivery(
                    descriptor,
                    reason=_DELIVERY_FAILED_MESSAGE,
                )
            )
            continue

        if not status.delivered:
            enriched.append(
                _failed_delivery(
                    descriptor,
                    reason=_DELIVERY_FAILED_MESSAGE,
                    conversation_id=status.conversation_id,
                )
            )
            continue

        try:
            agent_status = document_agent.ensure_document_agent(
                store_id=store.store_id,
                document_id=document_id,
                conversation_id=status.conversation_id,
            )
        except Exception:
            logger.exception(
                "Document-agent ensure failed after committed routing delivery: "
                "store=%s document=%s conversation=%s delivery=%s",
                store.store_id,
                document_id,
                status.conversation_id,
                descriptor.get("delivery_id"),
            )
            agent_status = _spawn_failure_status()
        enriched.append(
            {
                **descriptor,
                "delivered": True,
                "conversation_id": status.conversation_id,
                "message_id": status.message_id,
                "reason": None,
                "agent": agent_status.to_dict(),
            }
        )
    return {**receipt, "routing_deliveries": enriched}


def _registry():
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _store():
    store_id = str(request.args.get("store_id") or "").strip()
    if not store_id:
        raise sitting_lifecycle.SittingError(
            "store_id_required", "A folder selection is required."
        )
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise sitting_lifecycle.SittingError(
            "folder_unreachable", "The selected folder is not reachable.", status=404
        ) from exc


def _actor() -> Actor:
    from work_buddy.cowork.api import dashboard_user_ref

    return Actor("human", dashboard_user_ref(request.headers))


def _error(exc: sitting_lifecycle.SittingError):
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                    "retryable": exc.retryable,
                },
            }
        ),
        exc.status,
    )


def _reject_read_only():
    from work_buddy.cowork.api import _is_read_only

    if _is_read_only():
        raise sitting_lifecycle.SittingError(
            "read_only", "Co-work is read-only right now.", status=403
        )


@sitting_blueprint.post("/api/truth/doc/<document_id>/sitting/prepare")
def api_prepare_sitting(document_id: str):
    try:
        _reject_read_only()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise sitting_lifecycle.SittingError(
                "invalid_request", "request body must be a JSON object"
            )
        store = _store()
        from work_buddy.consent import user_initiated

        # A repeated prepare may recover a committed receipt after the client
        # lost the PUT response.  Keep that reconciliation under the same
        # lifecycle/user-action boundary as the original commit.
        with lifecycle_lock.document_lifecycle_lock(
            store.store_id,
            document_id,
        ):
            with user_initiated("dashboard.cowork.sitting"):
                intent, created = sitting_lifecycle.prepare_sitting(
                    store,
                    document_id=document_id,
                    actor=_actor(),
                    items=body.get("items"),
                    expected_file_sha256=body.get("expected_file_sha256"),
                    expected_structured_head_sha256=body.get(
                        "expected_ydoc_head_sha256"
                    ),
                    idempotency_key=body.get("idempotency_key"),
                )
                recovered_result = (
                    None
                    if intent.state != "committed" or intent.receipt is None
                    else _reconcile_routing_deliveries(
                        store,
                        document_id,
                        intent.receipt,
                    )
                )
        payload = {
            "ok": True,
            "intent_id": intent.id,
            "state": intent.state,
            "expires_at": intent.expires_at,
            "expected_file_sha256": intent.expected_file_sha256,
            "expected_ydoc_head_sha256": intent.expected_structured_head_sha256,
            "expected_snapshot_sha256": intent.expected_snapshot_sha256,
            "admitted_items": [entry["item"] for entry in intent.admitted],
            "failed_items": [entry["result"] for entry in intent.failed],
            "requires_document_commit": intent.has_apply,
        }
        if recovered_result is not None:
            payload["result"] = recovered_result
        return jsonify(payload), 201 if created else 200
    except sitting_lifecycle.SittingError as exc:
        return _error(exc)
    except InvariantViolation as exc:
        return _error(sitting_lifecycle.SittingError("invalid_request", str(exc)))


def _commit_parts() -> tuple[dict, bytes | None, str | None]:
    if request.mimetype == "multipart/form-data":
        try:
            metadata = json.loads(request.form.get("metadata") or "{}")
        except json.JSONDecodeError as exc:
            raise sitting_lifecycle.SittingError(
                "invalid_metadata", "metadata must be valid JSON"
            ) from exc
        if not isinstance(metadata, dict):
            raise sitting_lifecycle.SittingError(
                "invalid_metadata", "metadata must be a JSON object"
            )
        snapshot_part = request.files.get("snapshot")
        markdown_part = request.files.get("markdown")
        snapshot = None if snapshot_part is None else snapshot_part.read()
        if markdown_part is None:
            markdown = None
        else:
            try:
                markdown = markdown_part.read().decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise sitting_lifecycle.SittingError(
                    "invalid_markdown", "rendered Markdown must be UTF-8"
                ) from exc
        return metadata, snapshot, markdown
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise sitting_lifecycle.SittingError(
            "invalid_request", "routing-only commit body must be a JSON object"
        )
    return body, None, None


@sitting_blueprint.put(
    "/api/truth/doc/<document_id>/sitting/<intent_id>/commit"
)
def api_commit_sitting(document_id: str, intent_id: str):
    try:
        _reject_read_only()
        metadata, snapshot, markdown = _commit_parts()
        store = _store()
        from work_buddy.consent import user_initiated

        # The lifecycle lock is outer to the Truth/Ydoc sitting commit, the
        # conversations delivery, and agent ensure. Retirement therefore sees
        # and closes every binding created by this action, or wins first and
        # prevents routing from creating one.
        with lifecycle_lock.document_lifecycle_lock(
            store.store_id,
            document_id,
        ):
            with user_initiated("dashboard.cowork.sitting"):
                receipt, events = sitting_lifecycle.commit_sitting(
                    store,
                    document_id=document_id,
                    intent_id=intent_id,
                    actor=_actor(),
                    snapshot=snapshot,
                    snapshot_sha256=metadata.get("snapshot_sha256"),
                    rendered_markdown=markdown,
                    rendered_sha256=metadata.get("rendered_sha256"),
                )
                receipt = _reconcile_routing_deliveries(
                    store,
                    document_id,
                    receipt,
                )
        from work_buddy.cowork.api import _emit

        for event in events:
            _emit(
                event["event_type"],
                store.store_id,
                event["data"],
                event_id=event["event_id"],
            )
        return jsonify(receipt)
    except sitting_lifecycle.SittingError as exc:
        return _error(exc)
    except InvariantViolation as exc:
        return _error(sitting_lifecycle.SittingError("invalid_request", str(exc)))


@sitting_blueprint.delete("/api/truth/doc/<document_id>/sitting/<intent_id>")
def api_cancel_sitting(document_id: str, intent_id: str):
    try:
        _reject_read_only()
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.sitting"):
            receipt = sitting_lifecycle.cancel_sitting(
                _store(),
                document_id=document_id,
                intent_id=intent_id,
                actor=_actor(),
            )
        return jsonify(receipt)
    except sitting_lifecycle.SittingError as exc:
        return _error(exc)


def register_sitting_routes(app):
    app.register_blueprint(sitting_blueprint)
    return app


__all__ = ["register_sitting_routes", "sitting_blueprint"]
