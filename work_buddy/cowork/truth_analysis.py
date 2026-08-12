"""Staged AI analysis for the Co-work Truth review surface.

The service freezes one exact browser-selected passage, snapshots the bounded
Truth context shown to a job-scoped worker, and validates an immutable typed
candidate set into the private runtime database.  No claim, expression,
evidence, or support link is written until a separately authenticated human
decision calls :func:`commit_candidate_decision`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork import (
    truth_analysis_disclosure,
    truth_analysis_research,
    truth_analysis_runtime,
    truth_surface,
)
from work_buddy.cowork.chat_targets import action_snapshot_view
from work_buddy.cowork.execution_identity import cowork_truth_analysis_session_id
from work_buddy.cowork.truth_analysis_jobs import DEFAULT_TRUTH_ANALYSIS_BUDGET_USD
from work_buddy.cowork.truth_analysis_runtime import TruthAnalysisRuntimeRun
from work_buddy.agent_execution.disclosure import DisclosureError, ManifestDigest
from work_buddy.cowork.verify import (
    ActionSnapshot,
    record_model_call_authorization,
)
from work_buddy.cowork.verify import store as verify_store
from work_buddy.cowork.verify_orchestration import (
    VerifyOrchestrationError,
    _validate_capture,
)
from work_buddy.cowork.verify_execution import provider_cost_control
from work_buddy.paths import resolve
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import (
    HUMAN_AUTHORITY_ASSURANCE,
    HUMAN_AUTHORITY_BASIS,
    HumanAuthorityContext,
)
from work_buddy.sources import (
    CoworkActionSnapshotProvider,
    ProviderRegistry,
    SourceStore,
    cowork_action_snapshot_origin,
    source_capture_from_origin,
)
from work_buddy.truth import documents
from work_buddy.truth.anchors import CompositeSelector, reanchor, serialize_selector
from work_buddy.truth.contracts import Actor, InvariantViolation, TERMINAL_STATUSES
from work_buddy.truth.identity import (
    canonical_json,
    claim_sha256,
    sha256_text,
    utc_now,
)
from work_buddy.truth.profiles import validate_new_claim
from work_buddy.truth import queries as truth_queries
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.source_claims import (
    CandidateDecisionAuthorization,
    OPERATION_NAME as SOURCE_CLAIM_OPERATION,
    SOURCE_PURPOSE as SOURCE_CLAIM_PURPOSE,
    SourceClaimActors,
    SourceClaimCandidate,
    prepare_source_claim,
    reconcile_source_usage,
    source_claim_request_sha256,
    write_prepared_source_claim,
)
from work_buddy.truth.source_provenance import (
    record_candidate_decision as record_truth_candidate_decision,
    record_operation_result as record_truth_operation_result,
)
from work_buddy.truth.store import (
    EXPRESSION_ROLES,
    AcquisitionOrigin,
    DocumentRecord,
    TruthStore,
)


ANALYSIS_REQUEST_SCHEMA = "wb.cowork.truth-analysis-request/v1"
ANALYSIS_CONTEXT_SCHEMA = "wb.cowork.truth-analysis-job/v1"
ANALYSIS_OUTPUT_SCHEMA = "wb.cowork.truth-analysis-output/v1"
ANALYSIS_START_ACTION = "cowork.truth.analysis_start"
ANALYSIS_START_GESTURE_SCHEMA = "wb.cowork.truth-analysis-start-gesture/v1"
CANDIDATE_DECISION_ACTION = "cowork.truth.candidate_decision"
CANDIDATE_DECISION_GESTURE_SCHEMA = (
    "wb.cowork.truth-candidate-decision-gesture/v1"
)
MAX_CANDIDATES = 20
MAX_EVIDENCE_PER_CANDIDATE = 10
MAX_EXISTING_CLAIMS = 200
MAX_EXISTING_RECEIPTS = 200
MAX_WEB_SEARCHES = truth_analysis_research.MAX_QUERIES_PER_RUN
MAX_WEB_RESULTS_PER_SEARCH = truth_analysis_research.MAX_HITS_PER_QUERY
MAX_WEB_FETCHES = truth_analysis_research.MAX_FETCHES_PER_RUN
MAX_WEB_FETCH_BYTES = truth_analysis_research.MAX_RESPONSE_BYTES
MAX_WEB_CAPTURED_TEXT_BYTES = truth_analysis_research.MAX_CAPTURED_TEXT_BYTES
MAX_SELECTED_PASSAGE_BYTES = 32 * 1024
MAX_EXISTING_TRUTH_CONTEXT_BYTES = 32 * 1024
MAX_EXISTING_TRUTH_CLAIMS_BYTES = 18 * 1024
MAX_EXISTING_TRUTH_RECEIPTS_BYTES = 10 * 1024
MAX_WORKER_CONTEXT_BYTES = 90_000
MAX_NORMALIZED_OUTPUT_BYTES = 80_000
MAX_SUMMARY_CHARS = 4_000
MAX_LIMITATION_CHARS = 1_000
MAX_RATIONALE_CHARS = 2_000
MAX_COVERAGE_DETAIL_CHARS = 2_000
MAX_SOURCE_LOCATOR_CHARS = 4_096
_AUTHORIZATION_TTL = timedelta(hours=24)
_MATCH_RELATIONSHIPS = frozenset(
    {"exact", "equivalent", "overlaps", "conflicts"}
)
_EVIDENCE_RELATIONSHIPS = frozenset(
    {
        "supports",
        "partially_supports",
        "contradicts",
        "mentions",
        "does_not_address",
        "inconclusive",
    }
)
_COVERAGE_STATUSES = frozenset(
    {"supplied", "searched", "partial", "not_searched", "unavailable", "failed"}
)

SelectionValidator = Callable[[AgentExecutionSelection], AgentExecutionSelection]


class TruthAnalysisError(InvariantViolation):
    """Typed service failure safe for later HTTP projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


def _disclosure_failure(exc: Exception) -> TruthAnalysisError:
    code = (
        exc.error_code
        if isinstance(exc, DisclosureError)
        else "analysis_disclosure_unavailable"
    )
    return TruthAnalysisError(
        str(code),
        "Truth analysis could not safely account for model/provider disclosure.",
        status=409,
        retryable=False,
    )


def analysis_provider_capability(provider_id: str) -> dict[str, Any]:
    control = provider_cost_control(provider_id)
    eligible = (
        control.enforcement_class == "hard_ceiling"
        and control.ceiling_usd_per_worker_session is not None
        and control.ceiling_usd_per_worker_session
        >= DEFAULT_TRUTH_ANALYSIS_BUDGET_USD
    )
    return {
        "provider_id": provider_id,
        "analysis_available": eligible,
        "unavailable_reason": (
            None
            if eligible
            else "Truth analysis requires a provider-enforced hard spending ceiling."
        ),
        "applies_to_all_models": True,
        "cost_control": control.to_dict(),
    }


def analysis_capabilities_view() -> dict[str, Any]:
    return {
        "ok": True,
        "schema": "wb.cowork.truth-analysis-capabilities/v1",
        "required_cost_control": {
            "enforcement_class": "hard_ceiling",
            "scope": "worker_model_session",
            "maximum_usd_per_model_session": DEFAULT_TRUTH_ANALYSIS_BUDGET_USD,
        },
        "research_cost_control": {
            "enforcement_class": "unavailable",
            "scope": "web_search_and_fetch",
            "ceiling_usd": None,
            "basis": "research_provider_cost_not_enforced",
        },
        "providers": [
            analysis_provider_capability(provider_id)
            for provider_id in ("claude-code", "codex")
        ],
    }


