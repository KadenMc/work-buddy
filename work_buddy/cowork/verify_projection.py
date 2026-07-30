"""Read-only dashboard projection for Co-work Verify and Co-think."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from work_buddy.cowork import readiness
from work_buddy.cowork.verify import (
    ActionSnapshot,
    CheckDefinitionVersion,
    CheckExecution,
    CriterionDefinitionVersion,
    EvaluationResult,
    ResultRelation,
    cothink_items,
    surfaced_results,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_orchestration import (
    VERIFY_CONTRACT_VERSION,
    run_status_projection,
)
from work_buddy.cowork.execution_identity import CoworkVerifyRole
from work_buddy.cowork.verify_coordination import (
    coordination_jobs_with_runtime_fallback,
)
from work_buddy.cowork.verify_rechecks import verification_recheck_intents
from work_buddy.cowork.verify_configuration import (
    list_effective_verification_configuration,
)
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.store import DocumentRecord, TruthStore


_DISPOSITION_VIEW = {
    "surface": "surface_result",
    "route_to_correction": "surface_proposal",
    "suppress": "retain_without_interrupting",
    "defer": "defer_until_boundary",
    "supersede": "retain_without_interrupting",
}


def _current_head(store: TruthStore, document: DocumentRecord) -> str | None:
    if document.ydoc_snapshot_sha256 is None:
        return None
    return ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )


def _quote_anchor(evidence: object) -> dict[str, str] | None:
    try:
        selector = CompositeSelector.from_web_annotation(evidence)
    except Exception:
        return None
    return {
        "exact": selector.exact,
        "prefix": selector.prefix,
        "suffix": selector.suffix,
    }


def capability_projection(
    store: TruthStore,
    document: DocumentRecord,
    *,
    read_only: bool,
    document_readiness: readiness.DocumentReadiness | None = None,
    active: bool | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    state = document_readiness or readiness.classify_document(
        store,
        document,
        read_only=read_only,
        conn=conn,
    )
    if active is None:
        active = (
            documents.current_lifecycle(store, document.id, conn=conn)
            == "active"
        )
    enabled = state.initialization_state == "ready" and active
    disabled_reason = None
    if not active:
        disabled_reason = "Co-work Verify is unavailable for a retired document."
    elif state.initialization_state != "ready":
        disabled_reason = state.disabled_reason or (
            "This document is not ready for exact-version evaluation."
        )
    elif read_only:
        disabled_reason = "The dashboard is read-only."
    return {
        "enabled": enabled,
        "contract_version": VERIFY_CONTRACT_VERSION,
        "can_run": enabled and not read_only,
        "can_configure": enabled and not read_only,
        "can_cothink": enabled and not read_only,
        "disabled_reason": disabled_reason,
    }


def run_summaries(
    store: TruthStore,
    document: DocumentRecord,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            key: value
            for key, value in summary.items()
            if not key.startswith("_")
        }
        for summary in run_status_projection(
            store,
            document_id=document.id,
            current_document=document,
            conn=conn,
        )
    )


def result_projection(
    store: TruthStore,
    document: DocumentRecord,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    current_head = _current_head(store, document)
    projected: list[dict[str, Any]] = []
    for item in surfaced_results(
        store,
        document_id=document.id,
        conn=conn,
    ):
        result = verify_store.get_record(
            store,
            EvaluationResult,
            item["id"],
            conn=conn,
        )
        if result is None:
            continue
        execution = verify_store.get_record(
            store,
            CheckExecution,
            result.check_execution_id,
            conn=conn,
        )
        criterion = verify_store.get_record(
            store,
            CriterionDefinitionVersion,
            result.criterion_definition_version_id,
            conn=conn,
        )
        check = (
            None
            if execution is None
            else verify_store.get_record(
                store,
                CheckDefinitionVersion,
                execution.check_definition_version_id,
                conn=conn,
            )
        )
        action = verify_store.get_record(
            store,
            ActionSnapshot,
            item["action_snapshot_id"],
            conn=conn,
        )
        if criterion is None or action is None:
            continue
        relations = verify_store.list_records(
            store,
            ResultRelation,
            where=(
                "source.evaluation_result_id = ? "
                "AND source.relation_kind = 'addresses' "
                "AND source.target_kind = 'proposal'"
            ),
            params=(result.id,),
            conn=conn,
        )
        evidence = item["evidence_selector"]
        decision = str(item["disposition"]["decision"])
        try:
            result_payload = json.loads(result.payload_json)
        except (TypeError, json.JSONDecodeError):
            result_payload = {}
        coverage = (
            result_payload.get("coverage")
            if isinstance(result_payload, dict)
            else None
        )
        coverage_label = {
            "complete_target_review": "Model review of the complete frozen target",
            "partial_target_review": "Partial model review of the frozen target",
            "not_assessed": "Model review coverage was not assessed",
        }.get(
            coverage,
            (
                "Complete exact-string coverage of the frozen document"
                if action.target_kind == "document"
                else "Complete exact-string coverage of the frozen target"
            ),
        )
        check_limitations = (
            []
            if check is None
            else json.loads(check.limitations_json)
        )
        result_limitations = (
            result_payload.get("limitations", [])
            if isinstance(result_payload, dict)
            else []
        )
        limitations = list(
            dict.fromkeys(
                [
                    str(value)
                    for value in [*check_limitations, *result_limitations]
                    if isinstance(value, str) and value.strip()
                ]
            )
        )
        projected.append(
            {
                "result_id": result.id,
                "run_id": result.evaluation_run_id,
                "kind": (
                    "nonconforming"
                    if result.result_kind == "finding"
                    else result.result_kind
                    if result.result_kind
                    in {
                        "conforming",
                        "nonconforming",
                        "inconclusive",
                        "review_comment",
                    }
                    else "review_comment"
                ),
                "criterion_label": criterion.title,
                "criterion_statement": criterion.description,
                "check_label": (
                    "Unknown check" if check is None else check.title
                ),
                "method_label": (
                    "Unknown method"
                    if check is None
                    else (
                        "Deterministic exact match"
                        if check.mechanism == "deterministic"
                        else "Instruction-based model evaluation"
                        if check.mechanism == "model_judge"
                        else check.mechanism.replace("_", " ").title()
                    )
                ),
                "explanation": result.message,
                "quote_anchor": _quote_anchor(evidence),
                "coverage_label": coverage_label,
                "limitations": limitations,
                "current_version": (
                    current_head == action.structured_head_sha256
                ),
                "disposition": _DISPOSITION_VIEW.get(
                    decision,
                    "retain_without_interrupting",
                ),
                "canonical_sha256": result.canonical_sha256,
                "proposal_ids": [
                    relation.target_ref for relation in relations
                ],
                "created_at": result.created_at,
            }
        )
    return tuple(projected)


def cothink_projection(
    store: TruthStore,
    document: DocumentRecord,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    current_head = _current_head(store, document)
    items: list[dict[str, Any]] = []
    for item in cothink_items(
        store,
        document_id=document.id,
        conn=conn,
    ):
        action = verify_store.get_record(
            store,
            ActionSnapshot,
            item["action_snapshot_id"],
            conn=conn,
        )
        if action is None:
            continue
        selector = json.loads(action.target_selector_json)
        evidence = selector if action.target_kind == "text_quote" else None
        payload = item["payload"]
        items.append(
            {
                "item_id": item["id"],
                "subtype": "alternative_perspective",
                "content": str(payload.get("text") or ""),
                "rationale": item["rationale"],
                "target_label": (
                    "Whole document"
                    if action.target_kind == "document"
                    else "Document target"
                ),
                "quote_anchor": _quote_anchor(evidence),
                "status": str(item["lifecycle"]["status"]),
                "current_version": (
                    current_head == action.structured_head_sha256
                ),
                "canonical_sha256": item["canonical_sha256"],
                "created_at": item["created_at"],
            }
        )
    return tuple(items)


def cothink_outcome_projection(
    store: TruthStore,
    document: DocumentRecord,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project persisted Co-think completion/unavailability, even without items."""

    current_head = _current_head(store, document)
    outcomes: list[dict[str, Any]] = []
    for job in coordination_jobs_with_runtime_fallback(
        store,
        document_id=document.id,
        role=CoworkVerifyRole.COTHINK,
        conn=conn,
    ):
        action = verify_store.get_record(
            store,
            ActionSnapshot,
            job["action_snapshot_id"],
            conn=conn,
        )
        if action is None:
            continue
        outcome_kind = job["outcome_kind"]
        if outcome_kind in {
            "completed_with_item",
            "completed_no_useful_item",
        }:
            status = outcome_kind
        elif job["status"] in {"unavailable", "failed"}:
            status = "unavailable"
        else:
            status = "running"
        outcomes.append(
            {
                "outcome_id": job["job_id"],
                "status": status,
                "rationale": str(
                    job["message"]
                    or (
                        "Looking for one useful alternative perspective."
                        if status == "running"
                        else "No useful alternative perspective was found."
                        if status == "completed_no_useful_item"
                        else ""
                    )
                ),
                "target_label": (
                    "Whole document"
                    if action.target_kind == "document"
                    else "Document target"
                ),
                "current_version": (
                    current_head == action.structured_head_sha256
                ),
                "provider_id": str(
                    job["selection"].get("provider_id") or ""
                ),
                "model_id": str(
                    job["selection"].get("model_id") or ""
                ),
                "created_at": job["created_at"],
                "finished_at": (
                    job["updated_at"]
                    if status != "running"
                    else None
                ),
            }
        )
    return tuple(outcomes)


