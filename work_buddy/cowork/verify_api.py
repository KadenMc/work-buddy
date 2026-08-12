"""Dashboard HTTP adapter for exact Co-work Verify and Co-think actions."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.consent import user_initiated
from work_buddy.cowork.api import (
    _document_surface_or_403,
    _emit,
    _fail,
    _local_identity_error,
    _reject_read_only,
    _require_human_action,
    _resolve_document,
    _resolve_store,
)
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CothinkItem,
    record_cothink_item_status,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_orchestration import (
    VerifyOrchestrationError,
    affirm_verify_recheck_target,
    run_status_projection,
    start_cothink,
    start_verify_run,
)
from work_buddy.cowork.verify_inspection import verify_run_detail
from work_buddy.cowork.verify_configuration import (
    create_user_criterion_draft,
    create_user_verification_check,
    list_effective_verification_configuration,
    set_document_criterion_enabled,
)
from work_buddy.truth import documents
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.security.local_identity import LocalIdentityError


verify_blueprint = Blueprint("cowork_verify", __name__)
logger = logging.getLogger(__name__)


def _body() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise VerifyOrchestrationError("request body must be a JSON object")
    return dict(value)


def _selection(body: Mapping[str, Any]) -> AgentExecutionSelection:
    value = body.get("execution")
    if not isinstance(value, Mapping):
        raise VerifyOrchestrationError(
            "execution must name an explicit provider_id and model_id"
        )
    provider_id = value.get("provider_id")
    model_id = value.get("model_id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise VerifyOrchestrationError("execution.provider_id is required")
    if not isinstance(model_id, str) or not model_id.strip():
        raise VerifyOrchestrationError("execution.model_id is required")
    return AgentExecutionSelection(
        provider_id=provider_id,
        model_id=model_id,
        provider_label=str(value.get("provider_label") or ""),
        model_label=str(value.get("model_label") or ""),
    )


def _mutation_context(document_id: str):
    blocked = _reject_read_only()
    if blocked:
        return None, None, blocked
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return None, None, error
    gate = _document_surface_or_403(store)
    if gate:
        return None, None, gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return None, None, doc_error
    if documents.current_lifecycle(store, document.id) != "active":
        return None, None, _fail(
            "This action cannot start on a retired document.",
            409,
        )
    if not document_surface_allowed(store, document):
        return None, None, _fail(
            "This document is not available in Co-work for this folder.",
            403,
        )
    return store, document, None


def _safe_error(exc: Exception):
    if isinstance(exc, LocalIdentityError):
        return _local_identity_error(exc)
    if isinstance(exc, (VerifyOrchestrationError, InvariantViolation, ValueError)):
        return _fail(str(exc), 409)
    return _fail("Co-work could not start this exact action.", 500)


@verify_blueprint.post(
    "/api/truth/doc/<document_id>/verify/recheck-target-affirmations"
)
def api_affirm_verify_recheck_target(document_id: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        capture = body.get("capture")
        if not isinstance(capture, Mapping):
            raise VerifyOrchestrationError("capture is required")
        _authority, actor = _require_human_action(
            operation="verify.recheck_target_affirm",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.verify_target_affirmation"):
            result = affirm_verify_recheck_target(
                store,
                document_id=document.id,
                capture=capture,
                actor=actor,
                recheck_intent_id=str(body.get("recheck_intent_id") or ""),
                source_run_id=str(body.get("source_run_id") or ""),
                proposal_ids=body.get("proposal_ids", ()),
                user_goal=str(body.get("user_goal") or ""),
                protected_intent=str(body.get("protected_intent") or ""),
            )
    except Exception as exc:  # noqa: BLE001 - safe adapter projection below
        return _safe_error(exc)
    _emit(
        "truth.doc_verify_recheck_target_affirmed",
        store.store_id,
        {
            "document_id": document.id,
            "recheck_intent_id": result["recheck_intent_id"],
            "action_snapshot_id": result["affirmed_action_snapshot_id"],
        },
        event_id=(
            "cowork-verify-target-affirmation:"
            f"{result['affirmed_action_snapshot_id']}"
        ),
    )
    return jsonify(result), 201


@verify_blueprint.post("/api/truth/doc/<document_id>/verify/runs")
def api_start_verify_run(document_id: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        capture = body.get("capture")
        if not isinstance(capture, Mapping):
            raise VerifyOrchestrationError("capture is required")
        _authority, actor = _require_human_action(
            operation="verify.run",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.verify_run"):
            result = start_verify_run(
                store,
                document_id=document.id,
                capture=capture,
                selection=_selection(body),
                actor=actor,
                user_goal=str(body.get("user_goal") or ""),
                protected_intent=str(body.get("protected_intent") or ""),
                recheck_of_proposal_ids=body.get(
                    "recheck_of_proposal_ids",
                    (),
                ),
                recheck_of_run_id=body.get("recheck_of_run_id"),
                recheck_intent_id=body.get("recheck_intent_id"),
                recheck_target_confirmation=body.get(
                    "recheck_target_confirmation"
                ),
            )
    except Exception as exc:  # noqa: BLE001 - safe adapter projection below
        return _safe_error(exc)
    _emit(
        "truth.doc_verify_run_started",
        store.store_id,
        {
            "document_id": document.id,
            "run_id": result["run_id"],
            "action_snapshot_id": result["action_snapshot_id"],
        },
        event_id=f"cowork-verify-run:{result['run_id']}",
    )
    return jsonify(result), 202


@verify_blueprint.get(
    "/api/truth/doc/<document_id>/verify/runs/<run_id>"
)
def api_verify_run_get(document_id: str, run_id: str):
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    summary = next(
        (
            item
            for item in run_status_projection(
                store,
                document_id=document.id,
            )
            if item["run_id"] == run_id
        ),
        None,
    )
    if summary is None:
        return _fail("Verify run does not exist for this document.", 404)
    return jsonify(
        {
            "ok": True,
            "run": {
                key: value
                for key, value in summary.items()
                if not key.startswith("_")
            },
            "detail": verify_run_detail(
                store,
                document_id=document.id,
                run_id=run_id,
            ),
        }
    )


@verify_blueprint.get(
    "/api/truth/doc/<document_id>/verify/configuration"
)
def api_verify_configuration_get(document_id: str):
    store, error = _resolve_store(request.args.get("store_id"))
    if error:
        return error
    gate = _document_surface_or_403(store)
    if gate:
        return gate
    document, doc_error = _resolve_document(store, document_id)
    if doc_error:
        return doc_error
    try:
        configuration = list_effective_verification_configuration(
            store,
            document_id=document.id,
            ensure_system_defaults=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    return jsonify({"ok": True, "configuration": configuration})


@verify_blueprint.patch(
    "/api/truth/doc/<document_id>/verify/criteria/<criterion_key>"
)
def api_verify_criterion_update(document_id: str, criterion_key: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        if not isinstance(body.get("enabled"), bool):
            raise VerifyOrchestrationError("enabled must be a boolean")
        expected_activation_id = body.get("expected_activation_id")
        if expected_activation_id is not None and not isinstance(
            expected_activation_id,
            str,
        ):
            raise VerifyOrchestrationError(
                "expected_activation_id must be a string or null"
            )
        _authority, actor = _require_human_action(
            operation="verify.criterion_update",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.verify_configuration"):
            result = set_document_criterion_enabled(
                store,
                document_id=document.id,
                criterion_key=criterion_key,
                enabled=body["enabled"],
                actor=actor,
                expected_activation_id=expected_activation_id,
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    if result["changed"]:
        _emit(
            "truth.doc_verify_configuration_changed",
            store.store_id,
            {
                "document_id": document.id,
                "criterion_key": criterion_key,
                "activation_id": result["activation_id"],
                "enabled": body["enabled"],
            },
            event_id=f"cowork-verify-activation:{result['activation_id']}",
        )
    return jsonify({"ok": True, **result})


@verify_blueprint.post(
    "/api/truth/doc/<document_id>/verify/criteria/drafts"
)
def api_verify_criterion_draft_create(document_id: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        limitations = body.get("limitations", [])
        if not isinstance(limitations, list) or not all(
            isinstance(item, str) for item in limitations
        ):
            raise VerifyOrchestrationError(
                "limitations must be an array of strings"
            )
        _authority, actor = _require_human_action(
            operation="verify.criterion_draft_create",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.verify_criterion_draft"):
            result = create_user_criterion_draft(
                store,
                document_id=document.id,
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                evaluation_instructions=str(
                    body.get("evaluation_instructions") or ""
                ),
                limitations=limitations,
                actor=actor,
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    _emit(
        "truth.doc_verify_configuration_changed",
        store.store_id,
        {
            "document_id": document.id,
            "criterion_key": result["criterion_key"],
            "change": "user_criterion_draft_created",
        },
        event_id=(
            "cowork-verify-criterion-draft:"
            f"{document.id}:{result['criterion_key']}"
        ),
    )
    return jsonify({"ok": True, **result}), 201


@verify_blueprint.post("/api/truth/doc/<document_id>/verify/checks")
def api_verify_check_create(document_id: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        limitations = body.get("limitations", [])
        if not isinstance(limitations, list) or not all(
            isinstance(item, str) for item in limitations
        ):
            raise VerifyOrchestrationError(
                "limitations must be an array of strings"
            )
        _authority, actor = _require_human_action(
            operation="verify.check_create",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.verify_check"):
            result = create_user_verification_check(
                store,
                document_id=document.id,
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                evaluation_instructions=str(
                    body.get("evaluation_instructions") or ""
                ),
                limitations=limitations,
                actor=actor,
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    if result["created"]:
        _emit(
            "truth.doc_verify_configuration_changed",
            store.store_id,
            {
                "document_id": document.id,
                "criterion_key": result["criterion_key"],
                "change": "user_verification_check_created",
            },
            event_id=(
                "cowork-verify-check:"
                f"{document.id}:{result['criterion_key']}"
            ),
        )
    status_code = 201 if result["created"] else 200
    return jsonify({"ok": True, **result}), status_code


@verify_blueprint.post("/api/truth/doc/<document_id>/cothink")
def api_start_cothink(document_id: str):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        capture = body.get("capture")
        if not isinstance(capture, Mapping):
            raise VerifyOrchestrationError("capture is required")
        _authority, actor = _require_human_action(
            operation="cothink.run",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        with user_initiated("dashboard.cowork.cothink"):
            result = start_cothink(
                store,
                document_id=document.id,
                capture=capture,
                selection=_selection(body),
                actor=actor,
                purpose=str(
                    body.get("purpose")
                    or "Invite one useful alternative perspective."
                ),
                protected_intent=str(
                    body.get("protected_intent")
                    or "Support deliberation without presenting a defect claim."
                ),
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    _emit(
        "truth.doc_cothink_started",
        store.store_id,
        {
            "document_id": document.id,
            "action_snapshot_id": result["action_snapshot_id"],
            "job_id": result["job_id"],
        },
        event_id=f"cowork-cothink:{result['job_id']}",
    )
    if result.get("status") == "unavailable":
        _emit(
            "truth.doc_cothink_outcome_recorded",
            store.store_id,
            {
                "document_id": document.id,
                "action_snapshot_id": result["action_snapshot_id"],
                "job_id": result["job_id"],
                "outcome": "unavailable",
            },
            event_id=f"cowork-cothink-outcome:{result['job_id']}:unavailable",
        )
    return jsonify(result), 202


@verify_blueprint.post(
    "/api/truth/doc/<document_id>/cothink/items/<item_id>/actions"
)
def api_cothink_item_action(
    document_id: str,
    item_id: str,
):
    store, document, error = _mutation_context(document_id)
    if error:
        return error
    try:
        body = _body()
        action = body.get("action")
        if action not in {"discuss", "park", "dismiss"}:
            raise VerifyOrchestrationError(
                "action must be discuss, park, or dismiss"
            )
        if action == "discuss":
            # Fail before consuming the one-use human gesture.  The lower
            # persistence boundary checks again immediately before mutation.
            from work_buddy.backups.source_foundation_restore import (
                require_source_foundation_writable,
            )

            require_source_foundation_writable(
                "dashboard.cowork.cothink_discussion"
            )
        authority, actor = _require_human_action(
            operation="cothink.item_action",
            store_id=store.store_id,
            document_id=document.id,
            body=body,
        )
        canonical_sha256 = body.get("canonical_sha256")
        if not isinstance(canonical_sha256, str):
            raise VerifyOrchestrationError("canonical_sha256 is required")
        item = verify_store.get_record(store, CothinkItem, item_id)
        if item is None or item.canonical_sha256 != canonical_sha256:
            raise VerifyOrchestrationError(
                "Co-think item changed or does not exist"
            )
        snapshot = verify_store.get_record(
            store,
            ActionSnapshot,
            item.action_snapshot_id,
        )
        if snapshot is None or snapshot.document_id != document.id:
            raise VerifyOrchestrationError(
                "Co-think item does not belong to this document"
            )
        if action == "discuss":
            from work_buddy.cowork.chat_targets import (
                post_cothink_discussion_message,
            )
            from work_buddy.cowork.document_agent import (
                ensure_bound_document_agent,
            )

            with user_initiated("dashboard.cowork.cothink_item_discuss"):
                conversation_id, message = post_cothink_discussion_message(
                    store_id=store.store_id,
                    document_id=document.id,
                    item_id=item.id,
                    canonical_sha256=item.canonical_sha256,
                    ingress=authority.to_input_ingress(),
                )
                try:
                    # The discussion turn is already durable and its original
                    # lifecycle guard has been released. Reacquire through the
                    # shared bound-conversation wake path; startup is a
                    # recoverable consequence, never part of send success.
                    ensure_bound_document_agent(conversation_id)
                except Exception:
                    logger.exception(
                        "Document-agent wake failed after Co-think discussion "
                        "persisted: conversation=%s item=%s",
                        conversation_id,
                        item.id,
                    )
            return jsonify(
                {
                    "ok": True,
                    "item_id": item.id,
                    "status": "discussing",
                    "conversation_id": conversation_id,
                    "message_id": message.message_id,
                    "context": message.context,
                }
            )
        with user_initiated("dashboard.cowork.cothink_item_action"):
            status = record_cothink_item_status(
                store,
                cothink_item_id=item.id,
                status="parked" if action == "park" else "dismissed",
                actor=actor,
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    _emit(
        "truth.doc_cothink_item_status_changed",
        store.store_id,
        {
            "document_id": document.id,
            "item_id": item.id,
            "status": status.status,
            "status_event_id": status.id,
        },
        event_id=f"cowork-cothink-status:{status.id}",
    )
    return jsonify(
        {
            "ok": True,
            "item_id": item.id,
            "status": status.status,
            "status_event_id": status.id,
        }
    )


def register_verify_routes(app):
    app.register_blueprint(verify_blueprint)


__all__ = ["register_verify_routes", "verify_blueprint"]