def _required_text(value: object, label: str, *, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TruthAnalysisError("invalid_output", f"{label} must be nonempty text")
    if len(value) > maximum:
        raise TruthAnalysisError("invalid_output", f"{label} is too long")
    return value


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TruthAnalysisError("invalid_output", f"{label} must be an object")
    return dict(value)


def _confidence(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TruthAnalysisError("invalid_output", f"{label} must be a number")
    normalized = float(value)
    if normalized < 0 or normalized > 1:
        raise TruthAnalysisError("invalid_output", f"{label} must be 0 through 1")
    return normalized


def _optional_text(value: object, label: str, *, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TruthAnalysisError("invalid_output", f"{label} must be text")
    if len(value) > maximum:
        raise TruthAnalysisError("invalid_output", f"{label} is too long")
    return value


def _default_selection_validator(
    selection: AgentExecutionSelection,
) -> AgentExecutionSelection:
    from work_buddy.agent_execution.registry import validate_selection

    return validate_selection(selection, refresh=False)


def _action(store: TruthStore, run: TruthAnalysisRuntimeRun) -> ActionSnapshot:
    action = verify_store.get_record(store, ActionSnapshot, run.action_snapshot_id)
    if action is None or action.document_id != run.document_id:
        raise TruthAnalysisError(
            "analysis_context_unavailable",
            "The frozen passage for this analysis is unavailable.",
            status=409,
        )
    return action


def _action_context(action: ActionSnapshot) -> dict[str, Any]:
    try:
        value = json.loads(action.context_boundary_json)
    except (TypeError, ValueError) as exc:
        raise TruthAnalysisError(
            "analysis_context_unavailable",
            "The frozen passage metadata is unavailable.",
            status=409,
        ) from exc
    if not isinstance(value, Mapping):
        raise TruthAnalysisError(
            "analysis_context_unavailable",
            "The frozen passage metadata is unavailable.",
            status=409,
        )
    return dict(value)


def _existing_truth_context(
    store: TruthStore,
    document: DocumentRecord,
) -> dict[str, Any]:
    """Freeze compact folder claims and their active support receipts."""

    listing = truth_surface.truth_list(
        store,
        document,
        view="folder",
        filter_name="all",
        offset=0,
        limit=MAX_EXISTING_CLAIMS,
        read_only=True,
    )
    claim_candidates = [
        {
            key: item.get(key)
            for key in (
                "claim_id",
                "proposition",
                "claim_kind",
                "canonical_sha256",
                "scope",
                "base_status",
                "needs_review",
                "is_fact",
                "redacted",
            )
        }
        for item in listing["claims"]
        if not item.get("redacted") and isinstance(item.get("proposition"), str)
    ]
    claims: list[dict[str, Any]] = []
    claim_bytes = 0
    for item in claim_candidates:
        item_bytes = len(canonical_json(item).encode("utf-8")) + 1
        if claim_bytes + item_bytes > MAX_EXISTING_TRUTH_CLAIMS_BYTES:
            continue
        claims.append(item)
        claim_bytes += item_bytes
    claim_ids = {str(item["claim_id"]) for item in claims}
    claim_total = int(listing["total"])
    receipts: list[dict[str, Any]] = []
    receipt_total = 0
    if claim_ids:
        placeholders = ",".join("?" for _ in claim_ids)
        with store._read_connection() as conn:
            receipt_total = int(
                conn.execute(
                    "SELECT COUNT(*) "
                    "FROM claim_links l "
                    "JOIN evidence_spans s ON s.id = l.to_ref "
                    "JOIN evidence e ON e.id = s.evidence_id "
                    "LEFT JOIN link_retractions r ON r.link_id = l.id "
                    f"WHERE l.from_claim_id IN ({placeholders}) "
                    "AND l.link_type = 'supports_span' "
                    "AND l.to_kind = 'evidence_span' AND r.link_id IS NULL "
                    "AND s.redacted_at IS NULL AND e.redacted_at IS NULL",
                    tuple(sorted(claim_ids)),
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT l.from_claim_id AS claim_id, s.id AS span_id, "
                "s.quote_exact, s.span_sha256, e.id AS evidence_id, "
                "e.kind AS evidence_kind, e.source_locator, e.content_sha256, "
                "e.trust_class, e.acquired_at "
                "FROM claim_links l "
                "JOIN evidence_spans s ON s.id = l.to_ref "
                "JOIN evidence e ON e.id = s.evidence_id "
                "LEFT JOIN link_retractions r ON r.link_id = l.id "
                f"WHERE l.from_claim_id IN ({placeholders}) "
                "AND l.link_type = 'supports_span' "
                "AND l.to_kind = 'evidence_span' AND r.link_id IS NULL "
                "AND s.redacted_at IS NULL AND e.redacted_at IS NULL "
                "ORDER BY l.created_at, l.id LIMIT ?",
                (*sorted(claim_ids), MAX_EXISTING_RECEIPTS),
            ).fetchall()
        receipt_candidates = [
            {
                "claim_id": str(row["claim_id"]),
                "span_id": str(row["span_id"]),
                "quote": str(row["quote_exact"]),
                "span_sha256": str(row["span_sha256"]),
                "evidence_id": str(row["evidence_id"]),
                "evidence_kind": str(row["evidence_kind"]),
                "source_locator": str(row["source_locator"]),
                "content_sha256": str(row["content_sha256"]),
                "trust_class": str(row["trust_class"]),
                "acquired_at": str(row["acquired_at"]),
            }
            for row in rows
        ]
        receipt_bytes = 0
        for item in receipt_candidates:
            item_bytes = len(canonical_json(item).encode("utf-8")) + 1
            if receipt_bytes + item_bytes > MAX_EXISTING_TRUTH_RECEIPTS_BYTES:
                continue
            receipts.append(item)
            receipt_bytes += item_bytes
    value = {
        "scope": "folder_truth",
        "claims": claims,
        "receipts": receipts,
        "claim_total": claim_total,
        "claim_count_supplied": len(claims),
        "receipt_total_for_supplied_claims": receipt_total,
        "receipt_count_supplied": len(receipts),
        "claims_truncated": claim_total > len(claims),
        "receipts_truncated": receipt_total > len(receipts),
        "truncated": claim_total > len(claims) or receipt_total > len(receipts),
        "byte_budget": MAX_EXISTING_TRUTH_CONTEXT_BYTES,
    }
    value["serialized_bytes"] = 0
    for _ in range(3):
        value["serialized_bytes"] = len(canonical_json(value).encode("utf-8"))
    serialized_bytes = len(canonical_json(value).encode("utf-8"))
    if serialized_bytes > MAX_EXISTING_TRUTH_CONTEXT_BYTES:
        raise TruthAnalysisError(
            "analysis_context_budget_exceeded",
            "The bounded existing Truth context exceeded its serialized budget.",
            status=409,
        )
    value["serialized_bytes"] = serialized_bytes
    return value


def _authoritative_source_coverage(
    existing_truth: Mapping[str, Any],
) -> list[dict[str, Any]]:
    claims = list(existing_truth.get("claims", []))
    receipts = list(existing_truth.get("receipts", []))
    claim_total = int(existing_truth.get("claim_total") or len(claims))
    receipt_total = int(
        existing_truth.get("receipt_total_for_supplied_claims") or len(receipts)
    )
    partial = bool(
        existing_truth.get("claims_truncated")
        or existing_truth.get("receipts_truncated")
    )
    return [
        {
            "source": "selected_passage",
            "status": "supplied",
            "detail": "The exact frozen selected passage was supplied to the worker.",
            "external_egress": False,
        },
        {
            "source": "existing_truth",
            "status": "supplied",
            "detail": (
                f"Bounded context supplied {len(claims)} of {claim_total} claims and "
                f"{len(receipts)} of {receipt_total} support receipts for the supplied "
                f"claims; context coverage is {'partial' if partial else 'complete'}."
            ),
            "external_egress": False,
        },
        {
            "source": "web",
            "status": "not_searched",
            "detail": "This staged run had no admitted web-search call.",
            "external_egress": False,
        },
    ]


def _current_source_coverage(
    run: TruthAnalysisRuntimeRun,
) -> list[dict[str, Any]]:
    """Project actual persisted source use; never infer external egress."""

    base = [
        dict(item)
        for item in run.request.get("source_coverage", [])
        if isinstance(item, Mapping)
    ]
    if run.status == "completed" and run.output is not None:
        completed_coverage = {
            str(item.get("source")): dict(item)
            for item in run.output.get("source_coverage", [])
            if isinstance(item, Mapping)
        }
        base = [
            completed_coverage.get(str(item.get("source")), item)
            if item.get("source") in {"selected_passage", "existing_truth"}
            else item
            for item in base
        ]
    searches = truth_analysis_runtime.search_receipts_for_run(run.run_id)
    if not searches:
        return base
    fetches = truth_analysis_runtime.fetch_receipts_for_run(run.run_id)
    completed_searches = sum(item.status == "completed" for item in searches)
    hit_count = sum(len(item.hits) for item in searches if item.status == "completed")
    completed_fetches = sum(item.status == "completed" for item in fetches)
    failed_searches = len(searches) - completed_searches
    unavailable_fetches = len(fetches) - completed_fetches
    research_fetches = truth_analysis_research.receipts_for_run(
        run_id=run.run_id,
        agent_session_id=run.session_id,
    )
    truncated_fetches = sum(
        bool(item.acquisition_metadata.get("text_truncated"))
        for item in research_fetches
        if item.status == "completed"
    )
    partial = bool(
        completed_searches
        and (failed_searches or unavailable_fetches or truncated_fetches)
    )
    web = {
        "source": "web",
        "status": "partial" if partial else "searched" if completed_searches else "failed",
        "detail": (
            f"{completed_searches} completed bounded queries returned {hit_count} hits; "
            f"{completed_fetches} admitted hits supplied captured text; "
            f"{failed_searches} queries failed; {unavailable_fetches} requested fetches "
            f"failed or were unavailable; {truncated_fetches} captured source texts were "
            f"truncated at {MAX_WEB_CAPTURED_TEXT_BYTES} bytes."
        ),
        "external_egress": any(item.external_egress for item in searches)
        or any(item.external_egress for item in fetches),
    }
    return [web if item.get("source") == "web" else item for item in base]


def _unresolved_review_count(run: TruthAnalysisRuntimeRun) -> int | None:
    """Return the unresolved count, or ``None`` for an active worker run."""

    if run.status in {"prepared", "launching", "running"}:
        return None
    if run.status != "completed" or run.output is None:
        return 0
    candidates = run.output.get("candidates")
    if not isinstance(candidates, list):
        return 0
    decided = {
        item.candidate_id
        for item in truth_analysis_runtime.candidate_decisions_for_run(run.run_id)
    }
    return sum(
        isinstance(item, Mapping)
        and isinstance(item.get("candidate_id"), str)
        and item["candidate_id"] not in decided
        for item in candidates
    )


def prepare_analysis_run(
    store: TruthStore,
    *,
    document_id: str,
    capture: Mapping[str, Any],
    selection: AgentExecutionSelection,
    actor: Actor,
    selection_validator: SelectionValidator | None = None,
) -> dict[str, Any]:
    """Freeze one passage and create a prepared private analysis run."""

    if actor.kind != "human" or not actor.ref:
        raise TruthAnalysisError(
            "human_actor_required", "A dashboard human must start Truth analysis."
        )
    validator = selection_validator or _default_selection_validator
    try:
        validated_selection = validator(selection)
        provider_capability = analysis_provider_capability(
            validated_selection.provider_id
        )
        if provider_capability["analysis_available"] is not True:
            raise TruthAnalysisError(
                "analysis_provider_cost_control_unavailable",
                str(provider_capability["unavailable_reason"]),
                status=409,
                details={"provider_capability": provider_capability},
            )
        action = _validate_capture(
            store,
            document_id=document_id,
            capture=capture,
            actor=actor,
            selection=validated_selection,
            purpose="truth_analysis",
            authority_context={"authorized_by_ref": actor.ref},
        )
    except TruthAnalysisError:
        raise
    except (VerifyOrchestrationError, ValueError) as exc:
        raise TruthAnalysisError("invalid_analysis_request", str(exc), status=409) from exc
    if action.target_kind != "text_quote":
        raise TruthAnalysisError(
            "passage_required",
            "Analyze selected passage requires an exact text selection.",
            status=409,
        )
    action_context = _action_context(action)
    target_choice = action_context.get("target_source")
    if target_choice not in {"current_selection", "working_target"}:
        raise TruthAnalysisError(
            "passage_required",
            "Analyze selected passage requires the current selection or Working on target.",
            status=409,
        )
    captured_view = action_snapshot_view(
        store,
        document_id=document_id,
        action_snapshot_id=action.id,
    )
    captured_target = str(captured_view["target"]["text"])
    captured_target_bytes = len(captured_target.encode("utf-8"))
    if captured_target_bytes > MAX_SELECTED_PASSAGE_BYTES:
        raise TruthAnalysisError(
            "analysis_passage_too_large",
            "The selected passage is too large for one bounded Truth analysis.",
            status=413,
            details={
                "selected_passage_bytes": captured_target_bytes,
                "maximum_selected_passage_bytes": MAX_SELECTED_PASSAGE_BYTES,
            },
        )
    document = documents.get_document(store, document_id)
    existing_truth = _existing_truth_context(store, document)
    coverage = _authoritative_source_coverage(existing_truth)
    request_payload = {
        "schema": ANALYSIS_REQUEST_SCHEMA,
        "authorized_by_ref": actor.ref,
        "target_choice": target_choice,
        "existing_truth": existing_truth,
        "source_coverage": coverage,
    }
    context_identity = {
        "schema": ANALYSIS_CONTEXT_SCHEMA,
        "action_snapshot_id": action.id,
        "target_text_sha256": action.target_text_sha256,
        "allowed_claim_kinds": list(store.profile.allowed_claim_kinds),
        "request": request_payload,
    }
    context_sha256 = sha256_text(canonical_json(context_identity))
    run_identity_sha256 = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-analysis/v1",
                "store_id": store.store_id,
                "document_id": document_id,
                "action_snapshot_id": action.id,
                "context_sha256": context_sha256,
                "provider_id": validated_selection.provider_id,
                "model_id": validated_selection.model_id,
            }
        )
    )
    attempt = 0
    while True:
        run_id = (
            run_identity_sha256[:32]
            if attempt == 0
            else sha256_text(
                canonical_json(
                    {
                        "domain": "work-buddy.cowork-truth-analysis-attempt/v1",
                        "identity_sha256": run_identity_sha256,
                        "attempt": attempt,
                    }
                )
            )[:32]
        )
        existing_run = truth_analysis_runtime.get_run(run_id)
        if existing_run is None:
            break
        unresolved = _unresolved_review_count(existing_run)
        if unresolved is None or unresolved > 0:
            return analysis_run_view(existing_run, store=store)
        attempt += 1
    _worker_context_contract(
        run_id=run_id,
        document_id=document_id,
        target_text=captured_target,
        target_sha256=action.target_text_sha256,
        target_selector=_mapping(
            captured_view["target"]["selector"], "target.selector"
        ),
        existing_truth=existing_truth,
        source_coverage=coverage,
        allowed_claim_kinds=store.profile.allowed_claim_kinds,
    )
    for prior_run in reversed(
        truth_analysis_runtime.runs_for_document(store.store_id, document_id)
    ):
        unresolved = _unresolved_review_count(prior_run)
        if unresolved is None or unresolved > 0:
            raise TruthAnalysisError(
                "analysis_review_pending",
                (
                    "Finish reviewing the current Truth analysis before starting "
                    "another passage."
                ),
                status=409,
                details={
                    "analysis_run_id": prior_run.run_id,
                    "status": _public_status(prior_run.status),
                    "pending_candidates": unresolved,
                },
            )
    created_at = utc_now()
    expires_at = (
        datetime.now(timezone.utc) + _AUTHORIZATION_TTL
    ).isoformat(timespec="milliseconds")
    authorization = record_model_call_authorization(
        store,
        action_snapshot_id=action.id,
        provider=validated_selection.provider_id,
        model=validated_selection.model_id,
        context_sha256=context_sha256,
        content_boundary={
            "role": "truth_analysis",
            "run_id": run_id,
            "document": "captured_target_and_bounded_truth_context",
            "action_snapshot_id": action.id,
            "web_research": {
                "max_searches": MAX_WEB_SEARCHES,
                "max_results_per_search": MAX_WEB_RESULTS_PER_SEARCH,
                "max_fetches": MAX_WEB_FETCHES,
                "max_fetch_bytes": MAX_WEB_FETCH_BYTES,
                "max_captured_text_bytes": MAX_WEB_CAPTURED_TEXT_BYTES,
                "raw_url_fetch": False,
            },
        },
        egress_class="account_backed_agent",
        cost_ceiling_usd=DEFAULT_TRUTH_ANALYSIS_BUDGET_USD,
        retry_limit=0,
        expires_at=expires_at,
        actor=actor,
        at=created_at,
    )
    session_id = cowork_truth_analysis_session_id(run_id)
    try:
        run = truth_analysis_runtime.create_run(
            run_id=run_id,
            store_id=store.store_id,
            document_id=document_id,
            action_snapshot_id=action.id,
            selection=validated_selection.to_dict(),
            authorization_receipt_id=authorization.id,
            context_sha256=context_sha256,
            request=request_payload,
            session_id=session_id,
            at=created_at,
        )
    except truth_analysis_runtime.TruthAnalysisRunConflict as exc:
        raise TruthAnalysisError(
            "analysis_review_pending",
            str(exc),
            status=409,
            details={
                "analysis_run_id": exc.run_id,
                "status": _public_status(exc.status),
                "pending_candidates": exc.pending_candidates,
            },
        ) from exc
    except sqlite3.IntegrityError:
        run = truth_analysis_runtime.get_run(run_id)
        if run is None:
            raise
    return analysis_run_view(run, store=store)


def _public_status(status: str) -> str:
    if status in {"prepared", "launching"}:
        return "queued"
    if status == "unavailable":
        return "failed"
    return status


def analysis_run_view(
    run: TruthAnalysisRuntimeRun,
    *,
    store: TruthStore | None = None,
) -> dict[str, Any]:
    """Return a safe dashboard projection of one private runtime run."""

    current = truth_analysis_runtime.get_run(run.run_id)
    if current is not None:
        run = current
    resolved_store = store or TruthStoreRegistry().open_store(run.store_id)
    action = _action(resolved_store, run)
    context = _action_context(action)
    target_choice = context.get("target_source")
    if target_choice not in {"current_selection", "working_target"}:
        raise TruthAnalysisError(
            "analysis_context_unavailable",
            "The staged Truth analysis has an unsupported target source.",
            status=409,
        )

    decisions = {
        item.candidate_id: item
        for item in truth_analysis_runtime.candidate_decisions_for_run(run.run_id)
    }
    candidates: list[dict[str, Any]] = []
    if run.output is not None:
        raw_candidates = run.output.get("candidates")
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if not isinstance(item, Mapping):
                    continue
                value = dict(item)
                decision = decisions.get(str(value.get("candidate_id") or ""))
                if decision is None:
                    live_claim = _live_exact_claim(resolved_store, value)
                    staged_match = value.get("existing_claim_match")
                    if (
                        live_claim is not None
                        and not (
                            isinstance(staged_match, Mapping)
                            and staged_match.get("relationship") == "exact"
                            and staged_match.get("claim_id") == live_claim.id
                        )
                        and not _claim_prepared_for_candidate(
                            live_claim,
                            run_id=run.run_id,
                            candidate_id=str(value.get("candidate_id") or ""),
                            candidate_canonical_sha256=str(
                                value.get("canonical_sha256") or ""
                            ),
                        )
                    ):
                        value["existing_claim_match"] = {
                            "claim_id": live_claim.id,
                            "proposition": live_claim.proposition,
                            "claim_kind": live_claim.claim_kind,
                            "status": None,
                            "relationship": "exact",
                            "confidence": 1.0,
                            "rationale": (
                                "Server-detected identical live claim after analysis."
                            ),
                        }
                value["status"] = (
                    "pending"
                    if decision is None
                    else "saved"
                    if decision.decision in {"save_as_proposed", "connect_existing"}
                    else "dismissed"
                )
                if decision is not None:
                    value["decision"] = {
                        "decision": decision.decision,
                        "result": dict(decision.result),
                        "decided_at": decision.decided_at,
                    }
                candidates.append(value)
    return {
        "ok": True,
        "schema": "wb.cowork.truth-analysis-run/v1",
        "analysis_run_id": run.run_id,
        "status": _public_status(run.status),
        "store_id": run.store_id,
        "document_id": run.document_id,
        "action_snapshot_id": run.action_snapshot_id,
        "context_sha256": run.context_sha256,
        "target_choice": target_choice,
        "target_label": str(context.get("target_label") or "Selected passage"),
        "captured_at": action.created_at,
        "structured_head_sha256": action.structured_head_sha256,
        "projection_sha256": action.projection_sha256,
        "execution": dict(run.selection),
        "source_coverage": _current_source_coverage(run),
        "reported_source_coverage": (
            []
            if run.output is None
            else list(run.output.get("reported_source_coverage", []))
        ),
        "summary": "" if run.output is None else str(run.output.get("summary") or ""),
        "limitations": (
            [] if run.output is None else list(run.output.get("limitations", []))
        ),
        "candidates": candidates,
        "error": run.error or None if run.status in {"unavailable", "failed"} else None,
        "error_code": run.error_code or None,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": (
            run.updated_at
            if run.status in {"completed", "unavailable", "failed"}
            else None
        ),
    }


