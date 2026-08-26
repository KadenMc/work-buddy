"""Human-authority HTTP boundary for Thread-backed task proposals."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.dashboard import local_identity_api
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.threads.action_proposals import (
    ActionProposalService,
    ProposalError,
    get_action_proposal_service,
)
from work_buddy.truth.identity import canonical_json, sha256_text

logger = logging.getLogger(__name__)
ProposalAuthorizer = Callable[[str, str, str, str, Mapping[str, Any]], str]


def _default_authorizer(
    operation: str, subject: str, method: str, path: str, body: Mapping[str, Any]
) -> str:
    authority = local_identity_api.require_human_authority_request(
        action=f"dashboard.action_proposals.{operation}",
        subject=subject,
        context_sha256=sha256_text(
            canonical_json(
                {
                    "method": method,
                    "path": path,
                    "body": dict(body),
                }
            )
        ),
    )
    return authority.principal.actor.canonical_id


def _body() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ProposalError(
            "proposal_invalid", "The request body must be a JSON object."
        )
    return value


def _error(exc: Exception):
    if isinstance(exc, ProposalError):
        return jsonify({"ok": False, "error": exc.to_dict()}), exc.status_code
    if isinstance(exc, LocalIdentityError):
        return jsonify(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                },
            }
        ), exc.status
    logger.exception("Action proposal request failed")
    return jsonify(
        {
            "ok": False,
            "error": {
                "code": "proposal_unavailable",
                "message": "The proposal is temporarily unavailable. Your draft has not been cleared.",
                "retryable": True,
            },
        }
    ), 503


def create_blueprint(
    proposal_service: ActionProposalService | None = None,
    *,
    authorizer: ProposalAuthorizer | None = None,
    dashboard_read_only: Callable[[], bool] | None = None,
) -> Blueprint:
    """Create an injectable boundary without constructing production services."""
    blueprint = Blueprint("action_proposals", __name__)
    authorize_request = authorizer or _default_authorizer

    def service() -> ActionProposalService:
        return proposal_service or get_action_proposal_service()

    def authorize(operation: str, subject: str, body: Mapping[str, Any]) -> str:
        if dashboard_read_only and dashboard_read_only():
            raise ProposalError(
                "proposal_read_only", "The dashboard is read-only.", 403
            )
        return authorize_request(operation, subject, request.method, request.path, body)

    @blueprint.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/threads/action-proposals")
    def create_proposal():
        try:
            body = _body()
            if set(body) - {"client_mutation_id", "action", "origin"}:
                raise ProposalError("proposal_invalid", "Unsupported proposal fields.")
            action = body.get("action")
            if (
                not isinstance(action, dict)
                or action.get("name") != "task_create"
                or action.get("kind", "standard") != "standard"
            ):
                raise ProposalError(
                    "proposal_wrong_kind",
                    "Only the standard task_create action is supported.",
                )
            if set(action) - {"name", "kind", "parameters"}:
                raise ProposalError("proposal_invalid", "Unsupported action fields.")
            actor = authorize(
                "create", f"proposal:new:{body.get('client_mutation_id', '')}", body
            )
            result = service().create_task_proposal(
                client_mutation_id=body.get("client_mutation_id"),
                parameters=action.get("parameters"),
                origin=body.get("origin"),
                actor=actor,
            )
            return jsonify(result), 200 if result.get("replayed") else 201
        except Exception as exc:  # noqa: BLE001 - sanitized HTTP error boundary
            return _error(exc)

    @blueprint.get("/api/threads/<thread_id>/proposal")
    def get_proposal(thread_id: str):
        try:
            return jsonify(service().get(thread_id))
        except Exception as exc:  # noqa: BLE001 - sanitized HTTP error boundary
            return _error(exc)

    @blueprint.post("/api/threads/<thread_id>/proposal/revise")
    def revise_proposal(thread_id: str):
        try:
            body = _body()
            if set(body) - {
                "client_mutation_id",
                "expected_proposal_event_id",
                "parameters",
            }:
                raise ProposalError("proposal_invalid", "Unsupported revision fields.")
            actor = authorize("revise", f"proposal:{thread_id}", body)
            return jsonify(
                service().revise(
                    thread_id,
                    client_mutation_id=body.get("client_mutation_id"),
                    expected_proposal_event_id=body.get("expected_proposal_event_id"),
                    parameters=body.get("parameters"),
                    actor=actor,
                )
            )
        except Exception as exc:  # noqa: BLE001 - sanitized HTTP error boundary
            return _error(exc)

    def decide(thread_id: str, operation: str):
        try:
            body = _body()
            if set(body) - {"client_mutation_id", "expected_proposal_event_id"}:
                raise ProposalError(
                    "proposal_invalid",
                    "Review edits before accepting or rejecting the proposal.",
                )
            actor = authorize(operation, f"proposal:{thread_id}", body)
            return jsonify(
                getattr(service(), operation)(
                    thread_id,
                    client_mutation_id=body.get("client_mutation_id"),
                    expected_proposal_event_id=body.get("expected_proposal_event_id"),
                    actor=actor,
                )
            )
        except Exception as exc:  # noqa: BLE001 - sanitized HTTP error boundary
            return _error(exc)

    @blueprint.post("/api/threads/<thread_id>/proposal/accept")
    def accept_proposal(thread_id: str):
        return decide(thread_id, "accept")

    @blueprint.post("/api/threads/<thread_id>/proposal/reject")
    def reject_proposal(thread_id: str):
        return decide(thread_id, "reject")

    return blueprint


__all__ = ["create_blueprint"]
