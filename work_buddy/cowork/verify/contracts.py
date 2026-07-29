"""Typed durable contracts for Co-work Verify and Co-think.

The record classes mirror the portable Truth v7 tables exactly. JSON fields
remain canonical JSON strings at the persistence boundary so hashes and
exports are byte-stable; service projections decode them for callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import InvariantViolation


class VerifyInvariantViolation(InvariantViolation):
    """A Verify or Co-think request is not valid for its frozen inputs."""


@dataclass(frozen=True, slots=True)
class ActionTarget:
    """One exact target inside a frozen canonical Markdown projection."""

    kind: str
    exact: str | None = None
    prefix: str = ""
    suffix: str = ""
    start: int | None = None
    end: int | None = None

    @classmethod
    def document(cls) -> "ActionTarget":
        return cls(kind="document")

    @classmethod
    def text_quote(
        cls,
        exact: str,
        *,
        prefix: str = "",
        suffix: str = "",
        start: int | None = None,
        end: int | None = None,
    ) -> "ActionTarget":
        # CompositeSelector owns the established Web Annotation validation.
        selector = CompositeSelector(
            exact=exact,
            prefix=prefix,
            suffix=suffix,
            start=start,
            end=end,
        )
        return cls(
            kind="text_quote",
            exact=selector.exact,
            prefix=selector.prefix,
            suffix=selector.suffix,
            start=selector.start,
            end=selector.end,
        )


class _Record:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CriterionDefinitionVersion(_Record):
    id: str
    stable_key: str
    version: int
    title: str
    description: str
    criterion_kind: str
    origin: str
    configuration_schema_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CheckDefinitionVersion(_Record):
    id: str
    stable_key: str
    version: int
    title: str
    mechanism: str
    executor_ref: str
    supported_criterion_kinds_json: str
    input_schema_json: str
    output_schema_json: str
    limitations_json: str
    origin: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CriterionCheckBinding(_Record):
    id: str
    criterion_definition_version_id: str
    check_definition_version_id: str
    configuration_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CriterionActivation(_Record):
    id: str
    criterion_definition_version_id: str
    criterion_check_binding_id: str
    scope_json: str
    is_enabled: int
    is_required: int
    origin: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class ActionSnapshot(_Record):
    id: str
    document_id: str
    document_version_id: str | None
    ydoc_snapshot_sha256: str
    structured_head_sha256: str
    ydoc_generation_sha256: str
    baseline_projection_sha256: str
    projection_sha256: str
    projection_blob_sha256: str
    target_kind: str
    target_selector_json: str
    target_text_sha256: str
    target_blob_sha256: str
    context_boundary_json: str
    allowed_change_ranges_json: str
    egress_boundary_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class EvaluationPlanSnapshot(_Record):
    id: str
    action_snapshot_id: str
    plan_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class EvaluationRun(_Record):
    id: str
    action_snapshot_id: str
    plan_snapshot_id: str
    run_kind: str
    status: str
    canonical_sha256: str
    started_at: str
    completed_at: str | None
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CheckExecution(_Record):
    id: str
    evaluation_run_id: str
    check_definition_version_id: str
    criterion_check_binding_id: str
    mechanism: str
    status: str
    input_sha256: str
    output_sha256: str | None
    diagnostics_json: str
    producer_json: str
    canonical_sha256: str
    started_at: str
    completed_at: str | None
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class EvaluationResult(_Record):
    id: str
    evaluation_run_id: str
    check_execution_id: str
    criterion_definition_version_id: str
    result_kind: str
    severity: str
    message: str
    evidence_selector_json: str | None
    payload_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class RoutingDisposition(_Record):
    id: str
    evaluation_result_id: str
    decision: str
    rationale: str
    policy_snapshot_sha256: str | None
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class ResultRelation(_Record):
    id: str
    evaluation_result_id: str
    relation_kind: str
    target_kind: str
    target_ref: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class ModelCallAuthorizationReceipt(_Record):
    id: str
    action_snapshot_id: str
    plan_snapshot_id: str | None
    provider: str
    model: str
    context_sha256: str
    content_boundary_json: str
    egress_class: str
    cost_ceiling_usd: float
    retry_limit: int
    expires_at: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CothinkItem(_Record):
    id: str
    action_snapshot_id: str
    subtype: str
    purpose: str
    payload_json: str
    rationale: str
    delivery_state: str
    provenance_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CothinkItemStatusEvent(_Record):
    id: str
    cothink_item_id: str
    status: str
    reason: str | None
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CoworkCoordinationJob(_Record):
    """Portable, sanitized binding for one constrained Verify/Co-think job."""

    id: str
    document_id: str
    evaluation_run_id: str | None
    action_snapshot_id: str
    plan_snapshot_id: str | None
    role: str
    parent_job_id: str | None
    authorization_receipt_id: str
    context_sha256: str
    selection_json: str
    request_summary_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CoworkCoordinationStatusEvent(_Record):
    """Append-only, sanitized lifecycle fact for a coordination job."""

    id: str
    coordination_job_id: str
    status: str
    outcome_kind: str | None
    output_sha256: str | None
    error_code: str | None
    message: str | None
    consequence_refs_json: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class CoworkReviewApplication(_Record):
    """One portable committed sitting batch containing applied proposals."""

    id: str
    document_id: str
    applied_proposal_ids_json: str
    canonical_sha256: str
    committed_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class SeededTerminologyExactMatch:
    criterion: CriterionDefinitionVersion
    check: CheckDefinitionVersion
    binding: CriterionCheckBinding
    activation: CriterionActivation


@dataclass(frozen=True, slots=True)
class DeterministicEvaluation:
    plan: EvaluationPlanSnapshot
    run: EvaluationRun
    executions: tuple[CheckExecution, ...]
    results: tuple[EvaluationResult, ...]

    @property
    def execution(self) -> CheckExecution:
        """Backward-compatible singular access for one-check evaluations."""

        if len(self.executions) != 1:
            raise VerifyInvariantViolation(
                "evaluation contains more than one check execution"
            )
        return self.executions[0]


__all__ = [
    "ActionSnapshot",
    "ActionTarget",
    "CheckDefinitionVersion",
    "CheckExecution",
    "CothinkItem",
    "CothinkItemStatusEvent",
    "CoworkCoordinationJob",
    "CoworkCoordinationStatusEvent",
    "CoworkReviewApplication",
    "CriterionActivation",
    "CriterionCheckBinding",
    "CriterionDefinitionVersion",
    "DeterministicEvaluation",
    "EvaluationPlanSnapshot",
    "EvaluationResult",
    "EvaluationRun",
    "ModelCallAuthorizationReceipt",
    "ResultRelation",
    "RoutingDisposition",
    "SeededTerminologyExactMatch",
    "VerifyInvariantViolation",
]