def _bound_run(
    run_id: str,
    agent_session_id: str | None,
) -> TruthAnalysisRuntimeRun:
    run = truth_analysis_runtime.get_run(run_id)
    if run is None:
        raise TruthAnalysisError(
            "analysis_run_not_found", "Truth analysis run does not exist.", status=404
        )
    if not agent_session_id or agent_session_id != run.session_id:
        raise TruthAnalysisError(
            "analysis_worker_binding_mismatch",
            "This worker is not authorized for that Truth analysis run.",
            status=403,
        )
    if run.error_code == "execution_deadline_exceeded":
        raise TruthAnalysisError(
            "analysis_run_terminal",
            "This Truth analysis run exceeded its execution deadline.",
            status=409,
            retryable=True,
        )
    return run


def _output_schema(allowed_claim_kinds: Sequence[str]) -> dict[str, Any]:
    """Return the exact worker-visible submission contract.

    This is deliberately explicit rather than a loose example: the isolated
    worker has no source-code access and must be able to construct a valid
    payload from this context alone.  Server validation remains authoritative.
    """

    selector = {
        "type": "object",
        "required": ["exact"],
        "properties": {
            "exact": "nonempty exact quote within the named captured text",
            "prefix": "optional immediately preceding context",
            "suffix": "optional immediately following context",
            "start": "optional zero-based code-point start offset",
            "end": "optional zero-based exclusive code-point end offset",
        },
    }
    all_evidence_relationships = sorted(_EVIDENCE_RELATIONSHIPS)
    return {
        "contract_kind": "json_object",
        "schema_literal": ANALYSIS_OUTPUT_SCHEMA,
        "maximum_normalized_serialized_bytes": MAX_NORMALIZED_OUTPUT_BYTES,
        "root": {
            "required": ["schema", "source_coverage", "candidates"],
            "optional": ["summary", "limitations"],
            "properties": {
                "schema": {"const": ANALYSIS_OUTPUT_SCHEMA},
                "summary": {
                    "type": "string",
                    "maximum_characters": MAX_SUMMARY_CHARS,
                    "default": "",
                },
                "limitations": {
                    "type": "array",
                    "items": "string",
                    "max_items": 20,
                    "maximum_item_characters": MAX_LIMITATION_CHARS,
                    "default": [],
                },
                "source_coverage": {
                    "type": "array",
                    "exact_sources": [
                        "selected_passage",
                        "existing_truth",
                        "web",
                    ],
                    "item": {
                        "required": [
                            "source",
                            "status",
                            "detail",
                            "external_egress",
                        ],
                        "properties": {
                            "source": {
                                "enum": [
                                    "selected_passage",
                                    "existing_truth",
                                    "web",
                                ]
                            },
                            "status": {"enum": sorted(_COVERAGE_STATUSES)},
                            "detail": {
                                "type": "string",
                                "maximum_characters": MAX_COVERAGE_DETAIL_CHARS,
                            },
                            "external_egress": {"type": "boolean"},
                        },
                        "rule": (
                            "Report each source exactly once. Keep supplied when it "
                            "was not inspected; after inspection promote selected_passage "
                            "to searched and existing_truth to searched, or partial when "
                            "its supplied context says truncated. Copy web status and all "
                            "external_egress booleans from current source_coverage."
                        ),
                    },
                },
                "candidates": {
                    "type": "array",
                    "max_items": MAX_CANDIDATES,
                    "items": "candidate",
                },
            },
        },
        "candidate": {
            "required": [
                "proposition",
                "claim_kind",
                "confidence_extraction",
                "expression",
            ],
            "optional": [
                "structured",
                "existing_claim_match",
                "evidence",
                "limitations",
            ],
            "properties": {
                "proposition": "one atomic nonempty proposition, at most 4000 characters",
                "claim_kind": {"enum": list(allowed_claim_kinds)},
                "structured": (
                    "optional object satisfying the selected claim kind's profile"
                ),
                "confidence_extraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "expression": {
                    "required": ["role", "selector"],
                    "properties": {
                        "role": {"enum": sorted(EXPRESSION_ROLES)},
                        "selector": selector,
                    },
                    "rule": "selector locates how target.text expresses this claim",
                },
                "existing_claim_match": {
                    "nullable": True,
                    "required_when_object": [
                        "claim_id",
                        "relationship",
                        "confidence",
                    ],
                    "optional_when_object": ["rationale"],
                    "properties": {
                        "claim_id": "an id present in existing_truth.claims",
                        "relationship": {
                            "enum": sorted(_MATCH_RELATIONSHIPS)
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "rationale": {
                            "type": "string",
                            "maximum_characters": MAX_RATIONALE_CHARS,
                        },
                    },
                    "default": None,
                },
                "evidence": {
                    "type": "array",
                    "max_items": MAX_EVIDENCE_PER_CANDIDATE,
                    "items": "one evidence_variants object",
                    "default": [],
                },
                "limitations": {
                    "type": "array",
                    "items": "string",
                    "max_items": 20,
                    "maximum_item_characters": MAX_LIMITATION_CHARS,
                    "default": [],
                },
            },
        },
        "evidence_variants": {
            "truth_span": {
                "required": ["source_kind", "relationship", "span_id"],
                "optional": ["evidence_id", "rationale"],
                "properties": {
                    "source_kind": {"const": "truth_span"},
                    "relationship": {"enum": all_evidence_relationships},
                    "span_id": "an id present in existing_truth.receipts",
                    "evidence_id": "optional matching recorded evidence id",
                    "rationale": {
                        "type": "string",
                        "maximum_characters": MAX_RATIONALE_CHARS,
                    },
                },
            },
            "web_fetch": {
                "required": [
                    "source_kind",
                    "relationship",
                    "fetch_id",
                    "selector",
                ],
                "optional": ["rationale"],
                "properties": {
                    "source_kind": {"const": "web_fetch"},
                    "relationship": {"enum": all_evidence_relationships},
                    "fetch_id": "a completed fetch_id issued by this run",
                    "selector": {
                        **selector,
                        "rule": "locates the assessed quote in fetched text",
                    },
                    "rationale": {
                        "type": "string",
                        "maximum_characters": MAX_RATIONALE_CHARS,
                    },
                },
            },
            "passage_citation": {
                "required": ["source_kind", "relationship", "selector"],
                "optional": ["source_locator", "rationale"],
                "properties": {
                    "source_kind": {"const": "passage_citation"},
                    "relationship": {"enum": ["mentions", "inconclusive"]},
                    "selector": {
                        **selector,
                        "rule": "locates the citation marker in target.text",
                    },
                    "source_locator": {
                        "type": "string",
                        "maximum_characters": MAX_SOURCE_LOCATOR_CHARS,
                    },
                    "rationale": {
                        "type": "string",
                        "maximum_characters": MAX_RATIONALE_CHARS,
                    },
                },
                "rule": "citation markers are unattested and never supporting evidence",
            },
        },
        "submission_template": {
            "schema": ANALYSIS_OUTPUT_SCHEMA,
            "summary": "",
            "limitations": [],
            "source_coverage": [
                {
                    "source": source,
                    "status": "copy from current source_coverage",
                    "detail": "state exactly what was inspected",
                    "external_egress": "copy boolean from current source_coverage",
                }
                for source in ("selected_passage", "existing_truth", "web")
            ],
            "candidates": [],
        },
    }


def _worker_context_contract(
    *,
    run_id: str,
    document_id: str,
    target_text: str,
    target_sha256: str,
    target_selector: Mapping[str, Any],
    existing_truth: Mapping[str, Any],
    source_coverage: Sequence[Mapping[str, Any]],
    allowed_claim_kinds: Sequence[str],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ok": True,
        "schema": ANALYSIS_CONTEXT_SCHEMA,
        "analysis_run_id": run_id,
        "document": {"document_id": document_id},
        "target": {
            "kind": "text_quote",
            "text": target_text,
            "text_sha256": target_sha256,
            "selector": dict(target_selector),
        },
        "existing_truth": dict(existing_truth),
        "source_coverage": [dict(item) for item in source_coverage],
        "web_tools": {
            "max_searches": MAX_WEB_SEARCHES,
            "max_results_per_search": MAX_WEB_RESULTS_PER_SEARCH,
            "max_fetches": MAX_WEB_FETCHES,
            "max_fetch_bytes": MAX_WEB_FETCH_BYTES,
            "max_captured_text_bytes": MAX_WEB_CAPTURED_TEXT_BYTES,
            "truncation_metadata": (
                "Each completed fetch reports acquisition_metadata.text_truncated, "
                "captured_text_bytes, extracted_text_bytes, and full_extracted_text_sha256."
            ),
            "fetch_policy": (
                "Only a hit returned by this run may be fetched. Every destination "
                "and redirect must pass the public-network guard; arbitrary URLs are "
                "never accepted from the worker."
            ),
        },
        "output_schema": _output_schema(allowed_claim_kinds),
        "context_limits": {
            "maximum_serialized_bytes": MAX_WORKER_CONTEXT_BYTES,
            "maximum_selected_passage_bytes": MAX_SELECTED_PASSAGE_BYTES,
            "existing_truth_byte_budget": MAX_EXISTING_TRUTH_CONTEXT_BYTES,
            "serialized_bytes": 0,
        },
    }
    for _ in range(3):
        value["context_limits"]["serialized_bytes"] = len(
            canonical_json(value).encode("utf-8")
        )
    serialized_bytes = len(canonical_json(value).encode("utf-8"))
    if serialized_bytes > MAX_WORKER_CONTEXT_BYTES:
        raise TruthAnalysisError(
            "analysis_context_too_large",
            "The bounded Truth analysis context is too large for safe delivery.",
            status=413,
            details={
                "serialized_context_bytes": serialized_bytes,
                "maximum_serialized_context_bytes": MAX_WORKER_CONTEXT_BYTES,
            },
        )
    value["context_limits"]["serialized_bytes"] = serialized_bytes
    return value


def get_worker_context(
    *,
    run_id: str,
    agent_session_id: str | None,
    disclosure_boundary: (
        truth_analysis_disclosure.TruthAnalysisDisclosureBoundary | None
    ) = None,
) -> dict[str, Any]:
    """Return only the exact target and bounded Truth context for this worker."""

    run = _bound_run(run_id, agent_session_id)
    store = TruthStoreRegistry().open_store(run.store_id)
    action = _action(store, run)
    snapshot = action_snapshot_view(
        store,
        document_id=run.document_id,
        action_snapshot_id=action.id,
    )
    existing_truth = _mapping(run.request.get("existing_truth"), "existing_truth")
    identity = {
        "schema": ANALYSIS_CONTEXT_SCHEMA,
        "action_snapshot_id": action.id,
        "target_text_sha256": action.target_text_sha256,
        "allowed_claim_kinds": list(store.profile.allowed_claim_kinds),
        "request": dict(run.request),
    }
    if sha256_text(canonical_json(identity)) != run.context_sha256:
        raise TruthAnalysisError(
            "analysis_context_changed",
            "The staged Truth analysis context failed integrity validation.",
            status=409,
        )
    context = _worker_context_contract(
        run_id=run.run_id,
        document_id=run.document_id,
        target_text=str(snapshot["target"]["text"]),
        target_sha256=action.target_text_sha256,
        target_selector=_mapping(snapshot["target"]["selector"], "target.selector"),
        existing_truth=existing_truth,
        source_coverage=_current_source_coverage(run),
        allowed_claim_kinds=store.profile.allowed_claim_kinds,
    )
    boundary = (
        disclosure_boundary
        or truth_analysis_disclosure.configured_truth_analysis_disclosure()
    )
    if boundary is not None:
        try:
            from work_buddy.security.local_identity import get_default_authority

            _source_store, target_source_ref, _representation_id = (
                _capture_analysis_source(
                    run,
                    action,
                    human=get_default_authority().enrolled_actor(),
                )
            )
            truth_analysis_disclosure.account_worker_context(
                boundary,
                run,
                context,
                target_derivation_ref=target_source_ref.uri,
            )
        except Exception as exc:
            raise _disclosure_failure(exc) from exc
    return context


def search_web(
    *,
    run_id: str,
    query: str,
    agent_session_id: str | None,
    disclosure_boundary: (
        truth_analysis_disclosure.TruthAnalysisDisclosureBoundary | None
    ) = None,
) -> dict[str, Any]:
    """Run one replay-safe query through the guarded research broker."""

    run = _bound_run(run_id, agent_session_id)
    normalized_query = truth_analysis_research.normalize_query(query)

    def search() -> truth_analysis_research.ResearchSearchResult:
        return truth_analysis_research.search(
            run_id=run_id,
            query=normalized_query,
            agent_session_id=agent_session_id,
        )

    boundary = (
        disclosure_boundary
        or truth_analysis_disclosure.configured_truth_analysis_disclosure()
    )
    try:
        result = (
            boundary.execute_outbound(
                run,
                exact_content=normalized_query.encode("utf-8"),
                source_role="agent_output",
                tool_call_id=(
                    "truth-analysis-search:"
                    f"{sha256_text(normalized_query)[:32]}"
                ),
                idempotency_key=(
                    "truth-analysis-search-query:"
                    f"{sha256_text(normalized_query)}"
                ),
                recipient="web_search_provider",
                provider_id="websearch",
                call=search,
                external_egress=lambda item: item.external_egress,
            )
            if boundary is not None
            else search()
        )
    except Exception as exc:
        if boundary is not None and not isinstance(
            exc, truth_analysis_research.TruthAnalysisResearchError
        ):
            raise _disclosure_failure(exc) from exc
        raise
    response = {
        "ok": result.status == "completed",
        "analysis_run_id": run.run_id,
        **result.to_dict(),
        "source_coverage": _current_source_coverage(run),
    }
    if boundary is not None:
        try:
            boundary.account_inbound(
                run,
                payload=response,
                source_role="derived_content",
                tool_call_id=f"truth-analysis-search-response:{result.search_id}",
                idempotency_key=(
                    f"truth-analysis-search-response:{result.search_id}"
                ),
            )
        except Exception as exc:
            raise _disclosure_failure(exc) from exc
    return response


