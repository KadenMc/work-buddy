"""HTTP adapter for explicit, two-phase external Markdown replacement."""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import reimport
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_text


reimport_blueprint = Blueprint("cowork_reimport_lifecycle", __name__)


def _registry():
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _store():
    store_id = str(request.args.get("store_id") or "").strip()
    if not store_id:
        raise reimport.ReimportError("store_id_required", "A Folder selection is required.")
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise reimport.ReimportError(
            "folder_unreachable", "The selected Folder is not reachable.", status=404
        ) from exc


def _actor() -> Actor:
    from work_buddy.cowork.api import dashboard_user_ref

    return Actor("human", dashboard_user_ref(request.headers))


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
            intent, created = reimport.prepare_reimport(
                _store(),
                document_id=document_id,
                actor=_actor(),
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
    except InvariantViolation as exc:
        return _error(reimport.ReimportError("invalid_request", str(exc)))


@reimport_blueprint.get("/api/truth/doc/<document_id>/reimport/<intent_id>/source")
def api_reimport_source(document_id: str, intent_id: str):
    try:
        intent, data = reimport.read_reimport_source(
            _store(), intent_id=intent_id, actor=_actor()
        )
        if intent.document_id != document_id:
            raise reimport.ReimportError(
                "intent_document_mismatch",
                "This replacement belongs to another document.",
                status=409,
            )
        response = Response(data, mimetype="application/octet-stream")
        response.headers["ETag"] = f'"{intent.expected_file_sha256}"'
        response.headers["X-WB-Source-Sha256"] = intent.expected_file_sha256
        response.headers["X-WB-Source-Byte-Length"] = str(len(data))
        response.headers["X-WB-Source-Encoding"] = "utf-8"
        response.headers["X-WB-Source-BOM"] = (
            "utf-8" if data.startswith(b"\xef\xbb\xbf") else "none"
        )
        return response
    except reimport.ReimportError as exc:
        return _error(exc)


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
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.reimport"):
            receipt = reimport.commit_reimport(
                store,
                document_id=document_id,
                intent_id=intent_id,
                actor=_actor(),
                replacement_snapshot=snapshot_part.read(),
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
    except InvariantViolation as exc:
        return _error(reimport.ReimportError("invalid_request", str(exc)))


@reimport_blueprint.delete("/api/truth/doc/<document_id>/reimport/<intent_id>")
def api_cancel_reimport(document_id: str, intent_id: str):
    try:
        _reject_read_only()
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.reimport"):
            receipt = reimport.cancel_reimport(
                _store(),
                document_id=document_id,
                intent_id=intent_id,
                actor=_actor(),
            )
        return jsonify(receipt)
    except reimport.ReimportError as exc:
        return _error(exc)


def register_reimport_routes(app):
    app.register_blueprint(reimport_blueprint)
    return app


__all__ = ["register_reimport_routes", "reimport_blueprint"]
