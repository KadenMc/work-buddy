"""Dashboard HTTP adapter for durable AI-prepared Co-work Truth candidates."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.consent import user_initiated
from work_buddy.cowork import truth_analysis, truth_analysis_runtime, truth_surface
from work_buddy.cowork.api import (
    _document_surface_or_403,
    _emit,
    _fail,
    _reject_read_only,
    _resolve_document,
    _resolve_store,
)
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.truth_analysis_dispatch import enqueue_truth_analysis_launch
from work_buddy.dashboard.local_identity_api import require_human_authority_request
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth import documents
from work_buddy.truth.contracts import InvariantViolation


truth_analysis_blueprint = Blueprint("cowork_truth_analysis", __name__)
logger = logging.getLogger(__name__)


def _body() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise truth_analysis.TruthAnalysisError(
            "invalid_request", "request body must be a JSON object"
        )
    return dict(value)


def _selection(body: Mapping[str, Any]) -> AgentExecutionSelection:
    value = body.get("execution")
    if not isinstance(value, Mapping):
        raise truth_analysis.TruthAnalysisError(
            "invalid_execution",
            "execution must name an explicit provider_id and model_id",
        )
    provider_id = value.get("provider_id")
    model_id = value.get("model_id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise truth_analysis.TruthAnalysisError(
            "invalid_execution", "execution.provider_id is required"
        )
    if not isinstance(model_id, str) or not model_id.strip():
        raise truth_analysis.TruthAnalysisError(
            "invalid_execution", "execution.model_id is required"
        )
    return AgentExecutionSelection(
        provider_id=provider_id,
        model_id=model_id,
        provider_label=str(value.get("provider_label") or ""),
        model_label=str(value.get("model_label") or ""),
    )


def _context(document_id: str, *, mutation: bool):
    if mutation:
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
    if not document_surface_allowed(store, document):
        return None, None, _fail(
            "This document is not available in Co-work for this folder.", 403
        )
    if mutation and documents.current_lifecycle(store, document.id) != "active":
        return None, None, _fail(
            "This action cannot run on a retired document.", 409
        )
    return store, document, None


def _safe_error(exc: Exception):
    if isinstance(exc, LocalIdentityError):
        return (
            jsonify(
                {
                    "ok": False,
                    "code": exc.code,
                    "error": {"code": exc.code, "message": str(exc)},
                }
            ),
            exc.status,
        )
    if isinstance(exc, (truth_analysis.TruthAnalysisError, truth_surface.TruthSurfaceError)):
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "code": exc.code,
            "retryable": exc.retryable,
        }
        if exc.details:
            payload["details"] = dict(exc.details)
        return jsonify(payload), exc.status
    if isinstance(exc, (InvariantViolation, ValueError, KeyError)):
        return _fail(str(exc), 409)
    logger.exception("Co-work Truth analysis request failed")
    return _fail("Co-work could not complete this Truth analysis action.", 500)


def _bound_run(store_id: str, document_id: str, run_id: str):
    run = truth_analysis_runtime.get_run(run_id)
    if (
        run is None
        or run.store_id != store_id
        or run.document_id != document_id
    ):
        raise truth_analysis.TruthAnalysisError(
            "analysis_run_not_found",
            "Truth analysis run does not exist for this document.",
            status=404,
        )
    return run


@truth_analysis_blueprint.get(
    "/api/truth/doc/<document_id>/truth/analysis-capabilities"
)
def api_truth_analysis_capabilities(document_id: str):
    """Report only execution guarantees the server can actually enforce."""

    _store, _document, error = _context(document_id, mutation=False)
    if error:
        return error
    return jsonify(truth_analysis.analysis_capabilities_view())


@truth_analysis_blueprint.post(
    "/api/truth/doc/<document_id>/truth/analysis-runs"
)
def api_start_truth_analysis(document_id: str):
    store, document, error = _context(document_id, mutation=True)
    if error:
        return error
    try:
        body = _body()
        capture = body.get("capture")
        if not isinstance(capture, Mapping):
            raise truth_analysis.TruthAnalysisError(
                "invalid_request", "capture is required"
            )
        selection = _selection(body)
        authority = require_human_authority_request(
            action=truth_analysis.ANALYSIS_START_ACTION,
            subject=truth_analysis.analysis_start_subject(document.id),
            context_sha256=truth_analysis.analysis_start_context_sha256(
                store_id=store.store_id,
                document_id=document.id,
                capture=capture,
                selection=selection,
            ),
        )
        actor = truth_analysis.human_analysis_start_actor(
            authority,
            store_id=store.store_id,
            document_id=document.id,
            capture=capture,
            selection=selection,
        )
        with user_initiated("dashboard.cowork.truth_analysis"):
            view = truth_analysis.prepare_analysis_run(
                store,
                document_id=document.id,
                capture=capture,
                selection=selection,
                actor=actor,
            )
            run = _bound_run(store.store_id, document.id, view["analysis_run_id"])
            if run.status in {"prepared", "launching", "running"}:
                enqueue_truth_analysis_launch(run, store=store)
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    _emit(
        "truth.doc_analysis_started",
        store.store_id,
        {
            "document_id": document.id,
            "analysis_run_id": run.run_id,
            "action_snapshot_id": run.action_snapshot_id,
        },
        event_id=f"cowork-truth-analysis:{run.run_id}",
    )
    return jsonify(truth_analysis.analysis_run_view(run, store=store)), 202


@truth_analysis_blueprint.get(
    "/api/truth/doc/<document_id>/truth/analysis-runs/current"
)
def api_current_truth_analysis(document_id: str):
    store, document, error = _context(document_id, mutation=False)
    if error:
        return error
    runs = truth_analysis_runtime.runs_for_document(store.store_id, document.id)
    if not runs:
        return _fail("No Truth analysis run exists for this document.", 404)
    return jsonify(truth_analysis.analysis_run_view(runs[-1], store=store))


@truth_analysis_blueprint.get(
    "/api/truth/doc/<document_id>/truth/analysis-runs/<run_id>"
)
def api_truth_analysis_run(document_id: str, run_id: str):
    store, document, error = _context(document_id, mutation=False)
    if error:
        return error
    try:
        run = _bound_run(store.store_id, document.id, run_id)
        return jsonify(truth_analysis.analysis_run_view(run, store=store))
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)


@truth_analysis_blueprint.post(
    "/api/truth/doc/<document_id>/truth/analysis-runs/<run_id>/candidates/"
    "<candidate_id>/decisions"
)
def api_truth_analysis_candidate_decision(
    document_id: str,
    run_id: str,
    candidate_id: str,
):
    store, document, error = _context(document_id, mutation=True)
    if error:
        return error
    try:
        _bound_run(store.store_id, document.id, run_id)
        body = _body()
        edits = body.get("edits")
        if edits is not None and not isinstance(edits, Mapping):
            raise truth_analysis.TruthAnalysisError(
                "invalid_candidate_edits", "edits must be an object"
            )
        submitted_edits = None if edits is None else dict(edits)
        existing_claim_id = (
            None
            if body.get("existing_claim_id") is None
            else str(body.get("existing_claim_id"))
        )
        expected_canonical_sha256 = str(
            body.get("expected_canonical_sha256") or ""
        )
        decision = str(body.get("decision") or "")
        authority = require_human_authority_request(
            action=truth_analysis.CANDIDATE_DECISION_ACTION,
            subject=truth_analysis.candidate_decision_subject(
                run_id, candidate_id
            ),
            context_sha256=truth_analysis.candidate_decision_context_sha256(
                store_id=store.store_id,
                document_id=document.id,
                run_id=run_id,
                candidate_id=candidate_id,
                expected_canonical_sha256=expected_canonical_sha256,
                decision=decision,
                existing_claim_id=existing_claim_id,
                edits=submitted_edits,
            ),
        )
        with user_initiated("dashboard.cowork.truth_analysis_decision"):
            result = truth_analysis.commit_candidate_decision(
                run_id=run_id,
                candidate_id=candidate_id,
                expected_canonical_sha256=expected_canonical_sha256,
                decision=decision,
                authority_context=authority,
                edits=submitted_edits,
                existing_claim_id=existing_claim_id,
            )
    except Exception as exc:  # noqa: BLE001
        return _safe_error(exc)
    _emit(
        "truth.doc_analysis_candidate_decided",
        store.store_id,
        {
            "document_id": document.id,
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_status": result["candidate_status"],
            "claim_id": result["claim_id"],
            "expression_id": result["expression_id"],
        },
        event_id=(
            f"cowork-truth-analysis-decision:{run_id}:{candidate_id}:"
            f"{result['candidate_status']}"
        ),
    )
    return jsonify(result)


__all__ = ["truth_analysis_blueprint"]