def fetch_search_hit(
    *,
    run_id: str,
    hit_id: str,
    agent_session_id: str | None,
    disclosure_boundary: (
        truth_analysis_disclosure.TruthAnalysisDisclosureBoundary | None
    ) = None,
) -> dict[str, Any]:
    """Fetch one admitted hit through the guarded public-network broker."""

    run = _bound_run(run_id, agent_session_id)
    admitted_hit = truth_analysis_runtime.search_hit_for_run(run.run_id, hit_id)
    if admitted_hit is None:
        # Preserve the research broker's typed rejection and avoid capturing a
        # caller-controlled arbitrary URL/source.
        return truth_analysis_research.fetch_search_hit(
            run_id=run_id,
            hit_id=hit_id,
            agent_session_id=agent_session_id,
        )
    admitted_url = str(admitted_hit.get("url") or "")

    def fetch() -> truth_analysis_research.ResearchFetchResult:
        return truth_analysis_research.fetch(
            run_id=run_id,
            hit_id=hit_id,
            agent_session_id=agent_session_id,
        )

    boundary = (
        disclosure_boundary
        or truth_analysis_disclosure.configured_truth_analysis_disclosure()
    )
    try:
        result = (
            boundary.execute_outbound(
                run,
                exact_content=admitted_url.encode("utf-8"),
                source_role="derived_content",
                tool_call_id=f"truth-analysis-fetch:{hit_id}",
                idempotency_key=f"truth-analysis-fetch-request:{hit_id}",
                recipient="public_web_origin",
                provider_id="direct_http",
                call=fetch,
                external_egress=lambda item: item.receipt.external_egress,
            )
            if boundary is not None
            else fetch()
        )
    except Exception as exc:
        if boundary is not None and not isinstance(
            exc, truth_analysis_research.TruthAnalysisResearchError
        ):
            raise _disclosure_failure(exc) from exc
        raise
    response = {
        "ok": result.receipt.status == "completed",
        "analysis_run_id": run.run_id,
        **result.to_dict(),
        "source_coverage": _current_source_coverage(run),
    }
    if boundary is not None:
        try:
            boundary.account_inbound(
                run,
                payload=response,
                source_role="fetched_passage",
                tool_call_id=(
                    f"truth-analysis-fetch-response:{result.receipt.fetch_id}"
                ),
                idempotency_key=(
                    f"truth-analysis-fetch-response:{result.receipt.fetch_id}"
                ),
            )
        except Exception as exc:
            raise _disclosure_failure(exc) from exc
    return response


def _normalized_coverage(
    run: TruthAnalysisRuntimeRun,
    value: object,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list):
        raise TruthAnalysisError(
            "invalid_output", "source_coverage must be an array"
        )
    authoritative = {
        str(item.get("source")): dict(item)
        for item in _current_source_coverage(run)
        if isinstance(item, Mapping)
    }
    reported: dict[str, dict[str, Any]] = {}
    for raw in value:
        item = _mapping(raw, "source_coverage item")
        source = _required_text(item.get("source"), "source_coverage.source", maximum=80)
        status = item.get("status")
        if status not in _COVERAGE_STATUSES:
            raise TruthAnalysisError(
                "invalid_output", "source_coverage.status is not supported"
            )
        external = item.get("external_egress")
        if not isinstance(external, bool):
            raise TruthAnalysisError(
                "invalid_output", "source_coverage.external_egress must be boolean"
            )
        if source in reported:
            raise TruthAnalysisError(
                "invalid_output", "source_coverage contains a duplicate source"
            )
        reported[source] = {
            "source": source,
            "status": status,
            "detail": _optional_text(
                item.get("detail"),
                "source_coverage.detail",
                maximum=MAX_COVERAGE_DETAIL_CHARS,
            ),
            "external_egress": external,
        }
    if set(reported) != set(authoritative):
        raise TruthAnalysisError(
            "invalid_output",
            "source_coverage must report selected_passage, existing_truth, and web",
        )
    resolved = {key: dict(item) for key, item in authoritative.items()}
    existing_truth = _mapping(run.request.get("existing_truth"), "existing_truth")
    for source, actual in authoritative.items():
        observed = reported[source]
        allowed_statuses = {actual.get("status")}
        if source == "selected_passage" and actual.get("status") == "supplied":
            allowed_statuses.add("searched")
        elif source == "existing_truth" and actual.get("status") == "supplied":
            allowed_statuses.add(
                "partial"
                if existing_truth.get("truncated")
                else "searched"
            )
        if observed["status"] not in allowed_statuses or observed[
            "external_egress"
        ] != actual.get("external_egress"):
            raise TruthAnalysisError(
                "invalid_output",
                f"source_coverage overstates the work performed for {source}",
            )
        if source in {"selected_passage", "existing_truth"}:
            resolved[source] = dict(observed)
    return list(resolved.values()), [reported[key] for key in sorted(reported)]


