"""HTTP adapter for the Co-work Truth observability and management surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.cowork import lifecycle_lock, truth_surface
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.events import emit_truth_event
from work_buddy.truth.store import PostCommitHookError


truth_blueprint = Blueprint("cowork_truth_surface", __name__)


def _error(
    code: str,
    message: str,
    *,
    status: int = 400,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
):
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                    "retryable": retryable,
                },
            }
        ),
        status,
    )


def _surface_error(exc: truth_surface.TruthSurfaceError):
    return _error(
        exc.code,
        str(exc),
        status=exc.status,
        retryable=exc.retryable,
        details=exc.details,
    )


def _store_and_document(document_id: str):
    from work_buddy.cowork.api import _resolve_document, _resolve_store

    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return None, None, error
    document, error = _resolve_document(store, document_id)
    if error:
        return None, None, error
    if not document_surface_allowed(store, document):
        return (
            None,
            None,
            _error(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            ),
        )
    return store, document, None


def _actor() -> Actor:
    from work_buddy.cowork.api import dashboard_user_ref

    return Actor("human", dashboard_user_ref(request.headers))


def _read_only() -> bool:
    from work_buddy.cowork.api import _is_read_only

    return _is_read_only()


def _write_blocked():
    if _read_only():
        return _error(
            "read_only",
            "Co-work is read-only right now.",
            status=403,
        )
    return None


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, _error("invalid_body", "request body must be a JSON object")
    return body, None


def _invalid_optional_string(
    body: Mapping[str, Any],
    field: str,
    *,
    code: str,
    message: str,
):
    """Reject JSON containers before they reach text or SQLite boundaries."""

    value = body.get(field)
    if value is not None and not isinstance(value, str):
        return _error(code, message)
    return None


def _validate_new_claim_input(claim: Mapping[str, Any]):
    for field in ("proposition", "claim_kind"):
        value = claim.get(field)
        if not isinstance(value, str) or not value.strip():
            return _error(
                "invalid_claim",
                f"claim.{field} must be a nonempty string",
            )
    scope = claim.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        return _error(
            "invalid_claim",
            "claim.scope must be a nonempty string when supplied",
        )
    structured = claim.get("structured")
    if structured is not None and not isinstance(structured, (Mapping, str)):
        return _error(
            "invalid_claim",
            "claim.structured must be an object or JSON object string",
        )
    for field in ("valid_from", "valid_to"):
        invalid = _invalid_optional_string(
            claim,
            field,
            code="invalid_claim",
            message=f"claim.{field} must be a string when supplied",
        )
        if invalid:
            return invalid
    return None


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise truth_surface.TruthSurfaceError(
            "invalid_pagination", f"{name} must be an integer"
        ) from exc


def _emit_claim_event(
    event_type: str,
    *,
    store_id: str,
    claim_id: str,
    data: dict[str, Any],
) -> None:
    emit_truth_event(
        event_type,
        store_id=store_id,
        subject_kind="claim",
        subject_id=claim_id,
        data=data,
    )


@truth_blueprint.get("/api/truth/doc/<document_id>/truth")
def api_truth_list(document_id: str):
    store, document, error = _store_and_document(document_id)
    if error:
        return error
    try:
        payload = truth_surface.truth_list(
            store,
            document,
            view=request.args.get("view", "document"),
            filter_name=request.args.get("filter", "all"),
            offset=_int_arg("offset", 0),
            limit=_int_arg("limit", 100),
            read_only=_read_only(),
        )
    except truth_surface.TruthSurfaceError as exc:
        return _surface_error(exc)
    except InvariantViolation as exc:
        return _error("truth_unavailable", str(exc), status=409)
    return jsonify({"ok": True, **payload})


@truth_blueprint.get("/api/truth/doc/<document_id>/truth/claims/<claim_id>")
def api_truth_claim_detail(document_id: str, claim_id: str):
    store, document, error = _store_and_document(document_id)
    if error:
        return error
    try:
        payload = truth_surface.truth_claim_detail(
            store,
            document,
            claim_id,
            read_only=_read_only(),
        )
    except truth_surface.TruthSurfaceError as exc:
        return _surface_error(exc)
    except InvariantViolation as exc:
        return _error("truth_unavailable", str(exc), status=409)
    return jsonify({"ok": True, **payload})


def _connect(document_id: str, *, create: bool):
    blocked = _write_blocked()
    if blocked:
        return blocked
    store, document, error = _store_and_document(document_id)
    if error:
        return error
    body, error = _json_body()
    if error:
        return error
    assert body is not None
    if create:
        if not isinstance(body.get("claim"), Mapping):
            return _error("invalid_claim", "claim must be an object")
        invalid = _validate_new_claim_input(body["claim"])
        if invalid:
            return invalid
    else:
        claim_id = body.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            return _error("claim_id_required", "claim_id is required")
    if not isinstance(body.get("selector"), Mapping):
        return _error("invalid_selector", "selector must be an object")
    role = body.get("role")
    if not isinstance(role, str) or not role.strip():
        return _error("invalid_role", "role must be a nonempty string")
    expected_head = body.get("expected_structured_head_sha256")
    if not isinstance(expected_head, str) or not expected_head.strip():
        return _error(
            "expected_head_required",
            "expected_structured_head_sha256 is required",
        )
    expected_generation = body.get("expected_ydoc_generation_sha256")
    if expected_generation is not None and not isinstance(expected_generation, str):
        return _error(
            "invalid_generation",
            "expected_ydoc_generation_sha256 must be a string",
        )
    expected_projection = body.get("expected_projection_sha256")
    if not isinstance(expected_projection, str) or not expected_projection.strip():
        return _error(
            "expected_projection_required",
            "expected_projection_sha256 is required",
        )
    from work_buddy.consent import user_initiated

    try:
        with lifecycle_lock.document_lifecycle_lock(store.store_id, document.id):
            with ydoc_store.document_lock(
                store,
                document.id,
                path_key=documents.document_path_key(document.path),
            ):
                with user_initiated("dashboard.cowork.truth"):
                    result = truth_surface.connect_claim(
                        store,
                        document,
                        actor=_actor(),
                        selector_input=body.get("selector"),
                        role=body.get("role"),
                        expected_structured_head_sha256=expected_head.strip().lower(),
                        expected_projection_sha256=expected_projection.strip().lower(),
                        expected_ydoc_generation_sha256=(
                            None
                            if expected_generation is None
                            else expected_generation.strip().lower()
                        ),
                        claim_id=None if create else body.get("claim_id"),
                        claim_input=body.get("claim") if create else None,
                    )
    except truth_surface.TruthSurfaceError as exc:
        return _surface_error(exc)
    except InvariantViolation as exc:
        return _error("invalid_truth_connection", str(exc), status=400)

    if result.claim_created:
        _emit_claim_event(
            "truth.claim_proposed",
            store_id=store.store_id,
            claim_id=result.claim.id,
            data={"created": True, "document_id": document.id},
        )
    if result.expression_created:
        emit_truth_event(
            "truth.doc_expression_marked",
            store_id=store.store_id,
            subject_kind="expression",
            subject_id=result.expression_id,
            data={
                "document_id": document.id,
                "expression_id": result.expression_id,
                "claim_ref": result.claim.id,
            },
        )
    return (
        jsonify(
            {
                "ok": True,
                "claim_id": result.claim.id,
                "claim_created": result.claim_created,
                "canonical_sha256": result.claim.canonical_sha256,
                "span_id": result.span_id,
                "expression_id": result.expression_id,
                "expression_created": result.expression_created,
                "projection_sha256": result.projection_sha256,
                "structured_head_sha256": result.structured_head_sha256,
                "ydoc_generation_sha256": result.ydoc_generation_sha256,
            }
        ),
        201 if result.claim_created or result.expression_created else 200,
    )


@truth_blueprint.post("/api/truth/doc/<document_id>/truth/connections")
def api_truth_connect(document_id: str):
    return _connect(document_id, create=False)


@truth_blueprint.post("/api/truth/doc/<document_id>/truth/claims")
def api_truth_propose_and_connect(document_id: str):
    return _connect(document_id, create=True)


@truth_blueprint.post(
    "/api/truth/doc/<document_id>/truth/claims/<claim_id>/decisions"
)
def api_truth_claim_decision(document_id: str, claim_id: str):
    blocked = _write_blocked()
    if blocked:
        return blocked
    store, document, error = _store_and_document(document_id)
    if error:
        return error
    body, error = _json_body()
    if error:
        return error
    assert body is not None
    for field in ("action", "expected_canonical_sha256", "expected_context_sha256"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            return _error("missing_decision_field", f"{field} is required")
    invalid = _invalid_optional_string(
        body,
        "gesture_kind",
        code="invalid_gesture_kind",
        message="gesture_kind must be a string when supplied",
    )
    if invalid:
        return invalid
    if isinstance(body.get("gesture_kind"), str) and not body["gesture_kind"].strip():
        return _error(
            "invalid_gesture_kind",
            "gesture_kind must be nonempty when supplied",
        )
    invalid = _invalid_optional_string(
        body,
        "reason",
        code="invalid_redaction_reason",
        message="reason must be a string when supplied",
    )
    if invalid:
        return invalid
    from work_buddy.consent import user_initiated

    try:
        with lifecycle_lock.document_lifecycle_lock(store.store_id, document.id):
            with user_initiated("dashboard.cowork.truth"):
                result = truth_surface.decide_claim(
                    store,
                    document,
                    claim_id,
                    actor=_actor(),
                    action=body["action"],
                    expected_canonical_sha256=body["expected_canonical_sha256"],
                    expected_context_sha256=body["expected_context_sha256"],
                    gesture_kind=body.get("gesture_kind"),
                    reason=body.get("reason"),
                )
    except truth_surface.TruthSurfaceError as exc:
        return _surface_error(exc)
    except PostCommitHookError:
        # The authoritative Truth transaction has already committed. A
        # recovery-export or observer failure must not be reported as though
        # the user's decision was rejected, which would invite an unsafe retry
        # of a mutation that already happened.
        committed_action = str(body["action"]).strip().lower().replace("_", "-")
        event_type = {
            "confirm": "truth.claim_confirmed",
            "reaffirm": "truth.claim_confirmed",
            "reject": "truth.claim_rejected",
            "redact": "truth.claim_redacted",
        }.get(committed_action)
        if event_type is not None:
            _emit_claim_event(
                event_type,
                store_id=store.store_id,
                claim_id=claim_id,
                data={"action": committed_action, "document_id": document.id},
            )
        return (
            jsonify(
                {
                    "ok": True,
                    "action": committed_action,
                    "claim_id": claim_id,
                    "status": "committed_with_recovery_warning",
                    "warning": {
                        "code": "post_commit_recovery_failed",
                        "message": (
                            "Your decision was saved, but some background "
                            "recovery work still needs attention."
                        ),
                        "retryable": False,
                    },
                }
            ),
            202,
        )
    except InvariantViolation as exc:
        return _error("decision_rejected", str(exc), status=409)

    event_type = {
        "confirm": "truth.claim_confirmed",
        "reaffirm": "truth.claim_confirmed",
        "reject": "truth.claim_rejected",
        "redact": "truth.claim_redacted",
    }[result["action"]]
    _emit_claim_event(
        event_type,
        store_id=store.store_id,
        claim_id=claim_id,
        data={"action": result["action"], "document_id": document.id},
    )
    return jsonify({"ok": True, **result})


@truth_blueprint.post(
    "/api/truth/doc/<document_id>/truth/claims/<claim_id>/challenges"
)
def api_truth_claim_challenge(document_id: str, claim_id: str):
    blocked = _write_blocked()
    if blocked:
        return blocked
    store, document, error = _store_and_document(document_id)
    if error:
        return error
    body, error = _json_body()
    if error:
        return error
    assert body is not None
    required = (
        "challenging_claim_id",
        "expected_canonical_sha256",
        "expected_challenger_sha256",
    )
    for field in required:
        if not isinstance(body.get(field), str) or not body[field].strip():
            return _error("missing_challenge_field", f"{field} is required")
    invalid = _invalid_optional_string(
        body,
        "note",
        code="invalid_note",
        message="note must be a string when supplied",
    )
    if invalid:
        return invalid
    from work_buddy.consent import user_initiated

    try:
        with lifecycle_lock.document_lifecycle_lock(store.store_id, document.id):
            with user_initiated("dashboard.cowork.truth"):
                result = truth_surface.challenge_claim(
                    store,
                    document,
                    claim_id,
                    actor=_actor(),
                    challenging_claim_id=body["challenging_claim_id"],
                    expected_canonical_sha256=body["expected_canonical_sha256"],
                    expected_challenger_sha256=body["expected_challenger_sha256"],
                    note=body.get("note"),
                )
    except truth_surface.TruthSurfaceError as exc:
        return _surface_error(exc)
    except InvariantViolation as exc:
        return _error("challenge_rejected", str(exc), status=409)
    _emit_claim_event(
        "truth.claim_challenged",
        store_id=store.store_id,
        claim_id=claim_id,
        data={
            "challenging_claim_id": body["challenging_claim_id"],
            "document_id": document.id,
        },
    )
    return jsonify({"ok": True, **result})


__all__ = ["truth_blueprint"]
