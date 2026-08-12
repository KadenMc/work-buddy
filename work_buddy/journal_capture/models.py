"""Typed records for the production Journal capture path."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class CaptureTarget(StrEnum):
    AUTO = "auto"
    LOG = "log"
    RUNNING_NOTES = "running_notes"


class CaptureMode(StrEnum):
    DUMB = "dumb"
    SMART = "smart"


class ProcessingState(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProjectionState(StrEnum):
    PENDING = "pending"
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"
    PAUSED_DIVERGED = "paused_diverged"


class EffectState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"


class JournalMigrationComparison(StrEnum):
    PENDING = "pending"
    PARITY = "parity"
    MISMATCH = "mismatch"


class JournalMigrationState(StrEnum):
    SELECTED = "selected"
    SHADOW = "shadow_imported"
    COWORK = "cowork_authoritative"
    LEGACY = "legacy_authoritative"
    PAUSED_DIVERGED = "paused_diverged"
    RETIRED = "retired"


@dataclass(frozen=True)
class JournalCapture:
    capture_id: str
    client_mutation_id: str
    request_sha256: str
    source_ref: str
    representation_id: str
    submission_id: str
    command_id: str
    source_effect_id: str
    source_usage_id: str | None
    day_id: str
    requested_target: CaptureTarget
    resolved_target: CaptureTarget | None
    mode: CaptureMode
    input_mode: str
    stated_at: str | None
    submitted_at: str
    persistence_status: str
    processing_status: ProcessingState
    processing_error_code: str | None
    annotation: Mapping[str, Any] | None
    entry_id: str | None
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    capture_id: str
    day_id: str
    entry_kind: CaptureTarget
    source_ref: str
    content_sha256: str
    markdown: str
    created_at: str
    updated_at: str
    version: int
    resolution_state: str
    processing_status: ProcessingState
    annotation: Mapping[str, Any] | None
    processing_error_code: str | None
    projection_state: ProjectionState
    projection_marker: str
    projection_base_sha256: str | None
    projection_result_sha256: str | None


@dataclass(frozen=True)
class JournalDocumentBinding:
    """Journal-owned reverse pointer to an authoritative Co-work document.

    Exact prose stays in the Journal entry and Co-work document stores.  This
    record is deliberately content-free so Journal can restore navigation and
    authority state without duplicating the source bytes or the document
    causality ledger.
    """

    entry_id: str
    binding_id: str
    store_id: str
    document_id: str
    change_id: str
    source_consumer_id: str
    source_usage_id: str
    source_use_kind: str
    source_disclosure_kind: str
    source_redaction_policy: str
    source_maintenance_state: str
    source_maintenance: Mapping[str, Any]
    cowork_href: str
    content_authority_epoch: int
    entry_version: int
    inspection: Mapping[str, Any]
    state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalDocumentUsageTransition:
    """Crash-recoverable replacement of one document's active Source usage."""

    transition_id: str
    entry_id: str
    binding_id: str
    change_id: str
    prior_usage_id: str
    next_usage_id: str
    next_use_kind: str
    next_disclosure_kind: str
    next_redaction_policy: str
    state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalMigrationRecord:
    """Content-free mirror for one Journal content-entity migration.

    The document-kernel ``DomainDocumentBinding`` remains authoritative for
    content authority and its epoch.  This row records the legacy selection,
    parity evidence, rollback window, and recovery state needed to find and
    repair that canonical binding without retaining another prose copy.
    """

    entity_kind: str
    entity_id: str
    day_id: str
    marker_id: str
    selection_start: int | None
    selection_end: int | None
    selected_file_sha256: str | None
    selected_section_sha256: str | None
    source_ref: str | None
    representation_id: str | None
    source_content_sha256: str | None
    binding_id: str | None
    store_id: str | None
    document_id: str | None
    comparison_state: JournalMigrationComparison
    byte_parity: bool | None
    normalized_parity: bool | None
    structural_parity: bool | None
    rollback_deadline: str | None
    mirrored_state: JournalMigrationState
    mirrored_authority_epoch: int
    projection_state: str
    divergence_source_ref: str | None
    operation_id: str | None
    error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalEffect:
    effect_id: str
    capture_id: str
    effect_type: str
    state: EffectState
    attempts: int
    authorization_fingerprint: str
    authorization_expires_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    error_code: str | None
    created_at: str
    updated_at: str


class JournalCaptureError(RuntimeError):
    """Base class for content-free Journal capture errors."""

    code = "journal_capture_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class JournalCaptureConflict(JournalCaptureError):
    code = "journal_capture_conflict"


class JournalCaptureValidationError(JournalCaptureError):
    code = "journal_capture_invalid"


class JournalProjectionError(JournalCaptureError):
    code = "journal_projection_failed"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class JournalProjectionDiverged(JournalProjectionError):
    code = "journal_projection_diverged"