def _target_and_projection(
    store: TruthStore,
    run: TruthAnalysisRuntimeRun,
) -> tuple[ActionSnapshot, str, str, int]:
    action = _action(store, run)
    snapshot = action_snapshot_view(
        store,
        document_id=run.document_id,
        action_snapshot_id=action.id,
    )
    target = str(snapshot["target"]["text"])
    projection = str(snapshot["frozen_markdown"])
    try:
        target_payload = json.loads(action.target_selector_json)
        target_start = int(target_payload["resolved"]["start"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TruthAnalysisError(
            "analysis_context_unavailable",
            "The frozen passage selector is unavailable.",
            status=409,
        ) from exc
    if projection[target_start : target_start + len(target)] != target:
        raise TruthAnalysisError(
            "analysis_context_changed",
            "The frozen passage no longer matches its projection.",
            status=409,
        )
    return action, target, projection, target_start


def _normalize_expression(
    raw: object,
    *,
    target: str,
    projection: str,
    target_start: int,
    target_sha256: str,
) -> dict[str, Any]:
    expression = _mapping(raw, "candidate.expression")
    role = expression.get("role")
    if role not in EXPRESSION_ROLES:
        raise TruthAnalysisError(
            "invalid_output", "candidate.expression.role is not supported"
        )
    selector_value = _mapping(
        expression.get("selector"), "candidate.expression.selector"
    )
    try:
        local = CompositeSelector(
            exact=selector_value.get("exact"),
            prefix=selector_value.get("prefix", ""),
            suffix=selector_value.get("suffix", ""),
            start=selector_value.get("start"),
            end=selector_value.get("end"),
        )
        resolved = reanchor(
            target, local, expected_snapshot_sha256=target_sha256
        )
    except InvariantViolation as exc:
        raise TruthAnalysisError(
            "invalid_output",
            f"candidate expression does not locate in the frozen target: {exc}",
        ) from exc
    start = target_start + resolved.start
    end = target_start + resolved.end
    document_selector = {
        "exact": resolved.exact,
        "prefix": projection[max(0, start - 96) : start],
        "suffix": projection[end : min(len(projection), end + 96)],
        "start": start,
        "end": end,
    }
    return {
        "role": str(role),
        "quote": resolved.exact,
        "selector": document_selector,
        "target_selector": {
            "exact": resolved.exact,
            "prefix": local.prefix,
            "suffix": local.suffix,
            "start": resolved.start,
            "end": resolved.end,
        },
    }


def _normalize_existing_match(
    raw: object,
    *,
    proposition: str,
    claim_kind: str,
    claims: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    exact_matches = [
        value
        for value in claims.values()
        if value.get("proposition") == proposition
        and value.get("claim_kind") == claim_kind
        and value.get("base_status") not in TERMINAL_STATUSES
    ]
    if raw is None:
        if len(exact_matches) == 1:
            match = exact_matches[0]
            return {
                "claim_id": match["claim_id"],
                "proposition": match["proposition"],
                "claim_kind": match["claim_kind"],
                "status": match.get("base_status"),
                "relationship": "exact",
                "confidence": 1.0,
                "rationale": "Server-detected identical proposition and claim kind.",
            }
        return None
    value = _mapping(raw, "candidate.existing_claim_match")
    claim_id = _required_text(
        value.get("claim_id"), "existing_claim_match.claim_id", maximum=64
    )
    if claim_id not in claims:
        raise TruthAnalysisError(
            "invalid_output", "existing_claim_match names a claim outside job context"
        )
    relationship = value.get("relationship")
    if relationship not in _MATCH_RELATIONSHIPS:
        raise TruthAnalysisError(
            "invalid_output", "existing_claim_match.relationship is not supported"
        )
    if relationship == "exact" and claims[claim_id].get("proposition") != proposition:
        raise TruthAnalysisError(
            "invalid_output", "an exact existing claim match must have identical text"
        )
    if exact_matches and all(item.get("claim_id") != claim_id for item in exact_matches):
        raise TruthAnalysisError(
            "invalid_output", "existing_claim_match ignores an exact existing claim"
        )
    return {
        "claim_id": claim_id,
        "proposition": claims[claim_id].get("proposition"),
        "claim_kind": claims[claim_id].get("claim_kind"),
        "status": claims[claim_id].get("base_status"),
        "relationship": str(relationship),
        "confidence": _confidence(value.get("confidence"), "existing_claim_match.confidence"),
        "rationale": _optional_text(
            value.get("rationale"),
            "existing_claim_match.rationale",
            maximum=MAX_RATIONALE_CHARS,
        ),
    }


def _normalize_evidence(
    raw: object,
    *,
    target: str,
    target_sha256: str,
    receipts: Mapping[str, Mapping[str, Any]],
    fetches: Mapping[str, truth_analysis_runtime.TruthAnalysisFetchReceipt],
    research_fetches: Mapping[
        str, truth_analysis_research.ResearchFetchReceipt
    ],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_EVIDENCE_PER_CANDIDATE:
        raise TruthAnalysisError(
            "invalid_output",
            f"candidate.evidence must contain at most {MAX_EVIDENCE_PER_CANDIDATE} items",
        )
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_item in raw:
        item = _mapping(raw_item, "candidate.evidence item")
        relationship = item.get("relationship")
        if relationship not in _EVIDENCE_RELATIONSHIPS:
            raise TruthAnalysisError(
                "invalid_output", "candidate evidence relationship is not supported"
            )
        source_kind = item.get("source_kind")
        if source_kind == "truth_span":
            span_id = _required_text(
                item.get("span_id"), "candidate.evidence.span_id", maximum=64
            )
            receipt = receipts.get(span_id)
            if receipt is None:
                raise TruthAnalysisError(
                    "invalid_output",
                    "candidate evidence names a Truth span outside job context",
                )
            supplied_evidence_id = item.get("evidence_id")
            if (
                supplied_evidence_id is not None
                and supplied_evidence_id != receipt.get("evidence_id")
            ):
                raise TruthAnalysisError(
                    "invalid_output", "candidate evidence span/evidence ids disagree"
                )
            quote = str(receipt.get("quote") or "")
            value = {
                "source_kind": "truth_span",
                "relationship": str(relationship),
                "attachable": relationship
                in {"supports", "partially_supports"},
                "span_id": span_id,
                "evidence_id": str(receipt.get("evidence_id") or ""),
                "quote": quote,
                "source_locator": str(receipt.get("source_locator") or ""),
                "trust_class": str(receipt.get("trust_class") or "unattested"),
                "integrity": {
                    "status": "recorded",
                    "content_sha256": str(receipt.get("content_sha256") or ""),
                    "span_sha256": str(receipt.get("span_sha256") or ""),
                },
                "rationale": _optional_text(
                    item.get("rationale"),
                    "candidate.evidence.rationale",
                    maximum=MAX_RATIONALE_CHARS,
                ),
            }
            identity = ("truth_span", span_id, str(relationship))
        elif source_kind == "web_fetch":
            fetch_id = _required_text(
                item.get("fetch_id"), "candidate.evidence.fetch_id", maximum=64
            )
            fetch = fetches.get(fetch_id)
            if fetch is None or fetch.status != "completed" or not fetch.text:
                raise TruthAnalysisError(
                    "invalid_output",
                    "candidate evidence names an unavailable web fetch",
                )
            if sha256_text(fetch.text) != fetch.content_sha256:
                raise TruthAnalysisError(
                    "analysis_context_changed",
                    "captured web evidence failed integrity validation",
                    status=409,
                )
            selector_value = item.get("selector")
            if not isinstance(selector_value, Mapping):
                selector_value = {"exact": item.get("quote")}
            try:
                selector = CompositeSelector(
                    exact=selector_value.get("exact"),
                    prefix=selector_value.get("prefix", ""),
                    suffix=selector_value.get("suffix", ""),
                    start=selector_value.get("start"),
                    end=selector_value.get("end"),
                )
                anchored = reanchor(
                    fetch.text,
                    selector,
                    expected_snapshot_sha256=fetch.content_sha256,
                )
            except InvariantViolation as exc:
                raise TruthAnalysisError(
                    "invalid_output",
                    f"web evidence quote does not locate in its captured fetch: {exc}",
                ) from exc
            research_fetch = research_fetches.get(fetch_id)
            if (
                research_fetch is not None
                and (
                    research_fetch.status != "completed"
                    or research_fetch.text_sha256 != fetch.content_sha256
                    or research_fetch.exact_text != fetch.text
                )
            ):
                raise TruthAnalysisError(
                    "analysis_context_changed",
                    "captured web acquisition metadata failed integrity validation",
                    status=409,
                )
            captured_bytes = len(fetch.text.encode("utf-8"))
            acquisition = (
                {}
                if research_fetch is None
                else dict(research_fetch.acquisition_metadata)
            )
            captured_text_sha256 = str(
                acquisition.get("captured_text_sha256") or fetch.content_sha256
            )
            if captured_text_sha256 != fetch.content_sha256:
                raise TruthAnalysisError(
                    "analysis_context_changed",
                    "captured web acquisition digest no longer matches its text",
                    status=409,
                )
            capture_integrity = {
                "text_truncated": bool(acquisition.get("text_truncated", False)),
                "captured_text_bytes": int(
                    acquisition.get("captured_text_bytes", captured_bytes)
                ),
                "extracted_text_bytes": int(
                    acquisition.get("extracted_text_bytes", captured_bytes)
                ),
                "captured_text_sha256": captured_text_sha256,
                "full_extracted_text_sha256": str(
                    acquisition.get("full_extracted_text_sha256")
                    or fetch.content_sha256
                ),
                "maximum_captured_text_bytes": MAX_WEB_CAPTURED_TEXT_BYTES,
            }
            if (
                capture_integrity["captured_text_bytes"] != captured_bytes
                or capture_integrity["extracted_text_bytes"]
                < capture_integrity["captured_text_bytes"]
                or (
                    capture_integrity["text_truncated"]
                    and capture_integrity["extracted_text_bytes"]
                    <= capture_integrity["captured_text_bytes"]
                )
            ):
                raise TruthAnalysisError(
                    "analysis_context_changed",
                    "captured web acquisition byte metadata is inconsistent",
                    status=409,
                )
            value = {
                "source_kind": "web_fetch",
                "relationship": str(relationship),
                "attachable": relationship in {"supports", "partially_supports"},
                "fetch_id": fetch.fetch_id,
                "quote": anchored.exact,
                "selector": {
                    "exact": anchored.exact,
                    "prefix": fetch.text[max(0, anchored.start - 96) : anchored.start],
                    "suffix": fetch.text[
                        anchored.end : min(len(fetch.text), anchored.end + 96)
                    ],
                    "start": anchored.start,
                    "end": anchored.end,
                },
                "source_locator": fetch.canonical_url or fetch.url,
                "source_title": fetch.title,
                "trust_class": "external_quarantined",
                "integrity": {
                    "status": "captured_runtime",
                    "content_sha256": fetch.content_sha256,
                    "fetch_id": fetch.fetch_id,
                    "extractor": fetch.extractor,
                    "capture": capture_integrity,
                },
                "rationale": _optional_text(
                    item.get("rationale"),
                    "candidate.evidence.rationale",
                    maximum=MAX_RATIONALE_CHARS,
                ),
            }
            identity = ("web_fetch", fetch.fetch_id, str(relationship))
        elif source_kind == "passage_citation":
            if relationship not in {"mentions", "inconclusive"}:
                raise TruthAnalysisError(
                    "invalid_output",
                    "a passage citation can only be marked mentions or inconclusive",
                )
            quote_selector = item.get("selector")
            if isinstance(quote_selector, Mapping):
                selector_value = quote_selector
            else:
                selector_value = {"exact": item.get("quote")}
            try:
                selector = CompositeSelector(
                    exact=selector_value.get("exact"),
                    prefix=selector_value.get("prefix", ""),
                    suffix=selector_value.get("suffix", ""),
                    start=selector_value.get("start"),
                    end=selector_value.get("end"),
                )
                anchored = reanchor(
                    target,
                    selector,
                    expected_snapshot_sha256=target_sha256,
                )
            except InvariantViolation as exc:
                raise TruthAnalysisError(
                    "invalid_output",
                    f"passage citation does not locate in the target: {exc}",
                ) from exc
            locator = _optional_text(
                item.get("source_locator") or anchored.exact,
                "candidate.evidence.source_locator",
                maximum=MAX_SOURCE_LOCATOR_CHARS,
            )
            value = {
                "source_kind": "passage_citation",
                "relationship": str(relationship),
                "attachable": False,
                "quote": anchored.exact,
                "source_locator": locator,
                "trust_class": "unattested",
                "integrity": {"status": "unresolved"},
                "rationale": _optional_text(
                    item.get("rationale"),
                    "candidate.evidence.rationale",
                    maximum=MAX_RATIONALE_CHARS,
                ),
            }
            identity = ("passage_citation", anchored.exact, str(relationship))
        else:
            raise TruthAnalysisError(
                "invalid_output",
                "candidate evidence source_kind must be truth_span, web_fetch, or passage_citation",
            )
        if identity in seen:
            raise TruthAnalysisError(
                "invalid_output", "candidate evidence contains a duplicate relationship"
            )
        seen.add(identity)
        normalized.append(value)
    return normalized


def _text_list(
    value: object,
    label: str,
    *,
    maximum: int = 20,
    maximum_item_chars: int = MAX_LIMITATION_CHARS,
) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or not all(
            isinstance(item, str) and len(item) <= maximum_item_chars
            for item in value
        )
    ):
        raise TruthAnalysisError(
            "invalid_output",
            f"{label} must be an array of at most {maximum} bounded strings",
        )
    return [str(item) for item in value]


def _normalize_worker_output(
    store: TruthStore,
    run: TruthAnalysisRuntimeRun,
    payload: Mapping[str, Any],
    *,
    input_manifest: ManifestDigest | None = None,
) -> dict[str, Any]:
    if payload.get("schema") != ANALYSIS_OUTPUT_SCHEMA:
        raise TruthAnalysisError("invalid_output", "unsupported Truth analysis output schema")
    candidates_value = payload.get("candidates")
    if not isinstance(candidates_value, list) or len(candidates_value) > MAX_CANDIDATES:
        raise TruthAnalysisError(
            "invalid_output",
            f"candidates must be an array of at most {MAX_CANDIDATES} items",
        )
    coverage, reported_coverage = _normalized_coverage(
        run, payload.get("source_coverage")
    )
    action, target, projection, target_start = _target_and_projection(store, run)
    existing_truth = _mapping(run.request.get("existing_truth"), "existing_truth")
    claims = {
        str(item.get("claim_id")): dict(item)
        for item in existing_truth.get("claims", [])
        if isinstance(item, Mapping) and isinstance(item.get("claim_id"), str)
    }
    receipts = {
        str(item.get("span_id")): dict(item)
        for item in existing_truth.get("receipts", [])
        if isinstance(item, Mapping) and isinstance(item.get("span_id"), str)
    }
    fetches = {
        item.fetch_id: item
        for item in truth_analysis_runtime.fetch_receipts_for_run(run.run_id)
    }
    research_fetches = {
        item.fetch_id: item
        for item in truth_analysis_research.receipts_for_run(
            run_id=run.run_id,
            agent_session_id=run.session_id,
        )
    }
    normalized_candidates: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    duplicate_count = 0
    for index, raw_candidate in enumerate(candidates_value):
        candidate = _mapping(raw_candidate, f"candidates[{index}]")
        proposition = _required_text(
            candidate.get("proposition"),
            f"candidates[{index}].proposition",
            maximum=4_000,
        ).strip()
        claim_kind = _required_text(
            candidate.get("claim_kind"),
            f"candidates[{index}].claim_kind",
            maximum=120,
        )
        structured = candidate.get("structured")
        if structured is not None and not isinstance(structured, Mapping):
            raise TruthAnalysisError(
                "invalid_output", "candidate.structured must be an object when supplied"
            )
        try:
            validate_new_claim(
                store.profile,
                claim_kind=claim_kind,
                structured=structured,
            )
        except InvariantViolation as exc:
            raise TruthAnalysisError("invalid_output", str(exc)) from exc
        expression = _normalize_expression(
            candidate.get("expression"),
            target=target,
            projection=projection,
            target_start=target_start,
            target_sha256=action.target_text_sha256,
        )
        existing_match = _normalize_existing_match(
            candidate.get("existing_claim_match"),
            proposition=proposition,
            claim_kind=claim_kind,
            claims=claims,
        )
        evidence = _normalize_evidence(
            candidate.get("evidence"),
            target=target,
            target_sha256=action.target_text_sha256,
            receipts=receipts,
            fetches=fetches,
            research_fetches=research_fetches,
        )
        semantic_claim_sha256 = claim_sha256(
            proposition=proposition,
            claim_kind=claim_kind,
            structured=None if structured is None else dict(structured),
            scope="store",
            valid_from=None,
            valid_to=None,
        )
        if semantic_claim_sha256 in seen_claims:
            duplicate_count += 1
            continue
        seen_claims.add(semantic_claim_sha256)
        normalized = {
            "proposition": proposition,
            "claim_kind": claim_kind,
            "structured": None if structured is None else dict(structured),
            "confidence_extraction": _confidence(
                candidate.get("confidence_extraction"),
                f"candidates[{index}].confidence_extraction",
            ),
            "expression": expression,
            "existing_claim_match": existing_match,
            "evidence": evidence,
            "source_coverage": coverage,
            "limitations": _text_list(
                candidate.get("limitations"), f"candidates[{index}].limitations"
            ),
        }
        if input_manifest is not None:
            normalized["input_manifest_sha256"] = input_manifest.manifest_sha256
        canonical_sha256 = sha256_text(canonical_json(normalized))
        candidate_id = sha256_text(
            canonical_json(
                {
                    "domain": "work-buddy.cowork-truth-candidate/v1",
                    "run_id": run.run_id,
                    "index": index,
                    "canonical_sha256": canonical_sha256,
                }
            )
        )[:32]
        evidence_with_ids = [
            {
                "evidence_candidate_id": sha256_text(
                    canonical_json(
                        {
                            "domain": "work-buddy.cowork-truth-evidence-candidate/v1",
                            "run_id": run.run_id,
                            "candidate_id": candidate_id,
                            "candidate_canonical_sha256": canonical_sha256,
                            "index": evidence_index,
                            "evidence": item,
                        }
                    )
                )[:32],
                **item,
            }
            for evidence_index, item in enumerate(evidence)
        ]
        normalized_candidates.append(
            {
                "candidate_id": candidate_id,
                "canonical_sha256": canonical_sha256,
                **normalized,
                "evidence": evidence_with_ids,
            }
        )
    limitations = _text_list(payload.get("limitations"), "limitations")
    if duplicate_count:
        limitations.append(
            f"The server removed {duplicate_count} duplicate staged "
            f"candidate{'s' if duplicate_count != 1 else ''}."
        )
    normalized_output = {
        "schema": ANALYSIS_OUTPUT_SCHEMA,
        "summary": _optional_text(
            payload.get("summary"), "summary", maximum=MAX_SUMMARY_CHARS
        ),
        "limitations": limitations,
        "source_coverage": coverage,
        "reported_source_coverage": reported_coverage,
        "candidates": normalized_candidates,
    }
    if input_manifest is not None:
        normalized_output["input_manifest"] = input_manifest.to_dict()
    serialized_bytes = len(
        json.dumps(
            normalized_output,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    if serialized_bytes > MAX_NORMALIZED_OUTPUT_BYTES:
        raise TruthAnalysisError(
            "invalid_output",
            "The normalized Truth analysis output exceeds its safe delivery budget.",
            status=413,
            details={
                "normalized_output_bytes": serialized_bytes,
                "maximum_normalized_output_bytes": MAX_NORMALIZED_OUTPUT_BYTES,
            },
        )
    return normalized_output


def submit_worker_output(
    *,
    run_id: str,
    payload: Mapping[str, Any],
    agent_session_id: str | None,
    disclosure_boundary: (
        truth_analysis_disclosure.TruthAnalysisDisclosureBoundary | None
    ) = None,
) -> dict[str, Any]:
    """Validate and stage one immutable typed worker result, without Truth writes."""

    run = _bound_run(run_id, agent_session_id)
    if run.status in {"failed", "unavailable"}:
        raise TruthAnalysisError(
            "analysis_run_terminal",
            "This Truth analysis run cannot accept output.",
            status=409,
        )
    store = TruthStoreRegistry().open_store(run.store_id)
    boundary = (
        disclosure_boundary
        or truth_analysis_disclosure.configured_truth_analysis_disclosure()
    )
    try:
        input_manifest = (
            boundary.manifest_digest(run) if boundary is not None else None
        )
        normalized = _normalize_worker_output(
            store,
            run,
            _mapping(payload, "payload"),
            input_manifest=input_manifest,
        )
        binding = None
        if boundary is not None:
            normalized_sha256 = sha256_text(canonical_json(normalized))
            binding = boundary.bind_output(
                run,
                output_ref=f"truth-analysis-output:{normalized_sha256}",
                idempotency_key=f"truth-analysis-output-bind:{normalized_sha256}",
            )
            if (
                input_manifest is None
                or binding.manifest_sha256 != input_manifest.manifest_sha256
                or binding.entry_count != input_manifest.entry_count
                or binding.through_sequence != input_manifest.through_sequence
            ):
                raise TruthAnalysisError(
                    "analysis_disclosure_changed",
                    "Truth analysis inputs changed while binding the output.",
                    status=409,
                )
    except TruthAnalysisError:
        raise
    except Exception as exc:
        raise _disclosure_failure(exc) from exc
    try:
        completed = truth_analysis_runtime.update_run(
            run.run_id,
            status="completed",
            output=normalized,
        )
    except ValueError as exc:
        raise TruthAnalysisError(
            "analysis_output_conflict", str(exc), status=409
        ) from exc
    receipt = {
        "ok": True,
        "schema": "wb.cowork.truth-analysis-submit-receipt/v1",
        "analysis_run_id": completed.run_id,
        "status": _public_status(completed.status),
        "output_sha256": completed.output_sha256,
    }
    if binding is not None:
        receipt["input_manifest_sha256"] = binding.manifest_sha256
    return receipt


def _candidate_for_commit(
    run: TruthAnalysisRuntimeRun,
    candidate_id: str,
    expected_canonical_sha256: str,
) -> dict[str, Any]:
    if run.status != "completed" or run.output is None:
        raise TruthAnalysisError(
            "analysis_not_complete",
            "Truth analysis candidates are not ready.",
            status=409,
        )
    candidates = run.output.get("candidates")
    matches = (
        [
            dict(item)
            for item in candidates
            if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id
        ]
        if isinstance(candidates, list)
        else []
    )
    if len(matches) != 1:
        raise TruthAnalysisError(
            "candidate_not_found", "Truth analysis candidate does not exist.", status=404
        )
    candidate = matches[0]
    if candidate.get("canonical_sha256") != str(
        expected_canonical_sha256 or ""
    ).strip().lower():
        raise TruthAnalysisError(
            "stale_candidate",
            "The Truth candidate changed after it was shown.",
            status=409,
            retryable=True,
        )
    return candidate


def _effective_candidate(
    store: TruthStore,
    candidate: Mapping[str, Any],
    edits: Mapping[str, Any] | None,
) -> dict[str, Any]:
    value = dict(candidate)
    edits_value = {} if edits is None else dict(edits)
    unsupported = set(edits_value) - {
        "proposition",
        "claim_kind",
        "structured",
        "expression_role",
        "evidence_candidate_ids",
    }
    if unsupported:
        raise TruthAnalysisError(
            "invalid_candidate_edits",
            f"Unsupported candidate edits: {sorted(unsupported)}",
        )
    if "proposition" in edits_value:
        value["proposition"] = _required_text(
            edits_value["proposition"], "edits.proposition", maximum=4_000
        ).strip()
    if "claim_kind" in edits_value:
        value["claim_kind"] = _required_text(
            edits_value["claim_kind"], "edits.claim_kind", maximum=120
        )
    if "structured" in edits_value:
        structured = edits_value["structured"]
        if structured is not None and not isinstance(structured, Mapping):
            raise TruthAnalysisError(
                "invalid_candidate_edits", "edits.structured must be an object"
            )
        value["structured"] = None if structured is None else dict(structured)
    if "expression_role" in edits_value:
        role = edits_value["expression_role"]
        if role not in EXPRESSION_ROLES:
            raise TruthAnalysisError(
                "invalid_candidate_edits", "edits.expression_role is not supported"
            )
        value["expression"] = {**dict(value["expression"]), "role": str(role)}
    # Evidence attachment is an affirmative human choice. Missing selection is
    # deliberately equivalent to an empty list, never "all staged evidence".
    selected = edits_value.get("evidence_candidate_ids", [])
    if not isinstance(selected, list) or not all(
        isinstance(item, str) for item in selected
    ):
        raise TruthAnalysisError(
            "invalid_candidate_edits",
            "edits.evidence_candidate_ids must be an array of strings",
        )
    if len(selected) != len(set(selected)):
        raise TruthAnalysisError(
            "invalid_candidate_edits",
            "edits.evidence_candidate_ids must not contain duplicates",
        )
    evidence = [
        dict(item)
        for item in value.get("evidence", [])
        if isinstance(item, Mapping)
    ]
    admitted = {str(item.get("evidence_candidate_id")) for item in evidence}
    if not set(selected).issubset(admitted):
        raise TruthAnalysisError(
            "invalid_candidate_edits",
            "edits.evidence_candidate_ids names unstaged evidence",
        )
    attachable = {
        str(item.get("evidence_candidate_id"))
        for item in evidence
        if item.get("attachable") is True
    }
    if not set(selected).issubset(attachable):
        raise TruthAnalysisError(
            "invalid_candidate_edits",
            "edits.evidence_candidate_ids names evidence that cannot be attached",
        )
    selected_set = set(selected)
    value["evidence"] = [
        item
        for item in evidence
        if str(item.get("evidence_candidate_id")) in selected_set
    ]
    try:
        validate_new_claim(
            store.profile,
            claim_kind=str(value["claim_kind"]),
            structured=value.get("structured"),
        )
    except InvariantViolation as exc:
        raise TruthAnalysisError("invalid_candidate_edits", str(exc)) from exc
    return value


def _existing_decision(
    run_id: str,
    candidate_id: str,
):
    return next(
        (
            item
            for item in truth_analysis_runtime.candidate_decisions_for_run(run_id)
            if item.candidate_id == candidate_id
        ),
        None,
    )


def _live_exact_claim(
    store: TruthStore,
    candidate: Mapping[str, Any],
) -> Any | None:
    digest = claim_sha256(
        proposition=str(candidate.get("proposition") or ""),
        claim_kind=str(candidate.get("claim_kind") or ""),
        structured=candidate.get("structured"),
        scope="store",
        valid_from=None,
        valid_to=None,
    )
    with store._read_connection() as conn:
        return store._find_live_claim_locked(conn, digest)


def _claim_prepared_for_candidate(
    claim: Any,
    *,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
) -> bool:
    try:
        meta = json.loads(claim.meta_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(meta, Mapping) and all(
        meta.get(key) == value
        for key, value in {
            "source": "cowork_truth_analysis",
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_canonical_sha256": candidate_canonical_sha256,
        }.items()
    )


def _materialize_web_fetch_span(
    store: TruthStore,
    *,
    run_id: str,
    candidate: Mapping[str, Any],
    item: Mapping[str, Any],
    conn: sqlite3.Connection,
) -> str:
    """Capture one selected runtime fetch as quarantined canonical evidence."""

    fetch_id = str(item.get("fetch_id") or "")
    fetch = truth_analysis_runtime.get_fetch_receipt(run_id, fetch_id)
    run = truth_analysis_runtime.get_run(run_id)
    if (
        run is None
        or fetch is None
        or fetch.status != "completed"
        or not fetch.text
        or sha256_text(fetch.text) != fetch.content_sha256
    ):
        raise TruthAnalysisError(
            "staged_evidence_unavailable",
            "The selected web evidence is no longer available with its exact bytes.",
            status=409,
            retryable=True,
        )
    research_fetch = truth_analysis_research.get_receipt(
        run_id=run_id,
        fetch_id=fetch_id,
        agent_session_id=run.session_id,
    )
    acquisition_metadata = (
        {}
        if research_fetch is None
        else dict(research_fetch.acquisition_metadata)
    )
    acquisition_actor = Actor(
        "agent_run",
        run.session_id,
        {
            "model": str(run.selection.get("model_id") or "unknown"),
            "harness": str(run.selection.get("provider_id") or "account_backed"),
            "surface": "cowork_truth_analysis",
            "session_id": run.session_id,
            "call_id": run.run_id,
        },
    )
    evidence_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-web-evidence/v1",
                "run_id": run_id,
                "fetch_id": fetch.fetch_id,
                "content_sha256": fetch.content_sha256,
            }
        )
    )[:32]
    evidence = store.get_evidence(evidence_id, conn=conn)
    if evidence is None:
        raw = fetch.text.encode("utf-8")
        if len(raw) > store._inline_content_bytes:
            store._store_blob_bytes(fetch.content_sha256, raw)
        evidence = store.capture_evidence(
            kind="web",
            source_locator=fetch.canonical_url or fetch.url,
            actor=acquisition_actor,
            acquisition_method="fetch",
            origin=AcquisitionOrigin.EXTERNAL,
            content=fetch.text,
            content_sha256=fetch.content_sha256,
            media_type="text/plain",
            acquired_at=fetch.fetched_at,
            external_reviewed=False,
            meta={
                "surface": "cowork_truth_analysis",
                "analysis_run_id": run_id,
                "candidate_id": candidate["candidate_id"],
                "fetch_id": fetch.fetch_id,
                "search_hit_id": fetch.hit_id,
                "source_title": fetch.title,
                "extractor": fetch.extractor,
                "capture": {
                    "text_truncated": bool(
                        acquisition_metadata.get("text_truncated", False)
                    ),
                    "captured_text_bytes": int(
                        acquisition_metadata.get(
                            "captured_text_bytes", len(fetch.text.encode("utf-8"))
                        )
                    ),
                    "extracted_text_bytes": int(
                        acquisition_metadata.get(
                            "extracted_text_bytes", len(fetch.text.encode("utf-8"))
                        )
                    ),
                    "captured_text_sha256": str(
                        acquisition_metadata.get("captured_text_sha256")
                        or fetch.content_sha256
                    ),
                    "full_extracted_text_sha256": str(
                        acquisition_metadata.get("full_extracted_text_sha256")
                        or fetch.content_sha256
                    ),
                    "maximum_captured_text_bytes": MAX_WEB_CAPTURED_TEXT_BYTES,
                },
            },
            record_id=evidence_id,
            conn=conn,
        )
    elif (
        evidence.content_sha256 != fetch.content_sha256
        or evidence.source_locator != (fetch.canonical_url or fetch.url)
    ):
        raise TruthAnalysisError(
            "staged_evidence_conflict",
            "The canonical web evidence identity has conflicting content.",
            status=409,
        )
    selector_value = _mapping(item.get("selector"), "candidate.evidence.selector")
    selector = CompositeSelector(
        exact=selector_value.get("exact"),
        prefix=selector_value.get("prefix", ""),
        suffix=selector_value.get("suffix", ""),
        start=selector_value.get("start"),
        end=selector_value.get("end"),
    )
    span_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-web-evidence-span/v1",
                "evidence_id": evidence.id,
                "quote": item.get("quote"),
                "selector": selector_value,
            }
        )
    )[:32]
    existing_span = store.get_span(span_id, conn=conn)
    if existing_span is not None:
        if (
            existing_span.evidence_id != evidence.id
            or existing_span.quote_exact != item.get("quote")
        ):
            raise TruthAnalysisError(
                "staged_evidence_conflict",
                "The canonical web evidence span identity has conflicting content.",
                status=409,
            )
        return existing_span.id
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=selector,
        actor=acquisition_actor,
        author_kind="unknown",
        author_ref=None,
        record_id=span_id,
        conn=conn,
    )
    return span.id


