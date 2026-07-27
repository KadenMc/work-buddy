"""HTTP adapter for two-phase, client-snapshot Co-work sittings."""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from work_buddy.cowork import conversations, sitting_lifecycle
from work_buddy.truth.contracts import Actor, InvariantViolation


sitting_blueprint = Blueprint("cowork_sitting_lifecycle", __name__)


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
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.sitting"):
            intent, created = sitting_lifecycle.prepare_sitting(
                _store(),
                document_id=document_id,
                actor=_actor(),
                items=body.get("items"),
                expected_file_sha256=body.get("expected_file_sha256"),
                expected_structured_head_sha256=body.get(
                    "expected_ydoc_head_sha256"
                ),
                idempotency_key=body.get("idempotency_key"),
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
        if intent.state == "committed" and intent.receipt is not None:
            payload["result"] = intent.receipt
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
        from work_buddy.cowork.api import _emit

        for event in events:
            _emit(
                event["event_type"],
                store.store_id,
                event["data"],
                event_id=event["event_id"],
            )
        for delivery in receipt.get("routing_deliveries", []):
            try:
                conversations.deliver_decision(
                    document_id=document_id,
                    store_id=store.store_id,
                    verb=delivery["verb"],
                    proposal_id=delivery["proposal_id"],
                    note=delivery.get("note"),
                    delivery_id=delivery.get("delivery_id"),
                )
            except Exception:  # noqa: BLE001 - durable decision already committed
                pass
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
