"""Flask adapter for dual-CAS Co-work projection publication."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from work_buddy.cowork import materialization
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_text


materialization_blueprint = Blueprint("cowork_materialization", __name__)


def _registry():
    # Share the parent HTTP surface seam so configured deployments and tests
    # resolve the same machine registry as every other Co-work route.
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _store():
    store_id = (request.args.get("store_id") or "").strip()
    if not store_id:
        raise materialization.MaterializationError(
            "store_id_required", "store_id is required"
        )
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise materialization.MaterializationError(
            "store_unreachable", "That folder is not reachable by Co-work.", status=404
        ) from exc


def _error(exc: materialization.MaterializationError):
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


@materialization_blueprint.post(
    "/api/truth/doc/<document_id>/materialize"
)
def api_materialize(document_id: str):
    try:
        from work_buddy.cowork.api import _is_read_only

        if _is_read_only():
            raise materialization.MaterializationError(
                "read_only", "Dashboard is in read-only mode", status=403
            )
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise materialization.MaterializationError(
                "invalid_request", "request body must be a JSON object"
            )
        store = _store()
        from work_buddy.cowork.api import _require_human_action

        _authority, actor = _require_human_action(
            operation="document.materialize",
            store_id=store.store_id,
            document_id=document_id,
            body=body,
        )
        result = materialization.publish_projection(
            store,
            document_id=document_id,
            rendered_markdown=body.get("rendered_markdown"),
            rendered_sha256=str(body.get("rendered_sha256") or ""),
            expected_file_sha256=str(body.get("expected_file_sha256") or ""),
            expected_structured_head_sha256=str(
                body.get("expected_ydoc_head_sha256")
                or body.get("expected_structured_head_sha256")
                or ""
            ),
            snapshot_sha256=str(body.get("snapshot_sha256") or ""),
            actor=actor,
            idempotency_key=body.get("idempotency_key"),
        )
        try:
            from work_buddy.cowork.api import _emit

            _emit(
                "truth.doc_materialized",
                request.args.get("store_id") or "",
                {
                    "document_id": document_id,
                    "file_sha256": result["new_file_sha256"],
                    "document_version_id": result["document_version_id"],
                },
                event_id=sha256_text(
                    "cowork-materialization:"
                    f"{store.store_id}:{result['materialization_intent_id']}"
                ),
            )
        except Exception:  # noqa: BLE001 - ledger/file commit is authoritative
            pass
        return jsonify(result)
    except materialization.MaterializationError as exc:
        return _error(exc)
    except LocalIdentityError as exc:
        from work_buddy.cowork.api import _local_identity_error

        return _local_identity_error(exc)
    except InvariantViolation as exc:
        return _error(
            materialization.MaterializationError("invalid_request", str(exc))
        )


def register_materialization_routes(app):
    app.register_blueprint(materialization_blueprint)
    return app


__all__ = ["materialization_blueprint", "register_materialization_routes"]
