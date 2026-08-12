"""Typed task-note compatibility-migration records.

The task master list is intentionally absent from these models.  Only the
linked Markdown body identified by the stable ``note_uuid`` can move between
legacy Markdown and a bound Co-work document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthorityState(StrEnum):
    LEGACY = "legacy_authoritative"
    SHADOW = "shadow_imported"
    COWORK = "cowork_authoritative"
    RETIRED = "retired"


class ComparisonState(StrEnum):
    PENDING = "pending"
    PARITY = "parity"
    MISMATCH = "mismatch"


class ProjectionState(StrEnum):
    NONE = "none"
    CURRENT = "current"
    PAUSED_DIVERGED = "paused_diverged"
    RETIRED = "retired"


class SagaState(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    RECOVERABLE = "recoverable"


class SourceDependencyState(StrEnum):
    RESERVED = "reserved"
    ACKNOWLEDGED = "acknowledged"
    RELEASED = "released"
    REVIEW_REQUIRED = "review_required"


class ChangeOperationState(StrEnum):
    PREPARED = "prepared"
    SOURCE_RESERVED = "source_reserved"
    DOCUMENT_COMMITTED = "document_committed"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    RECOVERABLE = "recoverable"


@dataclass(frozen=True, slots=True)
class AuthorityEpoch:
    domain_namespace: str
    entity_kind: str
    entity_id: str
    state: AuthorityState
    epoch: int
    domain_revision: str | None
    rollback_deadline: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskNoteMigration:
    note_uuid: str
    source_ref: str | None
    source_content_sha256: str | None
    legacy_file_sha256: str | None
    legacy_normalized_sha256: str | None
    document_projection_sha256: str | None
    document_normalized_sha256: str | None
    byte_parity: bool | None
    normalized_parity: bool | None
    comparison_state: ComparisonState
    binding_id: str | None
    store_id: str | None
    document_id: str | None
    projection_base_sha256: str | None
    projection_result_sha256: str | None
    projection_generation: int
    projection_document_head: str | None
    projection_state: ProjectionState
    divergence_source_ref: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskNoteSaga:
    saga_id: str
    operation: str
    idempotency_key: str
    request_sha256: str
    note_uuid: str
    task_id: str | None
    state: SagaState
    required_steps: tuple[str, ...]
    completed_steps: tuple[str, ...]
    attempts: int
    error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskNoteSourceDependency:
    usage_id: str
    note_uuid: str
    consumer_id: str
    relationship: str
    source_ref: str
    representation_id: str
    content_sha256: str
    redaction_epoch: int
    binding_id: str
    store_id: str
    document_id: str
    result_document_head_sha256: str | None
    state: SourceDependencyState
    review_reason: str | None
    superseded_by_usage_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskNoteChangeOperation:
    operation_id: str
    idempotency_key: str
    request_sha256: str
    note_uuid: str
    state: ChangeOperationState
    source_ref: str | None
    representation_id: str | None
    source_content_sha256: str | None
    source_usage_id: str | None
    change_id: str | None
    result_document_head_sha256: str | None
    projection_state: str | None
    error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskNoteDescriptor:
    note_uuid: str
    item_id: str
    modified_at: float


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    note_uuid: str
    state: ProjectionState
    generation: int
    file_sha256: str | None
    document_head_sha256: str | None
    divergence_source_ref: str | None = None

    @property
    def status(self) -> str:
        """Expose the document-kernel projection status vocabulary.

        Bound Co-work push callers handle Journal and task-note projections
        through the same dispatch seam.  Keep the task-note state machine
        precise internally while presenting the shared committed/paused
        contract at that boundary.
        """

        if self.state is ProjectionState.CURRENT:
            return "committed"
        return self.state.value


__all__ = [
    "AuthorityEpoch",
    "AuthorityState",
    "ComparisonState",
    "ChangeOperationState",
    "ProjectionOutcome",
    "ProjectionState",
    "SagaState",
    "SourceDependencyState",
    "TaskNoteDescriptor",
    "TaskNoteChangeOperation",
    "TaskNoteMigration",
    "TaskNoteSaga",
    "TaskNoteSourceDependency",
]
