"""HTTP adapter for prepared document removal from Co-work."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from work_buddy.cowork import retirement
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_text


retirement_blueprint = Blueprint("cowork_retirement_lifecycle", __name__)


def _registry():
    from work_buddy.cowork.api import _registry as parent_registry

    return parent_registry()


def _store():
    store_id = str(request.args.get("store_id") or "").strip()
    if not store_id:
        raise retirement.RetirementError(
            "store_id_required", "A folder selection is required."
        )
    try:
        return _registry().open_store(store_id)
    except Exception as exc:  # noqa: BLE001
        raise retirement.RetirementError(
            "folder_unreachable", "The selected folder is not reachable.", status=404
        ) from exc


def _actor() -> Actor:
    from work_buddy.cowork.api import dashboard_user_ref

    return Actor("human", dashboard_user_ref(request.headers))


def _error(exc: retirement.RetirementError):
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


@retirement_blueprint.post("/api/truth/doc/<document_id>/retire")
def api_retire_document(document_id: str):
    try:
        from work_buddy.cowork.api import _is_read_only

        if _is_read_only():
            raise retirement.RetirementError(
                "read_only", "Co-work is view-only right now.", status=403
            )
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise retirement.RetirementError(
                "invalid_request", "Request body must be a JSON object."
            )
        store = _store()
        actor = _actor()
        from work_buddy.consent import user_initiated

        with user_initiated("dashboard.cowork.retire"):
            intent_id = str(body.get("intent_id") or "").strip()
            if intent_id:
                receipt = retirement.commit_retirement(
                    store,
                    document_id=document_id,
                    intent_id=intent_id,
                    actor=actor,
                )
                from work_buddy.cowork.api import _emit

                _emit(
                    "truth.doc_retired",
                    store.store_id,
                    {"document_id": document_id, "doc_event_id": receipt["doc_event_id"]},
                    event_id=sha256_text(f"cowork-retirement:{intent_id}"),
                )
                return jsonify(receipt)
            intent, created = retirement.prepare_retirement(
                store,
                document_id=document_id,
                actor=actor,
                idempotency_key=body.get("idempotency_key"),
            )
        payload = {
            "ok": True,
            "state": "confirmation_required",
            "intent_id": intent.id,
            "expires_at": intent.expires_at,
            "document_id": document_id,
            "expected_file_sha256": intent.expected_file_sha256,
            "expected_projection_sha256": intent.expected_projection_sha256,
            "expected_snapshot_sha256": intent.expected_snapshot_sha256,
            "expected_structured_head_sha256": intent.expected_structured_head_sha256,
            "consequence": retirement.CONSEQUENCE,
            "consequence_sha256": intent.consequence_sha256,
        }
        if intent.state == "committed" and intent.receipt is not None:
            payload["state"] = "committed"
            payload["result"] = intent.receipt
        return jsonify(payload), 201 if created else 200
    except retirement.RetirementError as exc:
        return _error(exc)
    except InvariantViolation as exc:
        return _error(retirement.RetirementError("invalid_request", str(exc)))


def register_retirement_routes(app):
    app.register_blueprint(retirement_blueprint)
    return app


__all__ = ["register_retirement_routes", "retirement_blueprint"]
