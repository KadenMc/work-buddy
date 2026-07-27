"""Flask adapter for the Co-work two-phase bootstrap service."""

from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import bootstrap
from work_buddy.truth.contracts import Actor, InvariantViolation


bootstrap_blueprint = Blueprint("cowork_bootstrap", __name__)


def _registry():
    # Share the parent HTTP surface seam so configured deployments and tests
    # resolve the same machine registry as every other Co-work route.
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _error(exc: bootstrap.BootstrapError):
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


def _store():
    store_id = (request.args.get("store_id") or "").strip()
    if not store_id:
        raise bootstrap.BootstrapError("store_id_required", "store_id is required")
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise bootstrap.BootstrapError(
            "store_unreachable", "That folder is not reachable by Co-work.", status=404
        ) from exc


def _actor() -> Actor:
    from work_buddy.cowork.api import dashboard_user_ref

    return Actor("human", dashboard_user_ref(request.headers))


def _require_writable() -> None:
    from work_buddy.cowork.api import _is_read_only

    if _is_read_only():
        raise bootstrap.BootstrapError(
            "read_only", "Dashboard is in read-only mode", status=403
        )


def _metadata_part() -> dict:
    raw: str | bytes | None
    uploaded = request.files.get("metadata")
    if uploaded is not None:
        raw = uploaded.read()
    else:
        raw = request.form.get("metadata")
    if raw is None:
        raise bootstrap.BootstrapError("metadata_required", "multipart metadata is required")
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise bootstrap.BootstrapError("invalid_metadata", "metadata must be a JSON object") from exc
    if not isinstance(value, dict):
        raise bootstrap.BootstrapError("invalid_metadata", "metadata must be a JSON object")
    return value


@bootstrap_blueprint.post("/api/truth/doc/bootstrap")
def api_bootstrap_prepare():
    try:
        _require_writable()
        if not (request.mimetype or "").startswith("multipart/form-data"):
            raise bootstrap.BootstrapError(
                "unsupported_media_type",
                "bootstrap prepare requires multipart/form-data",
                status=415,
            )
        metadata = _metadata_part()
        uploaded = request.files.get("source")
        source = None if uploaded is None else uploaded.read()
        intent, created = bootstrap.prepare_bootstrap(
            _store(), metadata=metadata, source=source, actor=_actor()
        )
        payload = {
            "ok": True,
            "bootstrap_id": intent.id,
            "document_id": intent.document_id,
            "mode": intent.mode,
            "normalized_path": intent.normalized_path,
            "source_sha256": intent.source_sha256,
            "source_byte_length": intent.source_byte_length,
            "source_url": (
                f"/api/truth/doc/bootstrap/{intent.id}/source"
                f"?store_id={request.args.get('store_id')}"
            ),
            "ydoc_schema": bootstrap.YDOC_SCHEMA,
            "file_precondition": (
                "must_not_exist" if intent.mode == "create" else intent.source_sha256
            ),
            "expires_at": intent.expires_at,
            "state": intent.state,
        }
        if intent.state == "committed" and intent.receipt is not None:
            payload["result"] = intent.receipt
        return jsonify(payload), 201 if created else 200
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except InvariantViolation as exc:
        return _error(bootstrap.BootstrapError("invalid_request", str(exc)))


@bootstrap_blueprint.get("/api/truth/doc/bootstrap/<bootstrap_id>/source")
def api_bootstrap_source(bootstrap_id: str):
    try:
        intent, payload = bootstrap.read_staged_source(
            _store(), bootstrap_id=bootstrap_id, actor=_actor()
        )
        response = Response(payload, mimetype="application/octet-stream")
        response.headers["ETag"] = f'"{intent.source_sha256}"'
        response.headers["X-WB-Source-Sha256"] = intent.source_sha256
        response.headers["X-WB-Source-Byte-Length"] = str(len(payload))
        response.headers["X-WB-Encoding"] = "utf-8"
        response.headers["X-WB-BOM"] = (
            "utf-8" if payload.startswith(b"\xef\xbb\xbf") else "none"
        )
        return response
    except bootstrap.BootstrapError as exc:
        return _error(exc)


@bootstrap_blueprint.put("/api/truth/doc/bootstrap/<bootstrap_id>")
def api_bootstrap_commit(bootstrap_id: str):
    try:
        _require_writable()
        if request.mimetype != "application/octet-stream":
            raise bootstrap.BootstrapError(
                "unsupported_media_type",
                "bootstrap commit requires application/octet-stream",
                status=415,
            )
        receipt = bootstrap.commit_bootstrap(
            _store(),
            bootstrap_id=bootstrap_id,
            snapshot=request.get_data(cache=False),
            source_sha256=request.headers.get("X-WB-Source-Sha256") or "",
            snapshot_sha256=request.headers.get("X-WB-Snapshot-Sha256") or "",
            ydoc_schema=request.headers.get("X-WB-Ydoc-Schema") or "",
            actor=_actor(),
        )
        return jsonify(receipt)
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except InvariantViolation as exc:
        return _error(bootstrap.BootstrapError("invalid_request", str(exc)))


@bootstrap_blueprint.delete("/api/truth/doc/bootstrap/<bootstrap_id>")
def api_bootstrap_cancel(bootstrap_id: str):
    try:
        _require_writable()
        cancelled = bootstrap.cancel_bootstrap(
            _store(), bootstrap_id=bootstrap_id, actor=_actor()
        )
        return jsonify({"ok": True, "cancelled": cancelled})
    except bootstrap.BootstrapError as exc:
        return _error(exc)


def register_bootstrap_routes(app):
    app.register_blueprint(bootstrap_blueprint)
    return app


__all__ = ["bootstrap_blueprint", "register_bootstrap_routes"]
