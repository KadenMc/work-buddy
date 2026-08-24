"""HTTP adapter for explicit, two-phase external Markdown replacement."""

from __future__ import annotations

import json
import hashlib

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import reimport
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_text


reimport_blueprint = Blueprint("cowork_reimport_lifecycle", __name__)


def _registry():
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _store():
    store_id = str(request.args.get("store_id") or "").strip()
    if not store_id:
        raise reimport.ReimportError("store_id_required", "A folder selection is required.")
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise reimport.ReimportError(
            "folder_unreachable", "The selected folder is not reachable.", status=404
        ) from exc


def _human_actor(*, operation: str, store_id: str, document_id: str, body: dict):
    from work_buddy.cowork.api import _require_human_action

    return _require_human_action(
        operation=operation,
        store_id=store_id,
        document_id=document_id,
        body=body,
    )[1]


def _reject_read_only():
    from work_buddy.cowork.api import _is_read_only

    if _is_read_only():
        raise reimport.ReimportError(
            "read_only", "Co-work is view-only right now.", status=403
        )


def _error(exc: reimport.ReimportError):
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


@reimport_blueprint.post("/api/truth/doc/<document_id>/reimport")
def api_prepare_reimport(document_id: str):
    try:
        _reject_read_only()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise reimport.ReimportError(
                "invalid_request", "Request body must be a JSON object."
            )
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.reimport"):
            store = _store()
            intent, created = reimport.prepare_reimport(
                store,
                document_id=document_id,
                actor=_human_actor(
                    operation="reimport.prepare",
                    store_id=store.store_id,
                    document_id=document_id,
                    body=body,
                ),
                idempotency_key=body.get("idempotency_key"),
            )
        payload = {
            "ok": True,
            "intent_id": intent.id,
            "state": intent.state,
            "expires_at": intent.expires_at,
            "source_sha256": intent.expected_file_sha256,
            "source_byte_length": intent.source_byte_length,
            "prior_projection_sha256": intent.prior_projection_sha256,
            "prior_snapshot_sha256": intent.prior_snapshot_sha256,
            "prior_structured_head_sha256": intent.prior_structured_head_sha256,
            "consequence": "Replace the Co-work document with the current external Markdown and mark existing proposals stale.",
        }
        if intent.state == "committed" and intent.receipt is not None:
            payload["result"] = intent.receipt
        return jsonify(payload), 201 if created else 200
    except reimport.ReimportError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        from work_buddy.cowork.api import _local_identity_error

        return _local_identity_error(exc)
    except InvariantViolation as exc:
        return _error(reimport.ReimportError("invalid_request", str(exc)))


@reimport_blueprint.post("/api/truth/doc/<document_id>/reimport/<intent_id>/source")
def api_reimport_source(document_id: str, intent_id: str):
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise reimport.ReimportError(
                "invalid_request", "Request body must be a JSON object."
            )
        if body.get("intent_id") != intent_id:
            raise reimport.ReimportError(
                "intent_id_mismatch",
                "intent_id must match the route target.",
            )
        store = _store()
        intent, data = reimport.read_reimport_source(
            store,
            intent_id=intent_id,
            actor=_human_actor(
                operation="reimport.source_read",
                store_id=store.store_id,
                document_id=document_id,
                body=body,
            ),
        )
        if intent.document_id != document_id:
            raise reimport.ReimportError(
                "intent_document_mismatch",
                "This replacement belongs to another document.",
                status=409,
            )
        response = Response(data, mimetype="application/octet-stream")
        response.headers["ETag"] = f'"{intent.expected_file_sha256}"'
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-WB-Source-Sha256"] = intent.expected_file_sha256
        response.headers["X-WB-Source-Byte-Length"] = str(len(data))
        response.headers["X-WB-Source-Encoding"] = "utf-8"
        response.headers["X-WB-Source-BOM"] = (
            "utf-8" if data.startswith(b"\xef\xbb\xbf") else "none"
        )
        return response
    except reimport.ReimportError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        from work_buddy.cowork.api import _local_identity_error

        return _local_identity_error(exc)


@reimport_blueprint.put("/api/truth/doc/<document_id>/reimport/<intent_id>/commit")
def api_commit_reimport(document_id: str, intent_id: str):
    try:
        _reject_read_only()
        if request.mimetype != "multipart/form-data":
            raise reimport.ReimportError(
                "multipart_required",
                "Replacement commit requires metadata and a binary snapshot.",
            )
        try:
            metadata = json.loads(request.form.get("metadata") or "{}")
        except json.JSONDecodeError as exc:
            raise reimport.ReimportError(
                "invalid_metadata", "Metadata must be valid JSON."
            ) from exc
        if not isinstance(metadata, dict):
            raise reimport.ReimportError(
                "invalid_metadata", "Metadata must be a JSON object."
            )
        snapshot_part = request.files.get("snapshot")
        if snapshot_part is None:
            raise reimport.ReimportError(
                "snapshot_required", "A complete replacement snapshot is required."
            )
        store = _store()
        replacement_snapshot = snapshot_part.read()
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.reimport"):
            receipt = reimport.commit_reimport(
                store,
                document_id=document_id,
                intent_id=intent_id,
                actor=_human_actor(
                    operation="reimport.commit",
                    store_id=store.store_id,
                    document_id=document_id,
                    body={
                        "intent_id": intent_id,
                        "metadata": metadata,
                        "snapshot_sha256": hashlib.sha256(
                            replacement_snapshot
                        ).hexdigest(),
                    },
                ),
                replacement_snapshot=replacement_snapshot,
                replacement_snapshot_sha256=metadata.get("snapshot_sha256"),
            )
        from work_buddy.cowork.api import _emit

        _emit(
            "truth.doc_reimported",
            store.store_id,
            {
                "document_id": document_id,
                "document_version_id": receipt["document_version_id"],
                "source_sha256": receipt["source_sha256"],
            },
            event_id=sha256_text(f"cowork-reimport:{intent_id}"),
        )
        return jsonify(receipt)
    except reimport.ReimportError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        from work_buddy.cowork.api import _local_identity_error

        return _local_identity_error(exc)
    except InvariantViolation as exc:
        return _error(reimport.ReimportError("invalid_request", str(exc)))


@reimport_blueprint.delete("/api/truth/doc/<document_id>/reimport/<intent_id>")
def api_cancel_reimport(document_id: str, intent_id: str):
    try:
        _reject_read_only()
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.reimport"):
            store = _store()
            receipt = reimport.cancel_reimport(
                store,
                document_id=document_id,
                intent_id=intent_id,
                actor=_human_actor(
                    operation="reimport.cancel",
                    store_id=store.store_id,
                    document_id=document_id,
                    body={"intent_id": intent_id},
                ),
            )
        return jsonify(receipt)
    except reimport.ReimportError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        from work_buddy.cowork.api import _local_identity_error

        return _local_identity_error(exc)


def register_reimport_routes(app):
    app.register_blueprint(reimport_blueprint)
    return app


__all__ = ["register_reimport_routes", "reimport_blueprint"]