def document_additions(
    store: TruthStore,
    document: DocumentRecord,
    *,
    read_only: bool,
    document_readiness: readiness.DocumentReadiness | None = None,
    active: bool | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Return the additive R2 fields; old clients safely ignore them."""

    return {
        "capabilities": {
            "cowork_verify": capability_projection(
                store,
                document,
                read_only=read_only,
                document_readiness=document_readiness,
                active=active,
                conn=conn,
            )
        },
        "evaluation_run_summaries": run_summaries(
            store,
            document,
            conn=conn,
        ),
        "evaluation_results": result_projection(
            store,
            document,
            conn=conn,
        ),
        "verification_recheck_intents": tuple(
            intent.to_dict()
            for intent in verification_recheck_intents(
                store,
                document_id=document.id,
                conn=conn,
            )
        ),
        "cothink_items": cothink_projection(
            store,
            document,
            conn=conn,
        ),
        "cothink_outcomes": cothink_outcome_projection(
            store,
            document,
            conn=conn,
        ),
        "verification_configuration": (
            list_effective_verification_configuration(
                store,
                document_id=document.id,
                ensure_system_defaults=False,
                document=document,
                conn=conn,
            )
        ),
    }


__all__ = [
    "capability_projection",
    "cothink_projection",
    "cothink_outcome_projection",
    "document_additions",
    "result_projection",
    "run_summaries",
]