def _attach_admitted_support(
    store: TruthStore,
    *,
    claim_id: str,
    run_id: str,
    candidate: Mapping[str, Any],
    actor: Actor,
    conn: sqlite3.Connection,
) -> list[str]:
    """Attach only existing Truth spans assessed as support or partial support."""

    attached: list[str] = []
    for item in candidate.get("evidence", []):
        if (
            not isinstance(item, Mapping)
            or item.get("relationship")
            not in {"supports", "partially_supports"}
            or item.get("attachable") is not True
        ):
            continue
        if item.get("source_kind") == "truth_span":
            span_id = str(item.get("span_id") or "")
        elif item.get("source_kind") == "web_fetch":
            span_id = _materialize_web_fetch_span(
                store,
                run_id=run_id,
                candidate=candidate,
                item=item,
                conn=conn,
            )
        else:
            continue
        relation_role = {
            "schema": "claim-evidence/v1",
            "evidential_effect": str(item.get("relationship")),
            # The staged verifier assessed evidential effect, but did not
            # establish a stronger extraction relationship for this separate
            # support item. Keep that axis explicit and conservative.
            "derivation_relationship": "context",
            "diagnostics": {
                "source": "cowork_truth_analysis",
                "analysis_run_id": run_id,
                "candidate_id": candidate["candidate_id"],
            },
        }
        link_id = sha256_text(
            canonical_json(
                {
                    "domain": "work-buddy.cowork-truth-analysis-evidence-relation/v1",
                    "run_id": run_id,
                    "candidate_id": candidate["candidate_id"],
                    "claim_id": claim_id,
                    "span_id": span_id,
                    "role": relation_role,
                }
            )
        )[:32]
        existing = store.get_link(link_id, conn=conn)
        if existing is not None:
            if (
                existing.from_claim_id != claim_id
                or existing.link_type != "evidence_relation"
                or existing.to_kind != "evidence_span"
                or existing.to_ref != span_id
                or json.loads(existing.role_json or "null") != relation_role
            ):
                raise TruthAnalysisError(
                    "staged_evidence_conflict",
                    "The canonical evidence relationship identity conflicts.",
                    status=409,
                )
            attached.append(existing.id)
            continue
        try:
            link = store.add_link(
                from_claim_id=claim_id,
                link_type="evidence_relation",
                to_kind="evidence_span",
                to_ref=span_id,
                actor=actor,
                role=relation_role,
                record_id=link_id,
                conn=conn,
            )
            attached.append(link.id)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM claim_links WHERE id = ?", (link_id,)
            ).fetchone()
            if row is None:
                raise
            attached.append(str(row["id"]))
    return attached


def _decision_claim_identity(
    *,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
) -> tuple[str, str]:
    binding = {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_canonical_sha256": candidate_canonical_sha256,
    }
    claim_id = sha256_text(
        canonical_json(
            {"domain": "work-buddy.cowork-truth-analysis-claim/v1", **binding}
        )
    )[:32]
    status_event_id = sha256_text(
        canonical_json(
            {
                "domain": "work-buddy.cowork-truth-analysis-claim-status/v1",
                **binding,
            }
        )
    )[:32]
    return claim_id, status_event_id


