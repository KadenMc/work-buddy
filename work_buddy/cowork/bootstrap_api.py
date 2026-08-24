"""Flask adapter for the Co-work two-phase bootstrap service."""

from __future__ import annotations

import json
import hashlib

from flask import Blueprint, Response, jsonify, request

from work_buddy.cowork import bootstrap, provenance
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.identity import sha256_text


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


def _human_actor(*, operation: str, store_id: str, document_id: str, body: dict):
    from work_buddy.cowork.api import _require_human_action

    return _require_human_action(
        operation=operation,
        store_id=store_id,
        document_id=document_id,
        body=body,
    )[1]


def _identity_error(exc: LocalIdentityError):
    from work_buddy.cowork.api import _local_identity_error

    return _local_identity_error(exc)


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
        source = (
            None
            if uploaded is None
            else uploaded.read(bootstrap.maximum_source_upload_bytes() + 1)
        )
        store = _store()
        authority_body = {
            "metadata": metadata,
            "source_sha256": (
                None if source is None else hashlib.sha256(source).hexdigest()
            ),
            "source_byte_length": 0 if source is None else len(source),
        }
        document_ref = str(
            metadata.get("document_id")
            or f"bootstrap:{metadata.get('idempotency_key') or 'new'}"
        )
        intent, created = bootstrap.prepare_bootstrap(
            store,
            metadata=metadata,
            source=source,
            actor=_human_actor(
                operation="bootstrap.prepare",
                store_id=store.store_id,
                document_id=document_ref,
                body=authority_body,
            ),
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
        if intent.mode == "import":
            payload["importer"] = bootstrap.importer_descriptor(intent)
        if intent.state == "committed" and intent.receipt is not None:
            payload["result"] = intent.receipt
        return jsonify(payload), 201 if created else 200
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        return _identity_error(exc)
    except InvariantViolation as exc:
        return _error(bootstrap.BootstrapError("invalid_request", str(exc)))


@bootstrap_blueprint.post("/api/truth/doc/bootstrap/<bootstrap_id>/source")
def api_bootstrap_source(bootstrap_id: str):
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise bootstrap.BootstrapError(
                "invalid_request",
                "bootstrap source read requires a JSON object",
            )
        if body.get("bootstrap_id") != bootstrap_id:
            raise bootstrap.BootstrapError(
                "bootstrap_id_mismatch",
                "bootstrap_id must match the route target",
            )
        store = _store()
        actor = _human_actor(
            operation="bootstrap.source_read",
            store_id=store.store_id,
            document_id=bootstrap_id,
            body=body,
        )
        intent, payload = bootstrap.read_staged_source(
            store, bootstrap_id=bootstrap_id, actor=actor
        )
        media_type = intent.source_media_type or "text/markdown"
        response = Response(payload, mimetype=media_type)
        response.headers["ETag"] = f'"{intent.source_sha256}"'
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-WB-Source-Sha256"] = intent.source_sha256
        response.headers["X-WB-Source-Byte-Length"] = str(len(payload))
        if intent.importer_id is not None:
            response.headers["X-WB-Importer-Id"] = intent.importer_id
        if media_type == "text/markdown":
            response.headers["X-WB-Encoding"] = "utf-8"
            response.headers["X-WB-BOM"] = (
                "utf-8" if payload.startswith(b"\xef\xbb\xbf") else "none"
            )
        return response
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        return _identity_error(exc)


@bootstrap_blueprint.put("/api/truth/doc/bootstrap/<bootstrap_id>")
def api_bootstrap_commit(bootstrap_id: str):
    try:
        _require_writable()
        if (request.mimetype or "").startswith("multipart/form-data"):
            metadata = _metadata_part()
            snapshot_upload = request.files.get("snapshot")
            projection_upload = request.files.get("projection")
            if snapshot_upload is None or projection_upload is None:
                raise bootstrap.BootstrapError(
                    "invalid_request",
                    "bootstrap commit requires snapshot and projection parts",
                )
            snapshot = snapshot_upload.read(bootstrap.MAX_SNAPSHOT_BYTES + 1)
            projection = projection_upload.read(
                bootstrap.MAX_CANONICAL_PROJECTION_BYTES + 1
            )
            source_sha256 = str(metadata.get("source_sha256") or "")
            snapshot_sha256 = str(metadata.get("snapshot_sha256") or "")
            projection_sha256 = str(metadata.get("projection_sha256") or "")
            ydoc_schema = str(metadata.get("ydoc_schema") or "")
        elif request.mimetype == "application/octet-stream":
            # Backward-compatible strict-fidelity client. Its source bytes are
            # also its projection bytes.
            snapshot = request.stream.read(bootstrap.MAX_SNAPSHOT_BYTES + 1)
            projection = None
            source_sha256 = request.headers.get("X-WB-Source-Sha256") or ""
            snapshot_sha256 = request.headers.get("X-WB-Snapshot-Sha256") or ""
            projection_sha256 = None
            ydoc_schema = request.headers.get("X-WB-Ydoc-Schema") or ""
        else:
            raise bootstrap.BootstrapError(
                "unsupported_media_type",
                "bootstrap commit requires multipart/form-data",
                status=415,
            )
        store = _store()
        pending = bootstrap.get_intent(store, bootstrap_id)
        authority_body = {
            "bootstrap_id": bootstrap_id,
            "metadata": metadata if (request.mimetype or "").startswith("multipart/form-data") else {
                "source_sha256": source_sha256,
                "snapshot_sha256": snapshot_sha256,
                "ydoc_schema": ydoc_schema,
            },
            "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
            "projection_sha256": (
                None if projection is None else hashlib.sha256(projection).hexdigest()
            ),
        }
        receipt = bootstrap.commit_bootstrap(
            store,
            bootstrap_id=bootstrap_id,
            snapshot=snapshot,
            projection=projection,
            source_sha256=source_sha256,
            snapshot_sha256=snapshot_sha256,
            projection_sha256=projection_sha256,
            ydoc_schema=ydoc_schema,
            actor=_human_actor(
                operation="bootstrap.commit",
                store_id=store.store_id,
                document_id=pending.document_id,
                body=authority_body,
            ),
        )
        attestation_id = receipt.get("authorship_attestation_id")
        if (
            receipt.get("mode") == "import"
            and isinstance(attestation_id, str)
            and attestation_id
        ):
            emit_truth_event(
                "truth.doc_provenance_attested",
                store_id=store.store_id,
                event_id=sha256_text(
                    f"cowork-file-import-provenance:{attestation_id}"
                ),
                data={
                    "document_id": receipt["document_id"],
                    "attestation_id": attestation_id,
                    "document_version_id": receipt["document_version_id"],
                    "target_structured_head_sha256": receipt[
                        "structured_head_sha256"
                    ],
                    "basis_kind": "user_attestation",
                },
            )
        return jsonify(receipt)
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        return _identity_error(exc)
    except provenance.ProvenanceConflictError as exc:
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
    except InvariantViolation as exc:
        return _error(bootstrap.BootstrapError("invalid_request", str(exc)))


@bootstrap_blueprint.delete("/api/truth/doc/bootstrap/<bootstrap_id>")
def api_bootstrap_cancel(bootstrap_id: str):
    try:
        _require_writable()
        store = _store()
        cancelled = bootstrap.cancel_bootstrap(
            store,
            bootstrap_id=bootstrap_id,
            actor=_human_actor(
                operation="bootstrap.cancel",
                store_id=store.store_id,
                document_id=bootstrap_id,
                body={"bootstrap_id": bootstrap_id},
            ),
        )
        return jsonify({"ok": True, "cancelled": cancelled})
    except bootstrap.BootstrapError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        return _identity_error(exc)


def register_bootstrap_routes(app):
    app.register_blueprint(bootstrap_blueprint)
    return app


__all__ = ["bootstrap_blueprint", "register_bootstrap_routes"]