def _claim_is_exact_decision_recovery(
    claim: Any,
    *,
    expected_claim_id: str,
    expected_claim_sha256: str,
    run_id: str,
    candidate_id: str,
    candidate_canonical_sha256: str,
    decision_payload_sha256: str,
    actor: Actor,
) -> bool:
    if (
        claim.id != expected_claim_id
        or claim.canonical_sha256 != expected_claim_sha256
        or claim.created_by_kind != actor.kind
        or claim.created_by_ref != actor.ref
    ):
        return False
    try:
        meta = json.loads(claim.meta_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(meta, Mapping) and all(
        meta.get(key) == value
        for key, value in {
            "source": "cowork_truth_analysis",
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_canonical_sha256": candidate_canonical_sha256,
            "decision_payload_sha256": decision_payload_sha256,
        }.items()
    )


def _recover_decision_connection(
    *,
    conn: sqlite3.Connection,
    document: DocumentRecord,
    action: ActionSnapshot,
    claim: Any,
    role: str,
    selector: Mapping[str, Any],
    decision_meta: Mapping[str, Any],
    actor: Actor,
    claim_created: bool,
) -> truth_surface.ConnectionWrite | None:
    """Locate the exact canonical connection from a prior decision attempt."""

    expected_selector_json = serialize_selector(
        CompositeSelector(
            exact=selector.get("exact"),
            prefix=selector.get("prefix", ""),
            suffix=selector.get("suffix", ""),
            start=selector.get("start"),
            end=selector.get("end"),
        )
    )
    rows = conn.execute(
        "SELECT e.id AS expression_id, e.document_span_id, e.meta_json, "
        "e.created_by_kind, e.created_by_ref, e.claim_canonical_sha256, "
        "s.selector_json, s.quote_exact "
        "FROM expressions e JOIN document_spans s ON s.id = e.document_span_id "
        "WHERE s.document_id = ? AND e.claim_ref_kind = 'local' "
        "AND e.claim_ref = ? AND e.role = ? ORDER BY e.created_at, e.id",
        (document.id, claim.id, role),
    ).fetchall()
    marked: list[tuple[sqlite3.Row, Mapping[str, Any]]] = []
    exact: list[tuple[sqlite3.Row, Mapping[str, Any]]] = []
    for row in rows:
        if (
            str(row["claim_canonical_sha256"]) != claim.canonical_sha256
            or str(row["quote_exact"]) != str(selector.get("exact") or "")
        ):
            continue
        try:
            meta = json.loads(str(row["meta_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if not isinstance(meta, Mapping):
            meta = {}
        marker_matches = (
            str(row["created_by_kind"]) == actor.kind
            and str(row["created_by_ref"] or "") == actor.ref
            and all(meta.get(key) == value for key, value in decision_meta.items())
        )
        value = (row, meta)
        if marker_matches:
            marked.append(value)
        elif str(row["selector_json"]) == expected_selector_json:
            exact.append(value)
    matches = marked if marked else exact
    if not matches:
        return None
    if len(matches) != 1:
        raise TruthAnalysisError(
            "candidate_decision_recovery_conflict",
            "The prior canonical connection for this candidate is ambiguous.",
            status=409,
        )
    row, meta = matches[0]
    return truth_surface.ConnectionWrite(
        claim=claim,
        claim_created=claim_created,
        span_id=str(row["document_span_id"]),
        expression_id=str(row["expression_id"]),
        expression_created=bool(marked),
        projection_sha256=str(
            meta.get("base_content_sha256") or action.projection_sha256
        ),
        structured_head_sha256=str(
            meta.get("base_structured_head_sha256")
            or action.structured_head_sha256
        ),
        ydoc_generation_sha256=str(
            meta.get("base_ydoc_generation_sha256")
            or action.ydoc_generation_sha256
            or ""
        ),
    )


def candidate_decision_subject(run_id: str, candidate_id: str) -> str:
    """Return the exact local-gesture subject for one staged candidate."""

    return f"cowork-truth-candidate-decision:{run_id}:{candidate_id}"


def analysis_start_subject(document_id: str) -> str:
    """Return the exact local-gesture subject for starting document analysis."""

    return f"cowork-truth-analysis-start:{document_id}"


def analysis_start_context_sha256(
    *,
    store_id: str,
    document_id: str,
    capture: Mapping[str, Any],
    selection: AgentExecutionSelection,
) -> str:
    """Bind a one-use browser gesture to the frozen capture and model IDs."""

    return sha256_text(
        canonical_json(
            {
                "schema": ANALYSIS_START_GESTURE_SCHEMA,
                "store_id": store_id,
                "document_id": document_id,
                "capture": dict(capture),
                "execution": {
                    "provider_id": selection.provider_id,
                    "model_id": selection.model_id,
                },
            }
        )
    )


def human_analysis_start_actor(
    authority: HumanAuthorityContext,
    *,
    store_id: str,
    document_id: str,
    capture: Mapping[str, Any],
    selection: AgentExecutionSelection,
) -> Actor:
    """Validate analysis-start authority and derive its canonical Truth actor."""

    actor = authority.principal.actor
    expected_subject = analysis_start_subject(document_id)
    expected_context = analysis_start_context_sha256(
        store_id=store_id,
        document_id=document_id,
        capture=capture,
        selection=selection,
    )
    if (
        actor.kind != "human"
        or authority.action != ANALYSIS_START_ACTION
        or authority.subject_sha256 != sha256_text(expected_subject)
        or authority.context_sha256 != expected_context
        or authority.assurance != HUMAN_AUTHORITY_ASSURANCE
        or authority.basis != HUMAN_AUTHORITY_BASIS
    ):
        raise TruthAnalysisError(
            "human_authority_required",
            "An authenticated gesture for this exact Truth analysis is required.",
            status=403,
        )
    return Actor("human", actor.canonical_id)


def candidate_decision_context_sha256(
    *,
    store_id: str,
    document_id: str,
    run_id: str,
    candidate_id: str,
    expected_canonical_sha256: str,
    decision: str,
    existing_claim_id: str | None,
    edits: Mapping[str, Any] | None,
) -> str:
    """Bind a browser gesture to the complete candidate-decision request."""

    return sha256_text(
        canonical_json(
            {
                "schema": CANDIDATE_DECISION_GESTURE_SCHEMA,
                "store_id": store_id,
                "document_id": document_id,
                "analysis_run_id": run_id,
                "candidate_id": candidate_id,
                "expected_canonical_sha256": expected_canonical_sha256,
                "decision": decision,
                "existing_claim_id": existing_claim_id,
                "edits": None if edits is None else dict(edits),
            }
        )
    )


def _human_decision_actors(
    authority: HumanAuthorityContext,
    *,
    run: TruthAnalysisRuntimeRun,
    candidate_id: str,
    expected_canonical_sha256: str,
    decision: str,
    existing_claim_id: str | None,
    edits: Mapping[str, Any] | None,
) -> tuple[ActorRef, Actor]:
    """Validate defense-in-depth authority and derive the compatibility actor."""

    actor = authority.principal.actor
    expected_subject = candidate_decision_subject(run.run_id, candidate_id)
    expected_context = candidate_decision_context_sha256(
        store_id=run.store_id,
        document_id=run.document_id,
        run_id=run.run_id,
        candidate_id=candidate_id,
        expected_canonical_sha256=expected_canonical_sha256,
        decision=decision,
        existing_claim_id=existing_claim_id,
        edits=edits,
    )
    if (
        actor.kind != "human"
        or authority.action != CANDIDATE_DECISION_ACTION
        or authority.subject_sha256 != sha256_text(expected_subject)
        or authority.context_sha256 != expected_context
        or authority.assurance != HUMAN_AUTHORITY_ASSURANCE
        or authority.basis != HUMAN_AUTHORITY_BASIS
    ):
        raise TruthAnalysisError(
            "human_authority_required",
            "An authenticated gesture for this exact Truth decision is required.",
            status=403,
        )
    return actor, Actor("human", actor.canonical_id)


def _analysis_actor_ref(run: TruthAnalysisRuntimeRun, human: ActorRef) -> ActorRef:
    return ActorRef(
        issuer_authority_id=human.issuer_authority_id,
        subject=run.session_id,
        kind="agent_run",
        tenant_scope_id=human.tenant_scope_id,
    )


def _analysis_actor(run: TruthAnalysisRuntimeRun) -> Actor:
    return Actor(
        "agent_run",
        run.session_id,
        {
            "model": str(run.selection.get("model_id") or "unknown"),
            "harness": "work-buddy-agent-execution",
            "surface": "cowork_truth_analysis",
            "session_id": run.session_id,
            "call_id": run.run_id,
        },
    )


def _truth_service_actor(human: ActorRef, subject: str) -> ActorRef:
    return ActorRef(
        issuer_authority_id=human.issuer_authority_id,
        subject=subject,
        kind="service",
        tenant_scope_id=human.tenant_scope_id,
    )


def _open_truth_source_store() -> SourceStore:
    """Open the one local Sources authority through its public store seam."""

    return SourceStore.create(resolve("stores/sources"))


def _capture_analysis_source(
    run: TruthAnalysisRuntimeRun,
    action: ActionSnapshot,
    *,
    human: ActorRef,
) -> tuple[SourceStore, Any, str]:
    """Capture the exact frozen target as a stable Sources occurrence."""

    source_store = _open_truth_source_store()
    source_principal = _truth_service_actor(human, "work-buddy-truth-service")
    provider_registry = ProviderRegistry()
    provider_registry.register(
        CoworkActionSnapshotProvider(
            tenant_scope_id=human.tenant_scope_id,
            issuer=_truth_service_actor(human, "work-buddy-cowork-source"),
            registry=TruthStoreRegistry(),
        )
    )
    origin = cowork_action_snapshot_origin(
        store_id=run.store_id,
        action_snapshot_id=action.id,
        revision=action.canonical_sha256,
        part="target",
    )
    source_ref = source_capture_from_origin(
        source_store,
        provider_registry,
        provider_id="cowork-document",
        origin_ref=origin,
        principal=source_principal,
        purpose=SOURCE_CLAIM_PURPOSE,
        tenant_scope_id=human.tenant_scope_id,
        originating_surface="cowork_truth_analysis",
        expected_revision=action.canonical_sha256,
        expected_digest=action.target_text_sha256,
        namespace=run.run_id,
    )
    item = source_store.get_item(source_ref)
    if item is None:
        raise TruthAnalysisError(
            "analysis_source_unavailable",
            "The frozen passage source could not be resolved.",
            status=409,
            retryable=True,
        )
    return source_store, source_ref, item.primary_representation_id


def _source_claim_candidate(
    effective: Mapping[str, Any],
    *,
    candidate_id: str,
    candidate_sha256: str,
) -> SourceClaimCandidate:
    expression = _mapping(effective.get("expression"), "candidate.expression")
    role = str(expression.get("role") or "")
    target_selector = _mapping(
        expression.get("target_selector"), "candidate.expression.target_selector"
    )
    derivation = {
        "quote": "direct_statement",
        "paraphrase": "paraphrase",
        "summary": "paraphrase",
        "instantiation": "inference",
    }[role]
    return SourceClaimCandidate(
        proposition=str(effective["proposition"]),
        claim_kind=str(effective["claim_kind"]),
        structured=effective.get("structured"),
        scope="store",
        confidence_extraction=effective.get("confidence_extraction"),
        selector=dict(target_selector),
        # The selected document passage is the extraction premise. It does not
        # by itself establish independent support for the proposition.
        evidential_effect="mentions",
        derivation_relationship=derivation,
        relation_diagnostics={
            "source": "cowork_truth_analysis",
            "expression_role": role,
        },
        candidate_id=candidate_id,
        candidate_sha256=candidate_sha256,
    )


def _source_claim_actors(
    run: TruthAnalysisRuntimeRun,
    *,
    human: ActorRef,
    original: Mapping[str, Any],
    effective: Mapping[str, Any],
    edits: Mapping[str, Any] | None,
    connect_existing: bool,
) -> SourceClaimActors:
    ai = _analysis_actor_ref(run, human)
    edit_keys = frozenset() if edits is None else frozenset(edits)
    semantic_changed = any(
        original.get(key) != effective.get(key)
        for key in ("proposition", "claim_kind", "structured")
    )
    return SourceClaimActors(
        semantic_producer=ai,
        selector=ai,
        candidate_preparer=ai,
        matcher=ai if connect_existing else None,
        semantic_reviser=human if semantic_changed else None,
        evidence_selector=(human if "evidence_candidate_ids" in edit_keys else None),
        expression_relation_assessor=(
            human if "expression_role" in edit_keys else None
        ),
        applier=_truth_service_actor(human, "work-buddy-truth-kernel"),
        producer_meta=dict(_analysis_actor(run).meta or {}),
        run_ref=run.run_id,
    )


def _candidate_operation_key(run_id: str, candidate_id: str) -> str:
    return f"cowork-truth-analysis:{run_id}:{candidate_id}"


def _stable_analysis_id(domain: str, value: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json({"domain": domain, **dict(value)}))[:32]


def _replay_canonical_candidate_result(
    store: TruthStore,
    *,
    operation_name: str,
    idempotency_key: str,
    run_id: str,
    candidate_id: str,
    candidate_sha256: str,
    decision: str,
    decision_payload_sha256: str,
) -> dict[str, Any] | None:
    prior = truth_queries.truth_operation_result(
        store,
        operation_name=operation_name,
        idempotency_key=idempotency_key,
    )
    if prior is None:
        return None
    result = json.loads(prior.result_json)
    expected = {
        "analysis_run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_canonical_sha256": candidate_sha256,
        "analysis_decision": decision,
        "decision_payload_sha256": decision_payload_sha256,
    }
    if not isinstance(result, Mapping) or any(
        result.get(key) != value for key, value in expected.items()
    ):
        raise TruthAnalysisError(
            "candidate_already_decided",
            "This Truth candidate already has another decision.",
            status=409,
        )
    return dict(result)


def commit_candidate_decision(
    *,
    run_id: str,
    candidate_id: str,
    expected_canonical_sha256: str,
    decision: str,
    authority_context: HumanAuthorityContext,
    edits: Mapping[str, Any] | None = None,
    existing_claim_id: str | None = None,
) -> dict[str, Any]:
    """Apply one explicit human staging decision through canonical Truth APIs."""

    if decision not in {"save_as_proposed", "connect_existing", "dismiss"}:
        raise TruthAnalysisError(
            "invalid_decision",
            "decision must be save_as_proposed, connect_existing, or dismiss",
        )
    run = truth_analysis_runtime.get_run(run_id)
    if run is None:
        raise TruthAnalysisError(
            "analysis_run_not_found", "Truth analysis run does not exist.", status=404
        )
    submitted_edits = None if edits is None else dict(edits)
    human_ref, actor = _human_decision_actors(
        authority_context,
        run=run,
        candidate_id=candidate_id,
        expected_canonical_sha256=expected_canonical_sha256,
        decision=decision,
        existing_claim_id=existing_claim_id,
        edits=submitted_edits,
    )
    candidate = _candidate_for_commit(
        run, candidate_id, expected_canonical_sha256
    )
    decision_edits = {} if submitted_edits is None else dict(submitted_edits)
    if decision == "connect_existing":
        decision_edits["existing_claim_id"] = str(existing_claim_id or "")
    prior = _existing_decision(run_id, candidate_id)
    if prior is not None:
        if (
            prior.candidate_canonical_sha256 != expected_canonical_sha256
            or prior.decision != decision
            or dict(prior.edits) != decision_edits
        ):
            raise TruthAnalysisError(
                "candidate_already_decided",
                "This Truth candidate already has another decision.",
                status=409,
            )
        return {
            "ok": True,
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "decision": decision,
            "candidate_status": str(prior.result.get("status") or ""),
            "claim_id": prior.result.get("claim_id"),
            "expression_id": prior.result.get("expression_id"),
            "result": dict(prior.result),
            "replayed": True,
        }
    store = TruthStoreRegistry().open_store(run.store_id)
    decision_payload_sha256 = sha256_text(
        canonical_json({"decision": decision, "edits": decision_edits})
    )
    operation_key = _candidate_operation_key(run_id, candidate_id)
    if decision == "dismiss":
        canonical = _replay_canonical_candidate_result(
            store,
            operation_name="cowork_truth_candidate_dismiss",
            idempotency_key=operation_key,
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_sha256=expected_canonical_sha256,
            decision=decision,
            decision_payload_sha256=decision_payload_sha256,
        )
        try:
            truth_analysis_runtime.prepare_candidate_decision(
                run_id=run_id,
                candidate_id=candidate_id,
                candidate_canonical_sha256=expected_canonical_sha256,
                decision=decision,
                edits=decision_edits,
                decided_by_ref=human_ref.canonical_id,
            )
        except ValueError as exc:
            raise TruthAnalysisError(
                "candidate_already_decided",
                str(exc),
                status=409,
            ) from exc
        if canonical is None:
            request_sha256 = sha256_text(
                canonical_json(
                    {
                        "schema": "wb.cowork.truth-candidate-dismiss/v1",
                        "analysis_run_id": run_id,
                        "candidate_id": candidate_id,
                        "candidate_canonical_sha256": expected_canonical_sha256,
                        "analysis_decision": decision,
                        "decision_payload_sha256": decision_payload_sha256,
                        "actor": human_ref.to_dict(),
                        "authorization_context_sha256": authority_context.context_sha256,
                    }
                )
            )
            canonical = {
                "status": "dismissed",
                "claim_id": None,
                "expression_id": None,
                "analysis_run_id": run_id,
                "candidate_id": candidate_id,
                "candidate_canonical_sha256": expected_canonical_sha256,
                "analysis_decision": decision,
                "decision_payload_sha256": decision_payload_sha256,
            }
            try:
                with store.write_transaction() as conn:
                    candidate_decision = record_truth_candidate_decision(
                        store,
                        candidate_id=candidate_id,
                        candidate_sha256=expected_canonical_sha256,
                        decision="dismiss",
                        claim_id=None,
                        actor=human_ref,
                        basis=authority_context.basis,
                        assurance=authority_context.assurance,
                        authorization_ref=authority_context.gesture_id,
                        authorization_context_sha256=authority_context.context_sha256,
                        run_ref=run_id,
                        record_id=_stable_analysis_id(
                            "work-buddy.cowork-truth-candidate-dismiss/v1",
                            {
                                "run_id": run_id,
                                "candidate_id": candidate_id,
                                "candidate_sha256": expected_canonical_sha256,
                            },
                        ),
                        conn=conn,
                    )
                    canonical["candidate_decision_id"] = candidate_decision.id
                    record_truth_operation_result(
                        store,
                        operation_name="cowork_truth_candidate_dismiss",
                        idempotency_key=operation_key,
                        request_sha256=request_sha256,
                        result=canonical,
                        actor=_truth_service_actor(
                            human_ref, "work-buddy-truth-kernel"
                        ),
                        record_id=_stable_analysis_id(
                            "work-buddy.cowork-truth-candidate-dismiss-result/v1",
                            {"idempotency_key": operation_key},
                        ),
                        conn=conn,
                    )
            except Exception:
                truth_analysis_runtime.clear_candidate_decision_intent(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    candidate_canonical_sha256=expected_canonical_sha256,
                    decision=decision,
                    edits=decision_edits,
                    decided_by_ref=human_ref.canonical_id,
                )
                raise
        result = {"status": "dismissed"}
        receipt, replayed = truth_analysis_runtime.record_candidate_decision(
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_canonical_sha256=expected_canonical_sha256,
            decision=decision,
            edits=decision_edits,
            result=result,
            decided_by_ref=human_ref.canonical_id,
        )
        return {
            "ok": True,
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "decision": receipt.decision,
            "candidate_status": "dismissed",
            "claim_id": None,
            "expression_id": None,
            "result": result,
            "replayed": replayed,
        }

    document = documents.get_document(store, run.document_id)
    action = _action(store, run)
    effective = _effective_candidate(store, candidate, edits)
    match = candidate.get("existing_claim_match")
    claim_record_id, claim_status_event_id = _decision_claim_identity(
        run_id=run_id,
        candidate_id=candidate_id,
        candidate_canonical_sha256=expected_canonical_sha256,
    )
    live_exact = _live_exact_claim(store, effective)
    live_is_recovery = bool(
        live_exact is not None
        and live_exact.id == claim_record_id
        and _claim_prepared_for_candidate(
            live_exact,
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_canonical_sha256=expected_canonical_sha256,
        )
    )
    if live_exact is not None and not live_is_recovery:
        match = {
            "claim_id": live_exact.id,
            "proposition": live_exact.proposition,
            "claim_kind": live_exact.claim_kind,
            "relationship": "exact",
            "confidence": 1.0,
        }
        if decision == "save_as_proposed":
            raise TruthAnalysisError(
                "existing_claim_requires_connection",
                "Connect this passage to the existing claim instead of creating a duplicate.",
                status=409,
                details={"claim_id": live_exact.id},
            )
    if isinstance(match, Mapping) and match.get("relationship") in {
        "exact",
        "equivalent",
    }:
        claim_text_changed = (
            effective.get("proposition") != candidate.get("proposition")
            or effective.get("claim_kind") != candidate.get("claim_kind")
        )
        effective_equals_match = (
            effective.get("proposition") == match.get("proposition")
            and effective.get("claim_kind") == match.get("claim_kind")
        )
        if decision == "save_as_proposed" and (
            not claim_text_changed or effective_equals_match
        ):
            raise TruthAnalysisError(
                "existing_claim_requires_connection",
                "Connect this passage to the existing claim instead of creating a duplicate.",
                status=409,
                details={"claim_id": match.get("claim_id")},
            )
        if decision == "connect_existing" and claim_text_changed:
            raise TruthAnalysisError(
                "existing_match_changed",
                "A candidate with edited claim text must be saved as a new proposal.",
                status=409,
                details={"claim_id": match.get("claim_id")},
            )
    matched_claim_id = ""
    if decision == "connect_existing":
        if not isinstance(match, Mapping) or match.get("relationship") not in {
            "exact",
            "equivalent",
        }:
            raise TruthAnalysisError(
                "existing_match_required",
                "Only an exact or equivalent staged match can connect an existing claim.",
                status=409,
            )
        matched_claim_id = str(match.get("claim_id") or "")
        if existing_claim_id != matched_claim_id:
            raise TruthAnalysisError(
                "existing_match_changed",
                "The selected existing claim does not match the staged analysis.",
                status=409,
            )
    selector = _mapping(
        _mapping(effective.get("expression"), "candidate.expression").get("selector"),
        "candidate.expression.selector",
    )
    decision_meta = {
        "source": "cowork_truth_analysis",
        "analysis_run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_canonical_sha256": expected_canonical_sha256,
        "decision_payload_sha256": decision_payload_sha256,
        "provider_id": run.selection.get("provider_id"),
        "model_id": run.selection.get("model_id"),
    }
    source_candidate = _source_claim_candidate(
        effective,
        candidate_id=candidate_id,
        candidate_sha256=expected_canonical_sha256,
    )
    source_actors = _source_claim_actors(
        run,
        human=human_ref,
        original=candidate,
        effective=effective,
        edits=submitted_edits,
        connect_existing=decision == "connect_existing",
    )
    source_decision = CandidateDecisionAuthorization(
        decision="connect" if decision == "connect_existing" else "add",
        actor=human_ref,
        basis=authority_context.basis,
        assurance=authority_context.assurance,
        authorization_ref=authority_context.gesture_id,
        authorization_context_sha256=authority_context.context_sha256,
    )
    canonical = _replay_canonical_candidate_result(
        store,
        operation_name=SOURCE_CLAIM_OPERATION,
        idempotency_key=operation_key,
        run_id=run_id,
        candidate_id=candidate_id,
        candidate_sha256=expected_canonical_sha256,
        decision=decision,
        decision_payload_sha256=decision_payload_sha256,
    )
    if canonical is not None:
        try:
            truth_analysis_runtime.prepare_candidate_decision(
                run_id=run_id,
                candidate_id=candidate_id,
                candidate_canonical_sha256=expected_canonical_sha256,
                decision=decision,
                edits=decision_edits,
                decided_by_ref=human_ref.canonical_id,
            )
        except ValueError as exc:
            raise TruthAnalysisError(
                "candidate_already_decided", str(exc), status=409
            ) from exc
        required_reconcile = {
            "resolution_record_id",
            "usage_id",
            "redaction_epoch",
            "consumer_ref",
        }
        if required_reconcile.issubset(canonical):
            reconcile_source_usage(
                store,
                _open_truth_source_store(),
                resolution_record_id=str(canonical["resolution_record_id"]),
                usage_id=str(canonical["usage_id"]),
                redaction_epoch=int(canonical["redaction_epoch"]),
                consumer_ref=str(canonical["consumer_ref"]),
                actor=source_actors.applier,
            )
        result = {
            "status": "saved",
            "claim_id": str(canonical["claim_id"]),
            "claim_created": bool(canonical["claim_created"]),
            "expression_id": str(canonical["expression_id"]),
            "expression_created": bool(canonical["expression_created"]),
            "support_link_ids": list(canonical.get("support_link_ids") or []),
        }
        receipt, replayed = truth_analysis_runtime.record_candidate_decision(
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_canonical_sha256=expected_canonical_sha256,
            decision=decision,
            edits=decision_edits,
            result=result,
            decided_by_ref=human_ref.canonical_id,
        )
        return {
            "ok": True,
            "analysis_run_id": run_id,
            "candidate_id": candidate_id,
            "decision": receipt.decision,
            "candidate_status": "saved",
            "claim_id": result["claim_id"],
            "expression_id": result["expression_id"],
            "result": result,
            "replayed": replayed,
        }

    source_store, source_ref, representation_id = _capture_analysis_source(
        run, action, human=human_ref
    )
    prepared = prepare_source_claim(
        store,
        source_store,
        source_ref=source_ref,
        representation_id=representation_id,
        expected_content_sha256=action.target_text_sha256,
        expected_native_revision=action.canonical_sha256,
        source_principal=_truth_service_actor(
            human_ref, "work-buddy-truth-service"
        ),
        candidate=source_candidate,
        actors=source_actors,
        idempotency_key=operation_key,
        existing_claim_id=(
            matched_claim_id if decision == "connect_existing" else None
        ),
        decision=source_decision,
    )
    source_store.precommit_recheck_usage(prepared.reservation.usage_id)
    try:
        truth_analysis_runtime.prepare_candidate_decision(
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_canonical_sha256=expected_canonical_sha256,
            decision=decision,
            edits=decision_edits,
            decided_by_ref=human_ref.canonical_id,
        )
    except ValueError as exc:
        source_store.release_usage(prepared.reservation.usage_id)
        if prepared.blob_created:
            store._remove_unreferenced_blob(
                prepared.resolved.representation.content_sha256
            )
        raise TruthAnalysisError(
            "candidate_already_decided",
            str(exc),
            status=409,
        ) from exc
    recovered_canonical_decision = False
    canonical_boundary_may_have_crossed = False
    ai_actor = _analysis_actor(run)
    try:
        with store.write_transaction() as conn:
            if decision == "connect_existing":
                matched_claim = store.get_claim(matched_claim_id, conn=conn)
                if matched_claim is None:
                    raise TruthAnalysisError(
                        "existing_match_changed",
                        "The selected existing claim is no longer available.",
                        status=409,
                    )
                connection = _recover_decision_connection(
                    conn=conn,
                    document=document,
                    action=action,
                    claim=matched_claim,
                    role=str(effective["expression"]["role"]),
                    selector=selector,
                    decision_meta=decision_meta,
                    actor=actor,
                    claim_created=False,
                )
                if connection is None:
                    connection = truth_surface.connect_claim(
                        store,
                        document,
                        actor=actor,
                        selector_input=selector,
                        role=str(effective["expression"]["role"]),
                        expected_structured_head_sha256=(
                            action.structured_head_sha256
                        ),
                        expected_projection_sha256=action.projection_sha256,
                        expected_ydoc_generation_sha256=(
                            action.ydoc_generation_sha256
                        ),
                        claim_id=matched_claim_id,
                        expression_meta=decision_meta,
                        conn=conn,
                        allow_safe_reanchor=True,
                    )
            else:
                expected_claim_sha256 = claim_sha256(
                    proposition=str(effective["proposition"]),
                    claim_kind=str(effective["claim_kind"]),
                    structured=effective.get("structured"),
                    scope="store",
                    valid_from=None,
                    valid_to=None,
                )
                prior_claim = store.get_claim(claim_record_id, conn=conn)
                if prior_claim is not None:
                    if not _claim_is_exact_decision_recovery(
                        prior_claim,
                        expected_claim_id=claim_record_id,
                        expected_claim_sha256=expected_claim_sha256,
                        run_id=run_id,
                        candidate_id=candidate_id,
                        candidate_canonical_sha256=expected_canonical_sha256,
                        decision_payload_sha256=decision_payload_sha256,
                        actor=ai_actor,
                    ):
                        raise TruthAnalysisError(
                            "candidate_decision_recovery_conflict",
                            "A prior canonical write for this candidate does not match "
                            "the submitted decision.",
                            status=409,
                        )
                    recovered_canonical_decision = True
                    connection = _recover_decision_connection(
                        conn=conn,
                        document=document,
                        action=action,
                        claim=prior_claim,
                        role=str(effective["expression"]["role"]),
                        selector=selector,
                        decision_meta=decision_meta,
                        actor=actor,
                        claim_created=True,
                    )
                    if connection is None:
                        raise TruthAnalysisError(
                            "candidate_decision_recovery_incomplete",
                            "The prior claim write exists without its bound passage "
                            "connection.",
                            status=409,
                        )
                else:
                    written = store.propose_claim(
                        proposition=str(effective["proposition"]),
                        claim_kind=str(effective["claim_kind"]),
                        actor=ai_actor,
                        structured=effective.get("structured"),
                        scope="store",
                        confidence_extraction=effective["confidence_extraction"],
                        meta=decision_meta,
                        record_id=claim_record_id,
                        status_event_id=claim_status_event_id,
                        conn=conn,
                    )
                    if not written.created:
                        raise TruthAnalysisError(
                            "candidate_decision_recovery_conflict",
                            "The candidate claim identity was already used.",
                            status=409,
                        )
                    connection = truth_surface.connect_claim(
                        store,
                        document,
                        actor=actor,
                        selector_input=selector,
                        role=str(effective["expression"]["role"]),
                        expected_structured_head_sha256=action.structured_head_sha256,
                        expected_projection_sha256=action.projection_sha256,
                        expected_ydoc_generation_sha256=(
                            action.ydoc_generation_sha256
                        ),
                        claim_id=written.claim.id,
                        expression_meta=decision_meta,
                        conn=conn,
                        allow_safe_reanchor=True,
                    )
                    connection = replace(connection, claim_created=True)
            support_link_ids = _attach_admitted_support(
                store,
                claim_id=connection.claim.id,
                run_id=run_id,
                candidate=effective,
                actor=actor,
                conn=conn,
            )
            source_write = write_prepared_source_claim(
                store,
                prepared,
                candidate=source_candidate,
                actors=source_actors,
                decision=source_decision,
                claim=connection.claim,
                claim_created=(
                    connection.claim_created or recovered_canonical_decision
                ),
                expression_id=connection.expression_id,
                extra_result={
                    "status": "saved",
                    "expression_id": connection.expression_id,
                    "expression_created": (
                        connection.expression_created
                        or recovered_canonical_decision
                    ),
                    "support_link_ids": support_link_ids,
                    "analysis_run_id": run_id,
                    "candidate_id": candidate_id,
                    "candidate_canonical_sha256": expected_canonical_sha256,
                    "analysis_decision": decision,
                    "decision_payload_sha256": decision_payload_sha256,
                    "redaction_epoch": prepared.reservation.redaction_epoch,
                    "consumer_ref": prepared.consumer_ref,
                },
                conn=conn,
            )
            # Set before leaving the transaction.  If COMMIT itself raises, the
            # outcome is ambiguous and the immutable recovery fence must stay.
            canonical_boundary_may_have_crossed = True
    except Exception:
        if not canonical_boundary_may_have_crossed:
            try:
                source_store.release_usage(prepared.reservation.usage_id)
            finally:
                if prepared.blob_created:
                    store._remove_unreferenced_blob(
                        prepared.resolved.representation.content_sha256
                    )
                truth_analysis_runtime.clear_candidate_decision_intent(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    candidate_canonical_sha256=expected_canonical_sha256,
                    decision=decision,
                    edits=decision_edits,
                    decided_by_ref=human_ref.canonical_id,
                )
        raise
    reconcile_source_usage(
        store,
        source_store,
        resolution_record_id=source_write.resolution.id,
        usage_id=prepared.reservation.usage_id,
        redaction_epoch=prepared.reservation.redaction_epoch,
        consumer_ref=prepared.consumer_ref,
        actor=source_actors.applier,
    )
    result = {
        "status": "saved",
        "claim_id": connection.claim.id,
        "claim_created": connection.claim_created or recovered_canonical_decision,
        "expression_id": connection.expression_id,
        "expression_created": (
            connection.expression_created or recovered_canonical_decision
        ),
        "support_link_ids": support_link_ids,
    }
    receipt, replayed = truth_analysis_runtime.record_candidate_decision(
        run_id=run_id,
        candidate_id=candidate_id,
        candidate_canonical_sha256=expected_canonical_sha256,
        decision=decision,
        edits=decision_edits,
        result=result,
        decided_by_ref=human_ref.canonical_id,
    )
    return {
        "ok": True,
        "analysis_run_id": run_id,
        "candidate_id": candidate_id,
        "decision": receipt.decision,
        "candidate_status": "saved",
        "claim_id": connection.claim.id,
        "expression_id": connection.expression_id,
        "result": result,
        "replayed": replayed,
    }


__all__ = [
    "ANALYSIS_CONTEXT_SCHEMA",
    "ANALYSIS_OUTPUT_SCHEMA",
    "ANALYSIS_REQUEST_SCHEMA",
    "MAX_NORMALIZED_OUTPUT_BYTES",
    "MAX_SELECTED_PASSAGE_BYTES",
    "MAX_WORKER_CONTEXT_BYTES",
    "MAX_WEB_FETCHES",
    "MAX_WEB_SEARCHES",
    "TruthAnalysisError",
    "analysis_capabilities_view",
    "analysis_provider_capability",
    "analysis_run_view",
    "commit_candidate_decision",
    "fetch_search_hit",
    "get_worker_context",
    "prepare_analysis_run",
    "search_web",
    "submit_worker_output",
]
