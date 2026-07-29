"""Deterministic, lossless JSONL transport for one truth ledger.

The committed export is a recovery format, not a projection. It carries the
profile, every globally ordered ledger row, sanctioned mutation state, and each
live content-addressed blob. Import validates the complete stream before it
writes into a staged sidecar and then publishes that sidecar with one rename.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.truth.contracts import (
    InvariantViolation,
    StorePaths,
    VALID_ACTOR_KINDS,
    VALID_STATUSES,
)
from work_buddy.truth.identity import (
    canonical_json,
    claim_sha256,
    parse_truth_uri,
    sha256_bytes,
)
from work_buddy.truth.migrations import (
    REDACTED_SELECTOR_JSON,
    SCHEMA_VERSION,
    migrate,
)
from work_buddy.truth.profiles import (
    StoreProfile,
    dump_profile,
    normalize_store_id,
    validate_profile,
)
from work_buddy.truth.store import TruthStore


FORMAT_NAME = "work-buddy.truth-ledger"
FORMAT_VERSION = 7
OLDEST_FORMAT_VERSION = 1
_IMPORT_STAGING_PREFIX = ".wbuddy-cowork-import-"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ACTION_TARGET_KINDS = frozenset({"document", "text_quote"})
_ROUTING_DECISIONS = frozenset(
    {"surface", "route_to_correction", "suppress", "defer", "supersede"}
)
_RESULT_RELATION_KINDS = frozenset(
    {"addresses", "rechecks", "supersedes", "derived_from", "related"}
)
_RESULT_RELATION_TARGET_KINDS = frozenset(
    {"evaluation_result", "evaluation_run", "proposal", "cothink_item", "external"}
)
_COTHINK_DELIVERY_STATES = frozenset({"queued", "delivered", "unavailable"})
_COTHINK_ITEM_STATUSES = frozenset({"open", "parked", "dismissed"})
_COTHINK_ITEM_TRANSITIONS = {
    "open": frozenset({"parked", "dismissed"}),
    "parked": frozenset({"dismissed"}),
    "dismissed": frozenset(),
}
_COTHINK_STATUS_DOMAIN = b"work-buddy:cothink-item-status:v1\0"
_COORDINATION_ROLES = frozenset(
    {"specialist", "reviser", "coordinator", "cothink"}
)
_COORDINATION_STATUSES = frozenset(
    {
        "prepared",
        "launching",
        "running",
        "submitted",
        "completed",
        "unavailable",
        "failed",
    }
)
_COORDINATION_OUTCOMES = frozenset(
    {
        "typed_submission_received",
        "routing_completed",
        "revision_requested",
        "revision_candidate_prepared",
        "correction_routing_completed",
        "completed_with_item",
        "completed_no_useful_item",
        "unavailable",
    }
)
_COORDINATION_TRANSITIONS = {
    "prepared": frozenset(
        {"launching", "running", "submitted", "unavailable", "failed"}
    ),
    "launching": frozenset(
        {"running", "submitted", "unavailable", "failed"}
    ),
    "running": frozenset({"submitted", "unavailable", "failed"}),
    "submitted": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "unavailable": frozenset(),
    "failed": frozenset(),
}


class TruthExportError(InvariantViolation):
    """A live store cannot be represented losslessly."""


class UncompactedDocumentError(TruthExportError):
    """A structured update tail must be compacted before explicit export."""

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(
            f"uncompacted_document:{document_id}: compact the Y.Doc before export"
        )


class TruthImportError(InvariantViolation):
    """An export stream cannot safely rebuild a truth store."""


class StoreIdentityCollision(TruthImportError):
    """The imported store identity is already live at another path."""


class StoreRegistry(Protocol):
    """Read seam for the machine-level truth registry."""

    def paths_for_store_id(self, store_id: str) -> Iterable[str | Path]:
        """Return every registered path carrying ``store_id``."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    sha256: str
    record_count: int
    blob_count: int
    last_seq: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    store: TruthStore
    source_format_version: int
    record_count: int
    blob_count: int


@dataclass(frozen=True, slots=True)
class _DataRecord:
    seq: int
    record_type: str
    record_key: str
    record: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _BlobRecord:
    content_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _Bundle:
    source_format_version: int
    store_info: Mapping[str, Any]
    profile: Mapping[str, Any]
    records: tuple[_DataRecord, ...]
    blobs: tuple[_BlobRecord, ...]


_RECORD_COLUMNS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "evidence": (
        "evidence",
        (
            "id",
            "kind",
            "source_locator",
            "content_sha256",
            "content",
            "content_path",
            "media_type",
            "acquired_at",
            "acquired_by_kind",
            "acquired_by_ref",
            "acquisition_method",
            "trust_class",
            "derived_from_store",
            "meta_json",
            "redacted_at",
            "created_at",
        ),
    ),
    "evidence_span": (
        "evidence_spans",
        (
            "id",
            "evidence_id",
            "selector_json",
            "quote_exact",
            "span_sha256",
            "author_kind",
            "author_ref",
            "redacted_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "claim": (
        "claims",
        (
            "id",
            "proposition",
            "canonical_sha256",
            "claim_kind",
            "structured_json",
            "scope",
            "valid_from",
            "valid_to",
            "confidence_extraction",
            "meta_json",
            "redacted_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "derivation": (
        "derivations",
        (
            "id",
            "claim_id",
            "method",
            "producer_kind",
            "producer_ref",
            "confidence",
            "rationale",
            "created_at",
        ),
    ),
    "derivation_premise": (
        "derivation_premises",
        ("derivation_id", "premise_kind", "premise_ref"),
    ),
    "claim_link": (
        "claim_links",
        (
            "id",
            "from_claim_id",
            "link_type",
            "to_kind",
            "to_ref",
            "role_json",
            "target_fingerprint",
            "fingerprint_reviewed_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "link_retraction": (
        "link_retractions",
        ("link_id", "at", "actor_kind", "actor_ref", "reason"),
    ),
    "claim_status_event": (
        "claim_status_events",
        (
            "seq",
            "id",
            "claim_id",
            "status",
            "at",
            "actor_kind",
            "actor_ref",
            "basis_kind",
            "basis_ref",
            "note",
        ),
    ),
    "gesture": (
        "gestures",
        (
            "id",
            "at",
            "surface",
            "actor_ref",
            "kind",
            "subject_ref",
            "payload_sha256",
            "payload_excerpt",
            "context_sha256",
            "expires_at",
            "consumed_at",
        ),
    ),
    "redaction_event": (
        "redaction_events",
        (
            "id",
            "subject_kind",
            "subject_ref",
            "at",
            "actor_ref",
            "basis_kind",
            "basis_ref",
            "reason",
        ),
    ),
    "sweep": (
        "sweeps",
        ("id", "kind", "at", "params_json"),
    ),
    "sweep_finding": (
        "sweep_findings",
        (
            "id",
            "sweep_id",
            "subject_kind",
            "subject_ref",
            "finding",
            "resolved_at",
            "resolved_by_ref",
        ),
    ),
    "document": (
        "documents",
        (
            "id",
            "path",
            "title",
            "document_class",
            "content_sha256",
            "ydoc_snapshot_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "meta_json",
        ),
    ),
    "document_version": (
        "document_versions",
        (
            "id",
            "document_id",
            "kind",
            "projection_sha256",
            "ydoc_snapshot_sha256",
            "structured_head_sha256",
            "created_at",
            "actor_kind",
            "actor_ref",
            "detail",
        ),
    ),
    "document_span": (
        "document_spans",
        (
            "id",
            "document_id",
            "selector_json",
            "quote_exact",
            "span_sha256",
            "author_kind",
            "author_ref",
            "created_at",
            "created_by_kind",
            "created_by_ref",
        ),
    ),
    "expression": (
        "expressions",
        (
            "id",
            "document_span_id",
            "claim_ref_kind",
            "claim_ref",
            "role",
            "claim_canonical_sha256",
            "span_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "meta_json",
        ),
    ),
    "proposal": (
        "proposals",
        (
            "id",
            "document_id",
            "base_content_sha256",
            "base_structured_head_sha256",
            "selector_json",
            "quote_exact",
            "span_sha256",
            "replacement",
            "rationale",
            "tldr",
            "claim_refs_json",
            "canonical_sha256",
            "dedup_key",
            "expires_at",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "meta_json",
            "redacted_at",
        ),
    ),
    "proposal_status_event": (
        "proposal_status_events",
        (
            "seq",
            "id",
            "proposal_id",
            "status",
            "decision",
            "at",
            "actor_kind",
            "actor_ref",
            "basis_kind",
            "basis_ref",
            "note",
        ),
    ),
    "doc_event": (
        "doc_events",
        (
            "id",
            "document_id",
            "kind",
            "at",
            "actor_kind",
            "actor_ref",
            "content_sha256",
            "ydoc_snapshot_sha256",
            "detail",
        ),
    ),
    "criterion_definition_version": (
        "criterion_definition_versions",
        (
            "id",
            "stable_key",
            "version",
            "title",
            "description",
            "criterion_kind",
            "origin",
            "configuration_schema_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "check_definition_version": (
        "check_definition_versions",
        (
            "id",
            "stable_key",
            "version",
            "title",
            "mechanism",
            "executor_ref",
            "supported_criterion_kinds_json",
            "input_schema_json",
            "output_schema_json",
            "limitations_json",
            "origin",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "criterion_check_binding": (
        "criterion_check_bindings",
        (
            "id",
            "criterion_definition_version_id",
            "check_definition_version_id",
            "configuration_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "criterion_activation": (
        "criterion_activations",
        (
            "id",
            "criterion_definition_version_id",
            "criterion_check_binding_id",
            "scope_json",
            "is_enabled",
            "is_required",
            "origin",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "action_snapshot": (
        "action_snapshots",
        (
            "id",
            "document_id",
            "document_version_id",
            "ydoc_snapshot_sha256",
            "structured_head_sha256",
            "ydoc_generation_sha256",
            "baseline_projection_sha256",
            "projection_sha256",
            "projection_blob_sha256",
            "target_kind",
            "target_selector_json",
            "target_text_sha256",
            "target_blob_sha256",
            "context_boundary_json",
            "allowed_change_ranges_json",
            "egress_boundary_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "evaluation_plan_snapshot": (
        "evaluation_plan_snapshots",
        (
            "id",
            "action_snapshot_id",
            "plan_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "evaluation_run": (
        "evaluation_runs",
        (
            "id",
            "action_snapshot_id",
            "plan_snapshot_id",
            "run_kind",
            "status",
            "canonical_sha256",
            "started_at",
            "completed_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "check_execution": (
        "check_executions",
        (
            "id",
            "evaluation_run_id",
            "check_definition_version_id",
            "criterion_check_binding_id",
            "mechanism",
            "status",
            "input_sha256",
            "output_sha256",
            "diagnostics_json",
            "producer_json",
            "canonical_sha256",
            "started_at",
            "completed_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "evaluation_result": (
        "evaluation_results",
        (
            "id",
            "evaluation_run_id",
            "check_execution_id",
            "criterion_definition_version_id",
            "result_kind",
            "severity",
            "message",
            "evidence_selector_json",
            "payload_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "routing_disposition": (
        "routing_dispositions",
        (
            "id",
            "evaluation_result_id",
            "decision",
            "rationale",
            "policy_snapshot_sha256",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "result_relation": (
        "result_relations",
        (
            "id",
            "evaluation_result_id",
            "relation_kind",
            "target_kind",
            "target_ref",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "model_call_authorization_receipt": (
        "model_call_authorization_receipts",
        (
            "id",
            "action_snapshot_id",
            "plan_snapshot_id",
            "provider",
            "model",
            "context_sha256",
            "content_boundary_json",
            "egress_class",
            "cost_ceiling_usd",
            "retry_limit",
            "expires_at",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "cothink_item": (
        "cothink_items",
        (
            "id",
            "action_snapshot_id",
            "subtype",
            "purpose",
            "payload_json",
            "rationale",
            "delivery_state",
            "provenance_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "cothink_item_status_event": (
        "cothink_item_status_events",
        (
            "id",
            "cothink_item_id",
            "status",
            "reason",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "cowork_coordination_job": (
        "cowork_coordination_jobs",
        (
            "id",
            "document_id",
            "evaluation_run_id",
            "action_snapshot_id",
            "plan_snapshot_id",
            "role",
            "parent_job_id",
            "authorization_receipt_id",
            "context_sha256",
            "selection_json",
            "request_summary_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "cowork_coordination_status_event": (
        "cowork_coordination_status_events",
        (
            "id",
            "coordination_job_id",
            "status",
            "outcome_kind",
            "output_sha256",
            "error_code",
            "message",
            "consequence_refs_json",
            "canonical_sha256",
            "created_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
    "cowork_review_application": (
        "cowork_review_applications",
        (
            "id",
            "document_id",
            "applied_proposal_ids_json",
            "canonical_sha256",
            "committed_at",
            "created_by_kind",
            "created_by_ref",
            "created_by_meta_json",
        ),
    ),
}

_ID_KEY_TYPES = frozenset(
    {
        "evidence",
        "evidence_span",
        "claim",
        "derivation",
        "claim_link",
        "claim_status_event",
        "gesture",
        "redaction_event",
        "sweep",
        "sweep_finding",
        "document",
        "document_version",
        "document_span",
        "expression",
        "proposal",
        "proposal_status_event",
        "doc_event",
        "criterion_definition_version",
        "check_definition_version",
        "criterion_check_binding",
        "criterion_activation",
        "action_snapshot",
        "evaluation_plan_snapshot",
        "evaluation_run",
        "check_execution",
        "evaluation_result",
        "routing_disposition",
        "result_relation",
        "model_call_authorization_receipt",
        "cothink_item",
        "cothink_item_status_event",
        "cowork_coordination_job",
        "cowork_coordination_status_event",
        "cowork_review_application",
    }
)

_DOC_EVENT_KINDS = frozenset(
    {
        "registered",
        "imported",
        "initialized",
        "repaired",
        "materialized",
        "snapshot_compacted",
        "drift_detected",
        "reimported",
        "retired",
        "session_opened",
        "session_closed",
    }
)

_EXPRESSION_ROLES = frozenset({"quote", "paraphrase", "summary", "instantiation"})

_PROPOSAL_STATUSES = frozenset({"open", "applied", "closed", "expired"})

_PROPOSAL_DECISIONS = frozenset(
    {
        "confirm",
        "edit_confirm",
        "reject_plain",
        "reject_as_false",
        "reject_as_preference",
        "redirect",
        "defer",
        "endorse",
        "dismiss",
    }
)

_LINK_TARGETS: Mapping[str, frozenset[str]] = {
    "supports_span": frozenset({"evidence_span"}),
    "about_entity": frozenset({"entity"}),
    "supersedes": frozenset({"claim"}),
    "conflicts_with": frozenset({"claim"}),
    "refutes": frozenset({"claim"}),
    "cites_external": frozenset({"external_uri"}),
    "relates_to": frozenset({"claim", "entity", "external_uri"}),
}


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TruthExportError("export data is not canonical JSON") from exc
    return text.encode("utf-8") + b"\n"


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TruthImportError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TruthImportError(f"{label} keys must be strings")
    return dict(value)


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: Iterable[str],
    label: str,
) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        raise TruthImportError(f"{label} has invalid keys: {', '.join(detail)}")


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TruthImportError(f"{label} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise TruthImportError(f"{label} must be at least {minimum}")
    return value


def _record_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _RECORD_ID_RE.fullmatch(value) is None:
        raise TruthImportError(f"{label} must be a lowercase 32-hex id")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise TruthImportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TruthImportError(f"{label} must be a nonempty string")
    return value


def _json_value(value: Any, label: str, *, mapping: bool = False) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TruthImportError(f"{label} must be JSON text or null")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TruthImportError(f"{label} is invalid JSON") from exc
    if mapping and not isinstance(parsed, dict):
        raise TruthImportError(f"{label} must contain a JSON object")
    return parsed


def _finite_confidence(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TruthImportError(f"{label} must be a number from 0 to 1")
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise TruthImportError(f"{label} must be a finite number from 0 to 1")


def _actor_record(row: Mapping[str, Any], label: str) -> None:
    if row["created_by_kind"] not in VALID_ACTOR_KINDS:
        raise TruthImportError(f"{label} has an invalid actor kind")
    _json_value(
        row.get("created_by_meta_json"),
        f"{label}.created_by_meta_json",
        mapping=True,
    )


def _portable_record(row: Mapping[str, Any], label: str) -> None:
    _digest(row["canonical_sha256"], f"{label}.canonical_sha256")
    _timestamp(row["created_at"], f"{label}.created_at")
    _actor_record(row, label)


def _timestamp(value: Any, label: str) -> None:
    text = _nonempty_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TruthImportError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TruthImportError(f"{label} must carry a UTC offset")


def _record_key(record_type: str, row: Mapping[str, Any]) -> str:
    if record_type in _ID_KEY_TYPES:
        return _record_id(row.get("id"), f"{record_type}.id")
    if record_type == "link_retraction":
        return _record_id(row.get("link_id"), "link_retraction.link_id")
    if record_type == "derivation_premise":
        derivation_id = _record_id(
            row.get("derivation_id"), "derivation_premise.derivation_id"
        )
        premise_ref = _nonempty_text(
            row.get("premise_ref"), "derivation_premise.premise_ref"
        )
        return canonical_json(
            {"derivation_id": derivation_id, "premise_ref": premise_ref}
        )
    raise TruthImportError(f"unsupported ledger record type {record_type!r}")


def _validate_header(bundle: _Bundle) -> StoreProfile:
    info = _require_mapping(bundle.store_info, "store_info")
    _require_exact_keys(
        info,
        {"store_id", "profile", "schema_version", "title", "created_at"},
        "store_info",
    )
    try:
        normalized_store_id = normalize_store_id(info["store_id"])
    except InvariantViolation as exc:
        raise TruthImportError(str(exc)) from exc
    if info["store_id"] != normalized_store_id:
        raise TruthImportError("store_info.store_id must use lowercase UUID hex")
    profile_name = _nonempty_text(info["profile"], "store_info.profile")
    schema_version = _positive_int(info["schema_version"], "schema_version")
    if schema_version > SCHEMA_VERSION:
        raise TruthImportError(
            f"store schema v{schema_version} is newer than supported v{SCHEMA_VERSION}"
        )
    if info["title"] is not None and not isinstance(info["title"], str):
        raise TruthImportError("store_info.title must be text or null")
    _timestamp(info["created_at"], "store_info.created_at")
    try:
        profile = validate_profile(bundle.profile)
    except InvariantViolation as exc:
        raise TruthImportError(str(exc)) from exc
    if (
        profile.store_id != normalized_store_id
        or profile.profile != profile_name
        or profile.title != info["title"]
    ):
        raise TruthImportError("profile identity does not match store_info")
    return profile


def _validate_claim_refs(value: Any, label: str) -> None:
    """Validate the one frozen claim_refs shape: a list of {claim, role}."""
    if not isinstance(value, list):
        raise TruthImportError(f"{label} must be a list of claim references")
    for entry in value:
        if not isinstance(entry, Mapping):
            raise TruthImportError(f"{label} entries must be objects")
        if set(entry) != {"claim", "role"}:
            raise TruthImportError(f"{label} entries must carry only claim and role")
        _nonempty_text(entry["claim"], f"{label} claim")
        if entry["role"] not in _EXPRESSION_ROLES:
            raise TruthImportError(f"{label} role is invalid")


def _validate_record_values(item: _DataRecord) -> None:
    row = item.record
    record_type = item.record_type
    _, columns = _RECORD_COLUMNS[record_type]
    _require_exact_keys(row, columns, f"{record_type} record")
    computed_key = _record_key(record_type, row)
    if computed_key != item.record_key:
        raise TruthImportError(
            f"{record_type} record_key does not match its primary key"
        )

    if record_type == "evidence":
        digest = _digest(row["content_sha256"], "evidence.content_sha256")
        _nonempty_text(row["kind"], "evidence.kind")
        _nonempty_text(row["source_locator"], "evidence.source_locator")
        if not urlparse(row["source_locator"]).scheme:
            raise TruthImportError("evidence.source_locator requires a URI scheme")
        content = row["content"]
        content_path = row["content_path"]
        if content is not None and not isinstance(content, str):
            raise TruthImportError("evidence.content must be text or null")
        if content is not None and content_path is not None:
            raise TruthImportError("evidence cannot contain inline and blob content")
        if content is not None and sha256_bytes(content.encode("utf-8")) != digest:
            raise TruthImportError("inline evidence does not match content_sha256")
        if content_path is not None and content_path != f"blobs/{digest}":
            raise TruthImportError("evidence.content_path must match content_sha256")
        if row["redacted_at"] is not None:
            if content is not None or content_path is not None:
                raise TruthImportError("redacted evidence cannot retain content")
            _timestamp(row["redacted_at"], "evidence.redacted_at")
        if row["derived_from_store"] is not None:
            try:
                derived = normalize_store_id(row["derived_from_store"])
            except InvariantViolation as exc:
                raise TruthImportError(str(exc)) from exc
            if derived != row["derived_from_store"]:
                raise TruthImportError("derived_from_store must use lowercase UUID hex")
        _json_value(row["meta_json"], "evidence.meta_json", mapping=True)
        _timestamp(row["acquired_at"], "evidence.acquired_at")
        _timestamp(row["created_at"], "evidence.created_at")
        return

    if record_type == "evidence_span":
        digest = _digest(row["span_sha256"], "evidence_span.span_sha256")
        _json_value(row["selector_json"], "evidence_span.selector_json")
        quote = row["quote_exact"]
        if quote is not None and not isinstance(quote, str):
            raise TruthImportError("evidence_span.quote_exact must be text or null")
        if quote is not None and sha256_bytes(quote.encode("utf-8")) != digest:
            raise TruthImportError("evidence span quote does not match span_sha256")
        if row["redacted_at"] is not None:
            if quote is not None:
                raise TruthImportError("redacted evidence span cannot retain its quote")
            _timestamp(row["redacted_at"], "evidence_span.redacted_at")
        elif quote is None:
            raise TruthImportError("live evidence span must retain its exact quote")
        _timestamp(row["created_at"], "evidence_span.created_at")
        return

    if record_type == "claim":
        digest = _digest(row["canonical_sha256"], "claim.canonical_sha256")
        proposition = _nonempty_text(row["proposition"], "claim.proposition")
        structured = _json_value(
            row["structured_json"], "claim.structured_json", mapping=True
        )
        _json_value(row["meta_json"], "claim.meta_json", mapping=True)
        if row["redacted_at"] is not None:
            if proposition != "[redacted]" or structured is not None:
                raise TruthImportError("redacted claim has retained claim content")
            _timestamp(row["redacted_at"], "claim.redacted_at")
        else:
            try:
                expected = claim_sha256(
                    proposition=proposition,
                    claim_kind=row["claim_kind"],
                    structured=structured,
                    scope=row["scope"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                )
            except (TypeError, ValueError) as exc:
                raise TruthImportError("claim canonical payload is invalid") from exc
            if expected != digest:
                raise TruthImportError("claim content does not match canonical_sha256")
        _finite_confidence(row["confidence_extraction"], "claim confidence")
        _timestamp(row["created_at"], "claim.created_at")
        return

    if record_type == "derivation":
        _finite_confidence(row["confidence"], "derivation confidence")
        _timestamp(row["created_at"], "derivation.created_at")
        return

    if record_type == "derivation_premise":
        if row["premise_kind"] not in {"local", "uri"}:
            raise TruthImportError("derivation premise kind must be local or uri")
        if row["premise_kind"] == "uri":
            try:
                parsed = parse_truth_uri(row["premise_ref"])
            except ValueError as exc:
                raise TruthImportError("derivation premise URI is malformed") from exc
            if parsed.kind != "claim":
                raise TruthImportError("derivation URI premises must target claims")
        return

    if record_type == "claim_link":
        link_type = _nonempty_text(row["link_type"], "claim_link.link_type")
        to_kind = _nonempty_text(row["to_kind"], "claim_link.to_kind")
        if link_type not in _LINK_TARGETS or to_kind not in _LINK_TARGETS[link_type]:
            raise TruthImportError("claim link type and target kind are incompatible")
        _json_value(row["role_json"], "claim_link.role_json", mapping=True)
        if row["target_fingerprint"] is not None:
            _digest(row["target_fingerprint"], "claim_link.target_fingerprint")
        if to_kind == "external_uri" and not urlparse(row["to_ref"]).scheme:
            raise TruthImportError("external claim links require a URI target")
        _timestamp(row["created_at"], "claim_link.created_at")
        return

    if record_type == "link_retraction":
        _timestamp(row["at"], "link_retraction.at")
        return

    if record_type == "claim_status_event":
        _positive_int(row["seq"], "claim_status_event.seq")
        if row["status"] not in VALID_STATUSES:
            raise TruthImportError("claim status event has an invalid status")
        if row["actor_kind"] not in VALID_ACTOR_KINDS:
            raise TruthImportError("claim status event has an invalid actor kind")
        _timestamp(row["at"], "claim_status_event.at")
        return

    if record_type == "gesture":
        _digest(row["payload_sha256"], "gesture.payload_sha256")
        if row["context_sha256"] is not None:
            _digest(row["context_sha256"], "gesture.context_sha256")
        _timestamp(row["at"], "gesture.at")
        if row["expires_at"] is not None:
            _timestamp(row["expires_at"], "gesture.expires_at")
        if row["consumed_at"] is not None:
            _timestamp(row["consumed_at"], "gesture.consumed_at")
        return

    if record_type == "redaction_event":
        if row["subject_kind"] not in {"claim", "evidence", "span", "proposal"}:
            raise TruthImportError("redaction subject kind is invalid")
        if row["basis_kind"] not in {"gesture", "policy"}:
            raise TruthImportError("redaction basis kind is invalid")
        _timestamp(row["at"], "redaction_event.at")
        return

    if record_type == "sweep":
        _json_value(row["params_json"], "sweep.params_json", mapping=True)
        _timestamp(row["at"], "sweep.at")
        return

    if record_type == "sweep_finding":
        if row["resolved_at"] is not None:
            _timestamp(row["resolved_at"], "sweep_finding.resolved_at")
        return

    if record_type == "document":
        _digest(row["content_sha256"], "document.content_sha256")
        _nonempty_text(row["path"], "document.path")
        if row["ydoc_snapshot_sha256"] is not None:
            _digest(row["ydoc_snapshot_sha256"], "document.ydoc_snapshot_sha256")
        _json_value(row["meta_json"], "document.meta_json", mapping=True)
        _timestamp(row["created_at"], "document.created_at")
        return

    if record_type == "document_version":
        if row["kind"] not in {
            "initial_import",
            "repaired",
            "materialized",
            "reimported",
            "snapshot_compacted",
        }:
            raise TruthImportError("document version kind is invalid")
        _digest(row["projection_sha256"], "document_version.projection_sha256")
        _digest(
            row["ydoc_snapshot_sha256"],
            "document_version.ydoc_snapshot_sha256",
        )
        _digest(
            row["structured_head_sha256"],
            "document_version.structured_head_sha256",
        )
        if row["actor_kind"] not in VALID_ACTOR_KINDS:
            raise TruthImportError("document version actor kind is invalid")
        _timestamp(row["created_at"], "document_version.created_at")
        return

    if record_type == "expression":
        if row["role"] not in _EXPRESSION_ROLES:
            raise TruthImportError("expression role is invalid")
        if row["claim_ref_kind"] not in {"local", "uri"}:
            raise TruthImportError("expression claim_ref_kind must be local or uri")
        if row["claim_ref_kind"] == "uri":
            try:
                parsed = parse_truth_uri(row["claim_ref"])
            except ValueError as exc:
                raise TruthImportError("expression claim_ref URI is malformed") from exc
            if parsed.kind != "claim":
                raise TruthImportError("expression URI refs must target claims")
        else:
            _nonempty_text(row["claim_ref"], "expression.claim_ref")
        _digest(row["claim_canonical_sha256"], "expression.claim_canonical_sha256")
        _digest(row["span_sha256"], "expression.span_sha256")
        _json_value(row["meta_json"], "expression.meta_json", mapping=True)
        _timestamp(row["created_at"], "expression.created_at")
        return

    if record_type == "proposal":
        _digest(row["canonical_sha256"], "proposal.canonical_sha256")
        _digest(row["dedup_key"], "proposal.dedup_key")
        _digest(row["base_content_sha256"], "proposal.base_content_sha256")
        if row["base_structured_head_sha256"] is not None:
            _digest(
                row["base_structured_head_sha256"],
                "proposal.base_structured_head_sha256",
            )
        _digest(row["span_sha256"], "proposal.span_sha256")
        claim_refs = _json_value(row["claim_refs_json"], "proposal.claim_refs_json")
        if claim_refs is not None:
            _validate_claim_refs(claim_refs, "proposal.claim_refs_json")
        _json_value(row["meta_json"], "proposal.meta_json", mapping=True)
        if row["redacted_at"] is not None:
            if (
                row["quote_exact"] is not None
                or row["replacement"] is not None
                or row["rationale"] is not None
                or row["tldr"] is not None
                or row["claim_refs_json"] is not None
            ):
                raise TruthImportError("redacted proposal has retained content")
            if row["selector_json"] != REDACTED_SELECTOR_JSON:
                raise TruthImportError(
                    "redacted proposal must carry the redacted selector"
                )
            _timestamp(row["redacted_at"], "proposal.redacted_at")
        else:
            _nonempty_text(row["quote_exact"], "proposal.quote_exact")
            _json_value(row["selector_json"], "proposal.selector_json")
        if row["expires_at"] is not None:
            _timestamp(row["expires_at"], "proposal.expires_at")
        _timestamp(row["created_at"], "proposal.created_at")
        return

    if record_type == "proposal_status_event":
        _positive_int(row["seq"], "proposal_status_event.seq")
        if row["status"] not in _PROPOSAL_STATUSES:
            raise TruthImportError("proposal status event has an invalid status")
        if row["decision"] is not None and row["decision"] not in _PROPOSAL_DECISIONS:
            raise TruthImportError("proposal status event has an invalid decision")
        if row["actor_kind"] not in VALID_ACTOR_KINDS:
            raise TruthImportError("proposal status event has an invalid actor kind")
        if row["basis_kind"] not in {"gesture", "rule", "sweep"}:
            raise TruthImportError("proposal status event has an invalid basis kind")
        _timestamp(row["at"], "proposal_status_event.at")
        return

    if record_type == "doc_event":
        if row["kind"] not in _DOC_EVENT_KINDS:
            raise TruthImportError("doc event has an invalid kind")
        if row["actor_kind"] not in VALID_ACTOR_KINDS:
            raise TruthImportError("doc event has an invalid actor kind")
        if row["content_sha256"] is not None:
            _digest(row["content_sha256"], "doc_event.content_sha256")
        if row["ydoc_snapshot_sha256"] is not None:
            _digest(row["ydoc_snapshot_sha256"], "doc_event.ydoc_snapshot_sha256")
        _timestamp(row["at"], "doc_event.at")
        return

    if record_type == "criterion_definition_version":
        _positive_int(row["version"], "criterion definition version")
        for field in ("stable_key", "title", "description", "criterion_kind", "origin"):
            _nonempty_text(row[field], f"criterion definition.{field}")
        _json_value(
            row["configuration_schema_json"],
            "criterion definition.configuration_schema_json",
            mapping=True,
        )
        _portable_record(row, "criterion definition")
        return

    if record_type == "check_definition_version":
        _positive_int(row["version"], "check definition version")
        for field in (
            "stable_key",
            "title",
            "mechanism",
            "executor_ref",
            "origin",
        ):
            _nonempty_text(row[field], f"check definition.{field}")
        supported = _json_value(
            row["supported_criterion_kinds_json"],
            "check definition.supported_criterion_kinds_json",
        )
        if not isinstance(supported, list) or not supported:
            raise TruthImportError(
                "check definition supported criterion kinds must be a nonempty list"
            )
        for field in (
            "input_schema_json",
            "output_schema_json",
            "limitations_json",
        ):
            _json_value(row[field], f"check definition.{field}")
        _portable_record(row, "check definition")
        return

    if record_type == "criterion_check_binding":
        _json_value(
            row["configuration_json"],
            "criterion check binding.configuration_json",
            mapping=True,
        )
        _portable_record(row, "criterion check binding")
        return

    if record_type == "criterion_activation":
        _json_value(
            row["scope_json"],
            "criterion activation.scope_json",
            mapping=True,
        )
        if row["is_enabled"] not in {0, 1} or row["is_required"] not in {0, 1}:
            raise TruthImportError("criterion activation booleans must be 0 or 1")
        _nonempty_text(row["origin"], "criterion activation.origin")
        _portable_record(row, "criterion activation")
        return

    if record_type == "action_snapshot":
        for field in (
            "ydoc_snapshot_sha256",
            "structured_head_sha256",
            "ydoc_generation_sha256",
            "baseline_projection_sha256",
            "projection_sha256",
            "projection_blob_sha256",
            "target_text_sha256",
            "target_blob_sha256",
        ):
            _digest(row[field], f"action snapshot.{field}")
        if row["projection_sha256"] != row["projection_blob_sha256"]:
            raise TruthImportError(
                "action snapshot projection blob must match projection_sha256"
            )
        if row["target_text_sha256"] != row["target_blob_sha256"]:
            raise TruthImportError(
                "action snapshot target blob must match target_text_sha256"
            )
        if row["target_kind"] not in _ACTION_TARGET_KINDS:
            raise TruthImportError("action snapshot has an invalid target kind")
        _json_value(
            row["target_selector_json"],
            "action snapshot.target_selector_json",
            mapping=True,
        )
        _json_value(
            row["context_boundary_json"],
            "action snapshot.context_boundary_json",
            mapping=True,
        )
        allowed = _json_value(
            row["allowed_change_ranges_json"],
            "action snapshot.allowed_change_ranges_json",
        )
        if not isinstance(allowed, list):
            raise TruthImportError(
                "action snapshot allowed change ranges must be a list"
            )
        _json_value(
            row["egress_boundary_json"],
            "action snapshot.egress_boundary_json",
            mapping=True,
        )
        _portable_record(row, "action snapshot")
        return

    if record_type == "evaluation_plan_snapshot":
        _json_value(
            row["plan_json"],
            "evaluation plan snapshot.plan_json",
            mapping=True,
        )
        _portable_record(row, "evaluation plan snapshot")
        return

    if record_type == "evaluation_run":
        _nonempty_text(row["run_kind"], "evaluation run.run_kind")
        _nonempty_text(row["status"], "evaluation run.status")
        _timestamp(row["started_at"], "evaluation run.started_at")
        if row["completed_at"] is not None:
            _timestamp(row["completed_at"], "evaluation run.completed_at")
        _digest(row["canonical_sha256"], "evaluation run.canonical_sha256")
        _actor_record(row, "evaluation run")
        return

    if record_type == "check_execution":
        for field in ("mechanism", "status"):
            _nonempty_text(row[field], f"check execution.{field}")
        _digest(row["input_sha256"], "check execution.input_sha256")
        if row["output_sha256"] is not None:
            _digest(row["output_sha256"], "check execution.output_sha256")
        _json_value(
            row["diagnostics_json"],
            "check execution.diagnostics_json",
            mapping=True,
        )
        _json_value(
            row["producer_json"],
            "check execution.producer_json",
            mapping=True,
        )
        _timestamp(row["started_at"], "check execution.started_at")
        if row["completed_at"] is not None:
            _timestamp(row["completed_at"], "check execution.completed_at")
        _digest(row["canonical_sha256"], "check execution.canonical_sha256")
        _actor_record(row, "check execution")
        return

    if record_type == "evaluation_result":
        for field in ("result_kind", "severity", "message"):
            _nonempty_text(row[field], f"evaluation result.{field}")
        if row["evidence_selector_json"] is not None:
            _json_value(
                row["evidence_selector_json"],
                "evaluation result.evidence_selector_json",
                mapping=True,
            )
        _json_value(
            row["payload_json"],
            "evaluation result.payload_json",
            mapping=True,
        )
        _portable_record(row, "evaluation result")
        return

    if record_type == "routing_disposition":
        if row["decision"] not in _ROUTING_DECISIONS:
            raise TruthImportError("routing disposition has an invalid decision")
        _nonempty_text(row["rationale"], "routing disposition.rationale")
        if row["policy_snapshot_sha256"] is not None:
            _digest(
                row["policy_snapshot_sha256"],
                "routing disposition.policy_snapshot_sha256",
            )
        _portable_record(row, "routing disposition")
        return

    if record_type == "result_relation":
        if row["relation_kind"] not in _RESULT_RELATION_KINDS:
            raise TruthImportError("result relation has an invalid relation kind")
        if row["target_kind"] not in _RESULT_RELATION_TARGET_KINDS:
            raise TruthImportError("result relation has an invalid target kind")
        _nonempty_text(row["target_ref"], "result relation.target_ref")
        _portable_record(row, "result relation")
        return

    if record_type == "model_call_authorization_receipt":
        for field in ("provider", "model", "egress_class"):
            _nonempty_text(row[field], f"model authorization.{field}")
        _digest(row["context_sha256"], "model authorization.context_sha256")
        _json_value(
            row["content_boundary_json"],
            "model authorization.content_boundary_json",
            mapping=True,
        )
        cost = row["cost_ceiling_usd"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost))
            or float(cost) < 0
        ):
            raise TruthImportError(
                "model authorization cost ceiling must be a finite nonnegative number"
            )
        _positive_int(
            row["retry_limit"],
            "model authorization.retry_limit",
            allow_zero=True,
        )
        _timestamp(row["expires_at"], "model authorization.expires_at")
        _portable_record(row, "model authorization")
        return

    if record_type == "cothink_item":
        for field in ("subtype", "purpose", "rationale"):
            _nonempty_text(row[field], f"Co-think item.{field}")
        if row["delivery_state"] not in _COTHINK_DELIVERY_STATES:
            raise TruthImportError("Co-think item has an invalid delivery state")
        _json_value(row["payload_json"], "Co-think item.payload_json", mapping=True)
        _json_value(
            row["provenance_json"],
            "Co-think item.provenance_json",
            mapping=True,
        )
        _portable_record(row, "Co-think item")
        return

    if record_type == "cothink_item_status_event":
        if row["status"] not in _COTHINK_ITEM_STATUSES:
            raise TruthImportError("Co-think status event has an invalid status")
        if row["reason"] is not None:
            _nonempty_text(row["reason"], "Co-think status event.reason")
        _portable_record(row, "Co-think status event")
        return

    if record_type == "cowork_coordination_job":
        if row["role"] not in _COORDINATION_ROLES:
            raise TruthImportError("coordination job has an invalid role")
        _digest(row["context_sha256"], "coordination job.context_sha256")
        selection = _json_value(
            row["selection_json"],
            "coordination job.selection_json",
            mapping=True,
        )
        _require_exact_keys(
            selection,
            {
                "provider_id",
                "model_id",
                "provider_label",
                "model_label",
            },
            "coordination job selection",
        )
        for field in ("provider_id", "model_id"):
            _nonempty_text(
                selection[field],
                f"coordination job selection.{field}",
            )
        for field in ("provider_label", "model_label"):
            if not isinstance(selection[field], str):
                raise TruthImportError(
                    f"coordination job selection.{field} must be text"
                )
        request = _json_value(
            row["request_summary_json"],
            "coordination job.request_summary_json",
            mapping=True,
        )
        request_keys = {
            "schema",
            "user_goal",
            "protected_intent",
            "effective_configuration",
            "effective_configuration_sha256",
            "effective_policy_sha256",
            "active_criterion_ids",
            "prior_disposition_ids",
            "prior_human_review_outcome_ids",
            "recheck_of_run_id",
            "recheck_of_proposal_ids",
            "recheck_intent_id",
            "coordinator_stage",
            "requested_revision_result_ids",
            "candidate_evaluations",
        }
        if "recheck_target_confirmation" in request:
            request_keys.add("recheck_target_confirmation")
        _require_exact_keys(
            request,
            request_keys,
            "coordination job request summary",
        )
        if request["schema"] != "work-buddy.cowork-coordination-request/v1":
            raise TruthImportError(
                "coordination job request summary has an invalid schema"
            )
        for field in ("user_goal", "protected_intent"):
            _nonempty_text(
                request[field],
                f"coordination job request summary.{field}",
            )
        configuration = request["effective_configuration"]
        if configuration is not None and not isinstance(configuration, dict):
            raise TruthImportError(
                "coordination effective_configuration must be an object or null"
            )
        for field in (
            "effective_configuration_sha256",
            "effective_policy_sha256",
        ):
            if request[field] is not None:
                _digest(
                    request[field],
                    f"coordination job request summary.{field}",
                )
        for field in (
            "active_criterion_ids",
            "prior_disposition_ids",
            "prior_human_review_outcome_ids",
            "recheck_of_proposal_ids",
            "requested_revision_result_ids",
        ):
            values = request[field]
            if not isinstance(values, list):
                raise TruthImportError(
                    f"coordination job request summary.{field} must be a list"
                )
            normalized = [
                _record_id(
                    value,
                    f"coordination job request summary.{field}",
                )
                for value in values
            ]
            if len(normalized) != len(set(normalized)):
                raise TruthImportError(
                    f"coordination job request summary.{field} has duplicates"
                )
        if request["recheck_of_run_id"] is not None:
            _record_id(
                request["recheck_of_run_id"],
                "coordination job request summary.recheck_of_run_id",
            )
        if request["recheck_intent_id"] is not None:
            _digest(
                request["recheck_intent_id"],
                "coordination job request summary.recheck_intent_id",
            )
        confirmation = request.get("recheck_target_confirmation")
        if confirmation is not None:
            if not isinstance(confirmation, dict):
                raise TruthImportError(
                    "coordination job request summary."
                    "recheck_target_confirmation must be an object or null"
                )
            _require_exact_keys(
                confirmation,
                {
                    "schema",
                    "method",
                    "affirmed_capture_id",
                    "affirmed_action_snapshot_id",
                    "run_capture_id",
                    "target_reference_sha256",
                    "target_text_sha256",
                },
                "coordination job recheck target confirmation",
            )
            if (
                confirmation["schema"]
                != "work-buddy.cowork-recheck-target-confirmation/v1"
                or confirmation["method"]
                != "user_affirmed_working_target"
            ):
                raise TruthImportError(
                    "coordination job recheck target confirmation has an "
                    "unsupported method"
                )
            for field in (
                "affirmed_capture_id",
                "affirmed_action_snapshot_id",
                "run_capture_id",
            ):
                _nonempty_text(
                    confirmation[field],
                    f"coordination job recheck target confirmation.{field}",
                )
            if (
                confirmation["affirmed_capture_id"]
                == confirmation["run_capture_id"]
            ):
                raise TruthImportError(
                    "coordination job recheck target confirmation requires "
                    "separate captures"
                )
            for field in (
                "target_reference_sha256",
                "target_text_sha256",
            ):
                _digest(
                    confirmation[field],
                    f"coordination job recheck target confirmation.{field}",
                )
        if request["coordinator_stage"] not in {
            None,
            "initial",
            "post_revision",
        }:
            raise TruthImportError(
                "coordination job request summary has an invalid stage"
            )
        try:
            from work_buddy.cowork.verify_candidate_evaluation import (
                CandidateEvaluationError,
                sanitize_candidate_evaluations,
            )

            candidate_evaluations = sanitize_candidate_evaluations(
                request["candidate_evaluations"]
            )
        except CandidateEvaluationError as exc:
            raise TruthImportError(str(exc)) from exc
        if candidate_evaluations != request["candidate_evaluations"]:
            raise TruthImportError(
                "coordination job candidate evaluations are not canonical"
            )
        payload = {
            "document_id": row["document_id"],
            "evaluation_run_id": row["evaluation_run_id"],
            "action_snapshot_id": row["action_snapshot_id"],
            "plan_snapshot_id": row["plan_snapshot_id"],
            "role": row["role"],
            "parent_job_id": row["parent_job_id"],
            "authorization_receipt_id": row[
                "authorization_receipt_id"
            ],
            "context_sha256": row["context_sha256"],
            "selection": selection,
            "request_summary": request,
        }
        if (
            sha256_bytes(canonical_json(payload).encode("utf-8"))
            != row["canonical_sha256"]
        ):
            raise TruthImportError(
                "coordination job canonical hash does not match"
            )
        _portable_record(row, "coordination job")
        return

    if record_type == "cowork_coordination_status_event":
        status = row["status"]
        outcome = row["outcome_kind"]
        if status not in _COORDINATION_STATUSES:
            raise TruthImportError("coordination status event has an invalid status")
        if outcome is not None and outcome not in _COORDINATION_OUTCOMES:
            raise TruthImportError(
                "coordination status event has an invalid outcome"
            )
        if status in {"prepared", "launching", "running"} and outcome is not None:
            raise TruthImportError(
                "nonterminal coordination state cannot have an outcome"
            )
        if status == "submitted" and outcome != "typed_submission_received":
            raise TruthImportError(
                "submitted coordination state requires its typed outcome"
            )
        if status in {"unavailable", "failed"} and outcome != "unavailable":
            raise TruthImportError(
                "unavailable coordination state requires unavailable outcome"
            )
        if status == "completed" and outcome in {None, "unavailable"}:
            raise TruthImportError(
                "completed coordination state requires a completed outcome"
            )
        if row["output_sha256"] is not None:
            _digest(
                row["output_sha256"],
                "coordination status event.output_sha256",
            )
        if status in {"unavailable", "failed"}:
            _nonempty_text(
                row["error_code"],
                "coordination status event.error_code",
            )
            _nonempty_text(
                row["message"],
                "coordination status event.message",
            )
        elif row["error_code"] is not None or row["message"] is not None:
            raise TruthImportError(
                "successful coordination status retained error content"
            )
        refs = _json_value(
            row["consequence_refs_json"],
            "coordination status event.consequence_refs_json",
            mapping=True,
        )
        if not set(refs) <= {
            "next_job_id",
            "cothink_item_id",
            "proposal_ids",
            "disposition_ids",
            "requested_revision_result_ids",
        }:
            raise TruthImportError(
                "coordination status event has unsupported consequence refs"
            )
        for field in ("next_job_id", "cothink_item_id"):
            if field in refs:
                _record_id(
                    refs[field],
                    f"coordination status event.{field}",
                )
        for field in (
            "proposal_ids",
            "disposition_ids",
            "requested_revision_result_ids",
        ):
            values = refs.get(field, [])
            if not isinstance(values, list):
                raise TruthImportError(
                    f"coordination status event.{field} must be a list"
                )
            normalized = [
                _record_id(
                    value,
                    f"coordination status event.{field}",
                )
                for value in values
            ]
            if len(normalized) != len(set(normalized)):
                raise TruthImportError(
                    f"coordination status event.{field} has duplicates"
                )
        payload = {
            "coordination_job_id": row["coordination_job_id"],
            "status": status,
            "outcome_kind": outcome,
            "output_sha256": row["output_sha256"],
            "error_code": row["error_code"],
            "message": row["message"],
            "consequence_refs": refs,
        }
        if (
            sha256_bytes(canonical_json(payload).encode("utf-8"))
            != row["canonical_sha256"]
        ):
            raise TruthImportError(
                "coordination status event canonical hash does not match"
            )
        _portable_record(row, "coordination status event")
        return

    if record_type == "cowork_review_application":
        proposal_ids = _json_value(
            row["applied_proposal_ids_json"],
            "review application.applied_proposal_ids_json",
        )
        if not isinstance(proposal_ids, list) or not proposal_ids:
            raise TruthImportError(
                "review application requires applied proposal ids"
            )
        normalized = [
            _record_id(value, "review application proposal id")
            for value in proposal_ids
        ]
        if len(normalized) != len(set(normalized)):
            raise TruthImportError(
                "review application proposal ids contain duplicates"
            )
        _timestamp(
            row["committed_at"],
            "review application.committed_at",
        )
        payload = {
            "document_id": row["document_id"],
            "applied_proposal_ids": proposal_ids,
            "committed_at": row["committed_at"],
        }
        if (
            sha256_bytes(canonical_json(payload).encode("utf-8"))
            != row["canonical_sha256"]
        ):
            raise TruthImportError(
                "review application canonical hash does not match"
            )
        _digest(
            row["canonical_sha256"],
            "review application.canonical_sha256",
        )
        _actor_record(row, "review application")
        return


def _validate_foreign_refs(records: tuple[_DataRecord, ...]) -> None:
    index = {(item.record_type, item.record_key): item.seq for item in records}
    cothink_status_by_item: dict[str, str] = {}
    coordination_status_by_job: dict[str, str] = {}

    def require_prior(
        record_type: str,
        key: Any,
        before: int,
        label: str,
    ) -> None:
        if record_type in _ID_KEY_TYPES or record_type == "link_retraction":
            normalized_key = _record_id(key, label)
        else:
            normalized_key = _nonempty_text(key, label)
        seq = index.get((record_type, normalized_key))
        if seq is None:
            raise TruthImportError(f"{label} references a missing {record_type}")
        if seq >= before:
            raise TruthImportError(f"{label} must reference an earlier ledger record")

    for item in records:
        row = item.record
        if item.record_type == "evidence_span":
            require_prior("evidence", row["evidence_id"], item.seq, "evidence_id")
        elif item.record_type == "derivation":
            require_prior("claim", row["claim_id"], item.seq, "derivation.claim_id")
        elif item.record_type == "derivation_premise":
            require_prior(
                "derivation",
                row["derivation_id"],
                item.seq,
                "derivation_premise.derivation_id",
            )
            if row["premise_kind"] == "local":
                require_prior(
                    "claim",
                    row["premise_ref"],
                    item.seq,
                    "derivation_premise.premise_ref",
                )
        elif item.record_type == "claim_link":
            require_prior(
                "claim", row["from_claim_id"], item.seq, "claim_link.from_claim_id"
            )
            if row["to_kind"] == "claim":
                require_prior("claim", row["to_ref"], item.seq, "claim_link.to_ref")
            elif row["to_kind"] == "evidence_span":
                require_prior(
                    "evidence_span", row["to_ref"], item.seq, "claim_link.to_ref"
                )
        elif item.record_type == "link_retraction":
            require_prior(
                "claim_link", row["link_id"], item.seq, "link_retraction.link_id"
            )
        elif item.record_type == "claim_status_event":
            require_prior("claim", row["claim_id"], item.seq, "status.claim_id")
            if row["basis_kind"] == "gesture" and row["basis_ref"] is not None:
                require_prior("gesture", row["basis_ref"], item.seq, "status.basis_ref")
        elif item.record_type == "redaction_event":
            subject_type = {
                "claim": "claim",
                "evidence": "evidence",
                "span": "evidence_span",
                "proposal": "proposal",
            }[row["subject_kind"]]
            require_prior(
                subject_type, row["subject_ref"], item.seq, "redaction subject"
            )
            if row["basis_kind"] == "gesture":
                require_prior("gesture", row["basis_ref"], item.seq, "redaction basis")
        elif item.record_type == "sweep_finding":
            require_prior("sweep", row["sweep_id"], item.seq, "sweep_finding.sweep_id")
        elif item.record_type == "document_span":
            require_prior(
                "document", row["document_id"], item.seq, "document_span.document_id"
            )
        elif item.record_type == "document_version":
            require_prior(
                "document", row["document_id"], item.seq, "document_version.document_id"
            )
        elif item.record_type == "expression":
            require_prior(
                "document_span",
                row["document_span_id"],
                item.seq,
                "expression.document_span_id",
            )
        elif item.record_type == "proposal":
            require_prior(
                "document", row["document_id"], item.seq, "proposal.document_id"
            )
        elif item.record_type == "proposal_status_event":
            require_prior(
                "proposal",
                row["proposal_id"],
                item.seq,
                "proposal_status_event.proposal_id",
            )
        elif item.record_type == "doc_event":
            require_prior(
                "document", row["document_id"], item.seq, "doc_event.document_id"
            )
        elif item.record_type == "criterion_check_binding":
            require_prior(
                "criterion_definition_version",
                row["criterion_definition_version_id"],
                item.seq,
                "criterion_check_binding.criterion_definition_version_id",
            )
            require_prior(
                "check_definition_version",
                row["check_definition_version_id"],
                item.seq,
                "criterion_check_binding.check_definition_version_id",
            )
        elif item.record_type == "criterion_activation":
            require_prior(
                "criterion_definition_version",
                row["criterion_definition_version_id"],
                item.seq,
                "criterion_activation.criterion_definition_version_id",
            )
            require_prior(
                "criterion_check_binding",
                row["criterion_check_binding_id"],
                item.seq,
                "criterion_activation.criterion_check_binding_id",
            )
        elif item.record_type == "action_snapshot":
            require_prior(
                "document",
                row["document_id"],
                item.seq,
                "action_snapshot.document_id",
            )
            if row["document_version_id"] is not None:
                require_prior(
                    "document_version",
                    row["document_version_id"],
                    item.seq,
                    "action_snapshot.document_version_id",
                )
        elif item.record_type == "evaluation_plan_snapshot":
            require_prior(
                "action_snapshot",
                row["action_snapshot_id"],
                item.seq,
                "evaluation_plan_snapshot.action_snapshot_id",
            )
        elif item.record_type == "evaluation_run":
            require_prior(
                "action_snapshot",
                row["action_snapshot_id"],
                item.seq,
                "evaluation_run.action_snapshot_id",
            )
            require_prior(
                "evaluation_plan_snapshot",
                row["plan_snapshot_id"],
                item.seq,
                "evaluation_run.plan_snapshot_id",
            )
        elif item.record_type == "check_execution":
            require_prior(
                "evaluation_run",
                row["evaluation_run_id"],
                item.seq,
                "check_execution.evaluation_run_id",
            )
            require_prior(
                "check_definition_version",
                row["check_definition_version_id"],
                item.seq,
                "check_execution.check_definition_version_id",
            )
            require_prior(
                "criterion_check_binding",
                row["criterion_check_binding_id"],
                item.seq,
                "check_execution.criterion_check_binding_id",
            )
        elif item.record_type == "evaluation_result":
            require_prior(
                "evaluation_run",
                row["evaluation_run_id"],
                item.seq,
                "evaluation_result.evaluation_run_id",
            )
            require_prior(
                "check_execution",
                row["check_execution_id"],
                item.seq,
                "evaluation_result.check_execution_id",
            )
            require_prior(
                "criterion_definition_version",
                row["criterion_definition_version_id"],
                item.seq,
                "evaluation_result.criterion_definition_version_id",
            )
        elif item.record_type == "routing_disposition":
            require_prior(
                "evaluation_result",
                row["evaluation_result_id"],
                item.seq,
                "routing_disposition.evaluation_result_id",
            )
        elif item.record_type == "result_relation":
            require_prior(
                "evaluation_result",
                row["evaluation_result_id"],
                item.seq,
                "result_relation.evaluation_result_id",
            )
            target_type = {
                "evaluation_result": "evaluation_result",
                "evaluation_run": "evaluation_run",
                "proposal": "proposal",
                "cothink_item": "cothink_item",
            }.get(row["target_kind"])
            if target_type is not None:
                require_prior(
                    target_type,
                    row["target_ref"],
                    item.seq,
                    "result_relation.target_ref",
                )
        elif item.record_type == "model_call_authorization_receipt":
            require_prior(
                "action_snapshot",
                row["action_snapshot_id"],
                item.seq,
                "model authorization.action_snapshot_id",
            )
            if row["plan_snapshot_id"] is not None:
                require_prior(
                    "evaluation_plan_snapshot",
                    row["plan_snapshot_id"],
                    item.seq,
                    "model authorization.plan_snapshot_id",
                )
        elif item.record_type == "cothink_item":
            require_prior(
                "action_snapshot",
                row["action_snapshot_id"],
                item.seq,
                "cothink_item.action_snapshot_id",
            )
        elif item.record_type == "cothink_item_status_event":
            item_id = row["cothink_item_id"]
            require_prior(
                "cothink_item",
                item_id,
                item.seq,
                "cothink_item_status_event.cothink_item_id",
            )
            previous = cothink_status_by_item.get(item_id)
            if previous is None:
                if row["status"] != "open":
                    raise TruthImportError(
                        "first Co-think item status must be open"
                    )
            elif row["status"] not in _COTHINK_ITEM_TRANSITIONS.get(
                previous,
                frozenset(),
            ):
                raise TruthImportError(
                    f"invalid Co-think item status transition: "
                    f"{previous} -> {row['status']}"
                )
            cothink_status_by_item[item_id] = row["status"]
        elif item.record_type == "cowork_coordination_job":
            require_prior(
                "document",
                row["document_id"],
                item.seq,
                "coordination job.document_id",
            )
            require_prior(
                "action_snapshot",
                row["action_snapshot_id"],
                item.seq,
                "coordination job.action_snapshot_id",
            )
            request_summary = json.loads(row["request_summary_json"])
            confirmation = request_summary.get(
                "recheck_target_confirmation"
            )
            if isinstance(confirmation, dict):
                require_prior(
                    "action_snapshot",
                    confirmation["affirmed_action_snapshot_id"],
                    item.seq,
                    "coordination job recheck target affirmation",
                )
            if row["evaluation_run_id"] is not None:
                require_prior(
                    "evaluation_run",
                    row["evaluation_run_id"],
                    item.seq,
                    "coordination job.evaluation_run_id",
                )
            if row["plan_snapshot_id"] is not None:
                require_prior(
                    "evaluation_plan_snapshot",
                    row["plan_snapshot_id"],
                    item.seq,
                    "coordination job.plan_snapshot_id",
                )
            if row["parent_job_id"] is not None:
                require_prior(
                    "cowork_coordination_job",
                    row["parent_job_id"],
                    item.seq,
                    "coordination job.parent_job_id",
                )
            require_prior(
                "model_call_authorization_receipt",
                row["authorization_receipt_id"],
                item.seq,
                "coordination job.authorization_receipt_id",
            )
            if row["role"] == "cothink":
                if (
                    row["evaluation_run_id"] is not None
                    or row["plan_snapshot_id"] is not None
                ):
                    raise TruthImportError(
                        "Co-think coordination cannot bind an evaluation run or plan"
                    )
            elif (
                row["evaluation_run_id"] is None
                or row["plan_snapshot_id"] is None
            ):
                raise TruthImportError(
                    "Verify coordination requires an evaluation run and plan"
                )
        elif item.record_type == "cowork_coordination_status_event":
            job_id = row["coordination_job_id"]
            require_prior(
                "cowork_coordination_job",
                job_id,
                item.seq,
                "coordination status event.coordination_job_id",
            )
            previous = coordination_status_by_job.get(job_id)
            if previous is None:
                if row["status"] != "prepared":
                    raise TruthImportError(
                        "first coordination status must be prepared"
                    )
            elif row["status"] not in _COORDINATION_TRANSITIONS[previous]:
                raise TruthImportError(
                    "invalid coordination status transition: "
                    f"{previous} -> {row['status']}"
                )
            refs = json.loads(row["consequence_refs_json"])
            if "next_job_id" in refs:
                require_prior(
                    "cowork_coordination_job",
                    refs["next_job_id"],
                    item.seq,
                    "coordination status event.next_job_id",
                )
            if "cothink_item_id" in refs:
                require_prior(
                    "cothink_item",
                    refs["cothink_item_id"],
                    item.seq,
                    "coordination status event.cothink_item_id",
                )
            for proposal_id in refs.get("proposal_ids", []):
                require_prior(
                    "proposal",
                    proposal_id,
                    item.seq,
                    "coordination status event.proposal_ids",
                )
            for disposition_id in refs.get("disposition_ids", []):
                require_prior(
                    "routing_disposition",
                    disposition_id,
                    item.seq,
                    "coordination status event.disposition_ids",
                )
            for result_id in refs.get(
                "requested_revision_result_ids",
                [],
            ):
                require_prior(
                    "evaluation_result",
                    result_id,
                    item.seq,
                    "coordination status event.requested_revision_result_ids",
                )
            coordination_status_by_job[job_id] = row["status"]
        elif item.record_type == "cowork_review_application":
            require_prior(
                "document",
                row["document_id"],
                item.seq,
                "review application.document_id",
            )
            for proposal_id in json.loads(
                row["applied_proposal_ids_json"]
            ):
                require_prior(
                    "proposal",
                    proposal_id,
                    item.seq,
                    "review application.applied_proposal_ids",
                )

    item_ids = {
        item.record_key
        for item in records
        if item.record_type == "cothink_item"
    }
    missing_status = item_ids - set(cothink_status_by_item)
    if missing_status:
        raise TruthImportError(
            "Co-think item is missing its initial open status event"
        )


def _validate_bundle(bundle: _Bundle) -> StoreProfile:
    profile = _validate_header(bundle)
    previous_seq = 0
    seen_pairs: set[tuple[str, str]] = set()
    status_seqs: set[int] = set()
    for item in bundle.records:
        if item.record_type not in _RECORD_COLUMNS:
            raise TruthImportError(
                f"unsupported ledger record type {item.record_type!r}"
            )
        seq = _positive_int(item.seq, "ledger seq")
        if seq <= previous_seq:
            raise TruthImportError("ledger records must be strictly ordered by seq")
        previous_seq = seq
        pair = (item.record_type, item.record_key)
        if pair in seen_pairs:
            raise TruthImportError("duplicate ledger record key")
        seen_pairs.add(pair)
        _validate_record_values(item)
        if item.record_type == "claim_status_event":
            status_seq = int(item.record["seq"])
            if status_seq in status_seqs:
                raise TruthImportError("duplicate claim status seq")
            status_seqs.add(status_seq)

    blob_map: dict[str, bytes] = {}
    previous_digest = ""
    for blob in bundle.blobs:
        digest = _digest(blob.content_sha256, "blob content_sha256")
        if digest <= previous_digest:
            raise TruthImportError("blob records must be unique and sorted by digest")
        previous_digest = digest
        if sha256_bytes(blob.content) != digest:
            raise TruthImportError("blob bytes do not match content_sha256")
        blob_map[digest] = blob.content

    referenced_blobs: set[str] = set()
    for item in bundle.records:
        row = item.record
        if item.record_type == "evidence":
            if row["redacted_at"] is None and row["content_path"] is not None:
                referenced_blobs.add(row["content_sha256"])
        elif item.record_type == "document":
            if row["ydoc_snapshot_sha256"] is not None:
                referenced_blobs.add(row["ydoc_snapshot_sha256"])
        elif item.record_type == "document_version":
            referenced_blobs.add(row["projection_sha256"])
            referenced_blobs.add(row["ydoc_snapshot_sha256"])
        elif item.record_type == "action_snapshot":
            referenced_blobs.add(row["projection_blob_sha256"])
            referenced_blobs.add(row["target_blob_sha256"])
    if referenced_blobs != set(blob_map):
        missing = sorted(referenced_blobs - set(blob_map))
        extra = sorted(set(blob_map) - referenced_blobs)
        if missing:
            raise TruthImportError(f"export is missing live blobs: {missing}")
        raise TruthImportError(f"export contains unreferenced blobs: {extra}")

    _validate_foreign_refs(bundle.records)
    return profile


def _fetch_row(
    conn: sqlite3.Connection,
    record_type: str,
    record_key: str,
) -> dict[str, Any]:
    table, columns = _RECORD_COLUMNS[record_type]
    selected = ", ".join(columns)
    if record_type in _ID_KEY_TYPES:
        key_column = "id"
        params = (record_key,)
    elif record_type == "link_retraction":
        key_column = "link_id"
        params = (record_key,)
    else:
        try:
            key = json.loads(record_key)
        except json.JSONDecodeError as exc:
            raise TruthExportError(
                "derivation premise ledger key is malformed"
            ) from exc
        if not isinstance(key, dict) or set(key) != {"derivation_id", "premise_ref"}:
            raise TruthExportError("derivation premise ledger key is malformed")
        key_column = "derivation_id = ? AND premise_ref"
        params = (key["derivation_id"], key["premise_ref"])
    sql = f"SELECT {selected} FROM {table} WHERE {key_column} = ?"
    rows = conn.execute(sql, params).fetchall()
    if len(rows) != 1:
        raise TruthExportError(
            f"ledger record {record_type}:{record_key} has no unique source row"
        )
    row = dict(rows[0])
    try:
        computed = _record_key(record_type, row)
    except TruthImportError as exc:
        raise TruthExportError(str(exc)) from exc
    if computed != record_key:
        raise TruthExportError("ledger record key does not match its source row")
    return row


def _assert_all_rows_are_ordered(
    conn: sqlite3.Connection,
    ordered_keys: set[tuple[str, str]],
) -> None:
    existing_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for record_type, (table, columns) in _RECORD_COLUMNS.items():
        if table not in existing_tables:
            # A store older than the schema that introduced this table simply
            # has no such rows to order. Real exports run on current-schema
            # stores where every base table is present, so this only relaxes
            # the check for a store still carrying an earlier schema.
            continue
        selected = ", ".join(columns)
        for raw in conn.execute(f"SELECT {selected} FROM {table}"):
            row = dict(raw)
            try:
                key = _record_key(record_type, row)
            except TruthImportError as exc:
                raise TruthExportError(str(exc)) from exc
            if (record_type, key) not in ordered_keys:
                raise TruthExportError(
                    f"{table} contains a row missing from ledger_records"
                )


def _collect_export_bundle(
    store: TruthStore,
    *,
    conn: sqlite3.Connection | None = None,
) -> _Bundle:
    profile = store.profile.to_dict()
    export_conn = store.connect() if conn is None else conn
    owns_transaction = conn is None
    if conn is not None:
        store._validate_connection_target(conn)
        store._require_transaction(conn)
    try:
        if owns_transaction:
            export_conn.execute("BEGIN IMMEDIATE")
        from work_buddy.truth import ydoc_store

        for row in export_conn.execute(
            "SELECT id FROM documents WHERE ydoc_snapshot_sha256 IS NOT NULL"
        ):
            if ydoc_store.update_tail_present(store, document_id=str(row["id"])):
                raise UncompactedDocumentError(str(row["id"]))
        info_rows = export_conn.execute("SELECT * FROM store_info").fetchall()
        if len(info_rows) != 1:
            raise TruthExportError("store_info must contain exactly one row")
        store_info = dict(info_rows[0])
        records: list[_DataRecord] = []
        ordered_keys: set[tuple[str, str]] = set()
        previous_seq = 0
        for ledger in export_conn.execute(
            "SELECT seq, record_type, record_key FROM ledger_records ORDER BY seq"
        ):
            seq = int(ledger["seq"])
            record_type = str(ledger["record_type"])
            record_key = str(ledger["record_key"])
            if seq <= previous_seq:
                raise TruthExportError("ledger_records is not strictly ordered")
            previous_seq = seq
            if record_type not in _RECORD_COLUMNS:
                raise TruthExportError(
                    f"ledger contains unsupported record type {record_type!r}"
                )
            pair = (record_type, record_key)
            if pair in ordered_keys:
                raise TruthExportError("ledger contains a duplicate record key")
            ordered_keys.add(pair)
            records.append(
                _DataRecord(
                    seq=seq,
                    record_type=record_type,
                    record_key=record_key,
                    record=_fetch_row(export_conn, record_type, record_key),
                )
            )
        _assert_all_rows_are_ordered(export_conn, ordered_keys)

        blobs: dict[str, bytes] = {}
        for item in records:
            if item.record_type == "evidence":
                row = item.record
                if row["redacted_at"] is not None or row["content_path"] is None:
                    continue
                digest = str(row["content_sha256"])
                path = store.resolve_blob_path(str(row["content_path"]))
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise TruthExportError(
                        f"live evidence blob is unavailable: {path}"
                    ) from exc
                if sha256_bytes(content) != digest:
                    raise TruthExportError(
                        "live evidence blob does not match content_sha256"
                    )
                blobs[digest] = content
            elif item.record_type == "document":
                snapshot_digest = item.record["ydoc_snapshot_sha256"]
                if snapshot_digest is None:
                    continue
                digest = str(snapshot_digest)
                # Content-addressed Y.Doc snapshots live in blobs/ and export
                # exactly like evidence blobs, deduped by content address. The
                # runtime/ update log is never serialized (PRD section 5).
                path = store.resolve_blob_path("blobs/" + digest)
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise TruthExportError(
                        f"live ydoc snapshot blob is unavailable: {path}"
                    ) from exc
                if sha256_bytes(content) != digest:
                    raise TruthExportError(
                        "live ydoc snapshot blob does not match ydoc_snapshot_sha256"
                    )
                blobs[digest] = content
            elif item.record_type == "document_version":
                for field in ("projection_sha256", "ydoc_snapshot_sha256"):
                    digest = str(item.record[field])
                    path = store.resolve_blob_path("blobs/" + digest)
                    try:
                        content = path.read_bytes()
                    except OSError as exc:
                        raise TruthExportError(
                            f"document version blob is unavailable: {path}"
                        ) from exc
                    if sha256_bytes(content) != digest:
                        raise TruthExportError(
                            f"document version blob does not match {field}"
                        )
                    blobs[digest] = content
            elif item.record_type == "action_snapshot":
                for field in ("projection_blob_sha256", "target_blob_sha256"):
                    digest = str(item.record[field])
                    path = store.resolve_blob_path("blobs/" + digest)
                    try:
                        content = path.read_bytes()
                    except OSError as exc:
                        raise TruthExportError(
                            f"action snapshot blob is unavailable: {path}"
                        ) from exc
                    if sha256_bytes(content) != digest:
                        raise TruthExportError(
                            f"action snapshot blob does not match {field}"
                        )
                    blobs[digest] = content
        if owns_transaction:
            export_conn.execute("COMMIT")
    except Exception:
        if owns_transaction and export_conn.in_transaction:
            export_conn.execute("ROLLBACK")
        raise
    finally:
        if owns_transaction:
            export_conn.close()

    bundle = _Bundle(
        source_format_version=FORMAT_VERSION,
        store_info=store_info,
        profile=profile,
        records=tuple(records),
        blobs=tuple(
            _BlobRecord(content_sha256=digest, content=content)
            for digest, content in sorted(blobs.items())
        ),
    )
    try:
        _validate_bundle(bundle)
    except TruthImportError as exc:
        raise TruthExportError(str(exc)) from exc
    return bundle


def _serialize_bundle(bundle: _Bundle) -> bytes:
    header = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "profile": dict(bundle.profile),
        "record_type": "header",
        "store_info": dict(bundle.store_info),
    }
    items: list[Mapping[str, Any]] = [header]
    items.extend(
        {
            "record": dict(item.record),
            "record_key": item.record_key,
            "record_type": item.record_type,
            "seq": item.seq,
        }
        for item in bundle.records
    )
    items.extend(
        {
            "content_base64": base64.b64encode(blob.content).decode("ascii"),
            "content_sha256": blob.content_sha256,
            "record_type": "blob",
        }
        for blob in bundle.blobs
    )
    prefix = b"".join(_canonical_line(item) for item in items)
    footer = {
        "blob_count": len(bundle.blobs),
        "last_seq": bundle.records[-1].seq if bundle.records else 0,
        "record_count": len(bundle.records),
        "record_type": "end",
        "stream_sha256": sha256_bytes(prefix),
    }
    return prefix + _canonical_line(footer)


def export_store(
    store: TruthStore,
    destination: str | Path | None = None,
) -> ExportResult:
    """Write a deterministic, atomic recovery export for ``store``."""
    if not isinstance(store, TruthStore):
        raise TypeError("store must be a TruthStore")
    path = (
        store.paths.claims_export
        if destination is None
        else Path(destination).expanduser().resolve()
    )
    # Keep the store's cross-process SQLite writer lock until the atomic file
    # publication completes. Without this, an older post-commit hook can
    # collect seq N, pause, and overwrite a newer seq N+K export after the
    # newer writer has published it.
    # The migration-store lock also serializes filesystem-only Y.Doc appends.
    # Without it an export could observe no tail, pause, and publish after an
    # append invalidated the artifact. Holding it through atomic publication
    # makes the final ordering safe: export then append+unlink, or append then
    # explicit export rejects the uncompacted tail.
    with store.migration_write_lock():
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bundle = _collect_export_bundle(store, conn=conn)
            payload = _serialize_bundle(bundle)
            atomic_write_bytes(path, payload)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return ExportResult(
        path=path,
        sha256=sha256_bytes(payload),
        record_count=len(bundle.records),
        blob_count=len(bundle.blobs),
        last_seq=bundle.records[-1].seq if bundle.records else 0,
    )


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value {value}")


def _read_objects(
    source: str | Path | bytes | bytearray | memoryview,
) -> list[dict[str, Any]]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
    else:
        try:
            payload = Path(source).expanduser().resolve().read_bytes()
        except OSError as exc:
            raise TruthImportError(f"cannot read truth export: {source}") from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TruthImportError("truth export must be UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise TruthImportError("truth export is empty")
    if any(not line.strip() for line in lines):
        raise TruthImportError("truth export contains a blank record")
    objects: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TruthImportError(f"malformed JSON record on line {number}") from exc
        if not isinstance(value, dict):
            raise TruthImportError(f"line {number} must contain a JSON object")
        objects.append(value)
    end_positions = [
        index
        for index, value in enumerate(objects)
        if value.get("record_type") == "end"
    ]
    if not end_positions:
        raise TruthImportError("truth export is missing its end record")
    if len(end_positions) != 1 or end_positions[0] != len(objects) - 1:
        raise TruthImportError("truth export contains duplicate or trailing records")
    return objects


def _parse_header(objects: list[dict[str, Any]]) -> int:
    header = objects[0]
    _require_exact_keys(
        header,
        {"format", "format_version", "profile", "record_type", "store_info"},
        "format header",
    )
    if header["record_type"] != "header" or header["format"] != FORMAT_NAME:
        raise TruthImportError("truth export has an invalid format header")
    version = _positive_int(header["format_version"], "format_version")
    if version > FORMAT_VERSION:
        raise TruthImportError(
            f"truth export format v{version} is newer than supported v{FORMAT_VERSION}"
        )
    if version < OLDEST_FORMAT_VERSION:
        raise TruthImportError(f"truth export format v{version} is unsupported")
    return version


def _upcast_records(
    records: list[_DataRecord],
    source_version: int,
) -> list[_DataRecord]:
    if source_version >= 4:
        upgraded = list(records)
    else:
        upgraded = []
        for item in records:
            row = dict(item.record)
            if item.record_type == "proposal":
                row.setdefault("base_structured_head_sha256", None)
            upgraded.append(
                _DataRecord(
                    seq=item.seq,
                    record_type=item.record_type,
                    record_key=item.record_key,
                    record=row,
                )
            )
    if source_version >= 6:
        return upgraded

    status_item_ids = {
        item.record["cothink_item_id"]
        for item in upgraded
        if item.record_type == "cothink_item_status_event"
    }
    next_seq = max((item.seq for item in upgraded), default=0)
    for item in tuple(upgraded):
        if (
            item.record_type != "cothink_item"
            or item.record_key in status_item_ids
        ):
            continue
        next_seq += 1
        item_id = item.record_key
        event_id = sha256_bytes(
            _COTHINK_STATUS_DOMAIN + item_id.encode("utf-8")
        )[:32]
        canonical_payload = {
            "cothink_item_id": item_id,
            "status": "open",
            "reason": None,
        }
        upgraded.append(
            _DataRecord(
                seq=next_seq,
                record_type="cothink_item_status_event",
                record_key=event_id,
                record={
                    "id": event_id,
                    "cothink_item_id": item_id,
                    "status": "open",
                    "reason": None,
                    "canonical_sha256": sha256_bytes(
                        canonical_json(canonical_payload).encode("utf-8")
                    ),
                    "created_at": item.record["created_at"],
                    "created_by_kind": "system",
                    "created_by_ref": "truth-schema-v6",
                    "created_by_meta_json": canonical_json(
                        {"basis": "pre_lifecycle_item_existence"}
                    ),
                },
            )
        )
    return upgraded


def _parse_v1(objects: list[dict[str, Any]]) -> _Bundle:
    header = objects[0]
    footer = objects[-1]
    _require_exact_keys(footer, {"record_count", "record_type"}, "v1 end record")
    records: list[_DataRecord] = []
    for number, value in enumerate(objects[1:-1], start=2):
        record_type = value.get("record_type")
        if record_type not in _RECORD_COLUMNS:
            raise TruthImportError(f"v1 line {number} has an unknown record type")
        _require_exact_keys(
            value, {"record", "record_type", "seq"}, f"v1 line {number}"
        )
        row = _require_mapping(value["record"], f"v1 line {number} record")
        records.append(
            _DataRecord(
                seq=_positive_int(value["seq"], f"v1 line {number} seq"),
                record_type=record_type,
                record_key=_record_key(record_type, row),
                record=row,
            )
        )
    expected_count = _positive_int(
        footer["record_count"], "v1 record_count", allow_zero=True
    )
    if expected_count != len(records):
        raise TruthImportError("v1 end record count does not match the stream")
    bundle = _Bundle(
        source_format_version=1,
        store_info=_require_mapping(header["store_info"], "store_info"),
        profile=_require_mapping(header["profile"], "profile"),
        records=tuple(_upcast_records(records, 1)),
        blobs=(),
    )
    _validate_bundle(bundle)
    return bundle


def _parse_v2_plus(objects: list[dict[str, Any]], version: int) -> _Bundle:
    # The v2+ framing is stable: one header, ledger records ordered by seq,
    # blobs sorted by digest, and a hashed end footer. Record registries may
    # grow without changing that framing, and source_format_version preserves
    # which portable contract was upcast.
    header = objects[0]
    footer = objects[-1]
    _require_exact_keys(
        footer,
        {
            "blob_count",
            "last_seq",
            "record_count",
            "record_type",
            "stream_sha256",
        },
        "end record",
    )
    expected_stream_hash = _digest(footer["stream_sha256"], "stream_sha256")
    canonical_prefix = b"".join(_canonical_line(item) for item in objects[:-1])
    if sha256_bytes(canonical_prefix) != expected_stream_hash:
        raise TruthImportError("truth export stream hash does not match")

    records: list[_DataRecord] = []
    blobs: list[_BlobRecord] = []
    in_blob_section = False
    for number, value in enumerate(objects[1:-1], start=2):
        record_type = value.get("record_type")
        if record_type == "blob":
            in_blob_section = True
            _require_exact_keys(
                value,
                {"content_base64", "content_sha256", "record_type"},
                f"blob line {number}",
            )
            digest = _digest(value["content_sha256"], "blob content_sha256")
            encoded = value["content_base64"]
            if not isinstance(encoded, str):
                raise TruthImportError("blob content_base64 must be text")
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise TruthImportError("blob content_base64 is malformed") from exc
            blobs.append(_BlobRecord(digest, content))
            continue
        if record_type not in _RECORD_COLUMNS:
            raise TruthImportError(f"line {number} has an unknown record type")
        if in_blob_section:
            raise TruthImportError("ledger data records cannot follow blob records")
        _require_exact_keys(
            value,
            {"record", "record_key", "record_type", "seq"},
            f"data line {number}",
        )
        records.append(
            _DataRecord(
                seq=_positive_int(value["seq"], f"line {number} seq"),
                record_type=record_type,
                record_key=_nonempty_text(
                    value["record_key"], f"line {number} record_key"
                ),
                record=_require_mapping(value["record"], f"line {number} record"),
            )
        )

    record_count = _positive_int(
        footer["record_count"], "record_count", allow_zero=True
    )
    blob_count = _positive_int(footer["blob_count"], "blob_count", allow_zero=True)
    last_seq = _positive_int(footer["last_seq"], "last_seq", allow_zero=True)
    if record_count != len(records) or blob_count != len(blobs):
        raise TruthImportError("end record counts do not match the stream")
    observed_last = records[-1].seq if records else 0
    if last_seq != observed_last:
        raise TruthImportError("end record last_seq does not match the stream")
    bundle = _Bundle(
        source_format_version=version,
        store_info=_require_mapping(header["store_info"], "store_info"),
        profile=_require_mapping(header["profile"], "profile"),
        records=tuple(_upcast_records(records, version)),
        blobs=tuple(blobs),
    )
    _validate_bundle(bundle)
    return bundle


def _parse_bundle(source: str | Path | bytes | bytearray | memoryview) -> _Bundle:
    objects = _read_objects(source)
    version = _parse_header(objects)
    bundle = (
        _parse_v1(objects)
        if version == 1
        else _parse_v2_plus(objects, version)
    )
    source_schema = int(bundle.store_info["schema_version"])
    if source_schema == SCHEMA_VERSION:
        return bundle

    # The JSONL format, not SQLite's internal schema version, governs the
    # portable record contract. Older streams have already been transport-
    # upcast and validated against the current record shapes above, so rebuild
    # them directly into the current schema and publish a current header.
    store_info = dict(bundle.store_info)
    store_info["schema_version"] = SCHEMA_VERSION
    return _Bundle(
        source_format_version=bundle.source_format_version,
        store_info=store_info,
        profile=bundle.profile,
        records=bundle.records,
        blobs=bundle.blobs,
    )


def _preflight_target(
    paths: StorePaths,
    store_id: str,
    registry: StoreRegistry,
) -> bool:
    if not paths.root.is_dir():
        raise TruthImportError("import target scope root must already exist")
    from work_buddy.cowork.project_store import (
        FolderLifecycleError,
        _assert_managed_layout_safe,
    )

    try:
        _assert_managed_layout_safe(paths.root)
    except FolderLifecycleError as exc:
        raise TruthImportError(
            "the import Folder contains redirected or unsupported Work Buddy data"
        ) from exc
    if paths.sidecar.parent.exists() and not paths.sidecar.parent.is_dir():
        raise TruthImportError(".wbuddy must be a directory")
    if paths.sidecar.parent.exists():
        from work_buddy.cowork.project_store import (
            FolderLifecycleError,
            read_manifest,
        )

        try:
            read_manifest(paths.root)
        except FolderLifecycleError as exc:
            raise TruthImportError(
                "the import Folder has an invalid .wbuddy/manifest.yaml"
            ) from exc
    existed_empty = False
    if paths.sidecar.exists():
        if not paths.sidecar.is_dir():
            raise TruthImportError("import target sidecar path is not a directory")
        if any(paths.sidecar.iterdir()):
            raise TruthImportError("truth import target must be empty")
        existed_empty = True
    try:
        registered_paths = registry.paths_for_store_id(store_id)
    except AttributeError as exc:
        raise TruthImportError(
            "registry does not implement paths_for_store_id"
        ) from exc
    target = paths.sidecar.resolve()
    for registered in registered_paths:
        existing = StorePaths.from_root(registered).sidecar.resolve()
        if existing != target:
            raise StoreIdentityCollision(
                f"store_id {store_id} is already registered at {existing}"
            )
    return existed_empty


def _insert_records(store: TruthStore, bundle: _Bundle) -> None:
    conn = store.connect()
    try:
        migrate(conn, store.paths.db)
        conn.execute("BEGIN IMMEDIATE")
        info = bundle.store_info
        conn.execute(
            "INSERT INTO store_info "
            "(store_id, profile, schema_version, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                info["store_id"],
                info["profile"],
                SCHEMA_VERSION,
                info["title"],
                info["created_at"],
            ),
        )
        for item in bundle.records:
            table, columns = _RECORD_COLUMNS[item.record_type]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(item.record[column] for column in columns),
            )
            store._insert_ledger_record_locked(
                conn,
                item.record_type,
                item.record_key,
                seq=item.seq,
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _validate_staged_integrity(store: TruthStore) -> None:
    """Reject imported rows that violate canonical kernel integrity.

    Transport validation proves that rows are well shaped and refer to earlier
    ledger records. It is not enough for kernel authority: an untrusted stream
    could otherwise label a status ``confirmed`` while claiming a rule basis,
    bind it to an ineligible gesture, break weakest-link derivation, or publish
    competing confirmed successors. The integrity engine is the canonical
    replay validator for those cross-record and temporal rules. Warnings remain
    portable because they include intentionally unresolved external state.
    Errors may not be published as a live store.
    """

    from work_buddy.truth.queries import integrity_findings

    try:
        findings = integrity_findings(store)
    except Exception as exc:
        raise TruthImportError("staged truth store could not be validated") from exc
    blockers = sorted(
        (finding for finding in findings if finding.severity == "error"),
        key=lambda finding: (
            finding.code,
            finding.subject_kind,
            finding.subject_ref,
            finding.detail,
        ),
    )
    if blockers:
        details = "; ".join(
            f"{finding.code}:{finding.subject_ref}" for finding in blockers
        )
        raise TruthImportError(
            "imported store violates truth invariants: " + details
        )


def _build_staged_store(
    container: Path,
    bundle: _Bundle,
    profile: StoreProfile,
) -> TruthStore:
    paths = StorePaths.from_root(container)
    paths.sidecar.mkdir(parents=True)
    paths.blobs.mkdir()
    paths.export_dir.mkdir()
    dump_profile(profile, paths.config)
    staged = TruthStore(paths)
    for blob in bundle.blobs:
        atomic_write_bytes(paths.blobs / blob.content_sha256, blob.content)
    _insert_records(staged, bundle)
    _validate_staged_integrity(staged)
    expected = _serialize_bundle(bundle)
    result = export_store(staged)
    if result.path.read_bytes() != expected:
        raise TruthImportError("staged store does not reproduce the validated export")
    TruthStore.open(paths.sidecar)
    from work_buddy.cowork.project_store import (
        patch_cowork_manifest,
        write_component_gitignore,
    )

    write_component_gitignore(paths.sidecar)
    patch_cowork_manifest(paths.root, expected_sha256=None)
    return staged


def _remove_staging(container: Path, allowed_parent: Path) -> None:
    if not container.exists():
        return
    resolved = container.resolve()
    parent = allowed_parent.resolve()
    if resolved.parent != parent or not resolved.name.startswith(
        _IMPORT_STAGING_PREFIX
    ):
        raise RuntimeError("refusing to remove an unexpected import staging path")
    shutil.rmtree(resolved)


def _canonical_import_target(target: str | Path) -> StorePaths:
    candidate = Path(os.path.abspath(Path(target).expanduser()))
    if candidate.name == "cowork" and candidate.parent.name == ".wbuddy":
        root = candidate.parent.parent
    else:
        root = candidate
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise TruthImportError(
            "import target scope root must already exist"
        ) from exc
    from work_buddy.cowork.project_store import (
        FolderLifecycleError,
        _assert_managed_layout_safe,
    )

    try:
        _assert_managed_layout_safe(root)
    except FolderLifecycleError as exc:
        raise TruthImportError(
            "the import Folder contains redirected or unsupported Work Buddy data"
        ) from exc
    return StorePaths.canonical(root)


def import_store(
    source: str | Path | bytes | bytearray | memoryview,
    target: str | Path,
    *,
    registry: StoreRegistry,
) -> ImportResult:
    """Preflight and atomically rebuild one empty target from JSONL."""
    bundle = _parse_bundle(source)
    profile = _validate_bundle(bundle)
    target_paths = _canonical_import_target(target)
    from work_buddy.truth.locks import folder_operation_locks

    registry_db = getattr(registry, "db_path", None)
    lock_data_root: Path | None = None
    if registry_db is not None:
        registry_parent = Path(registry_db).expanduser().resolve().parent
        lock_data_root = (
            registry_parent.parent
            if registry_parent.name == "db"
            else registry_parent
        )
    with folder_operation_locks(
        target_paths.root,
        data_root=lock_data_root,
    ):
        return _import_store_locked(
            bundle,
            profile,
            target_paths,
            registry=registry,
        )


def _import_store_locked(
    bundle: _Bundle,
    profile: StoreProfile,
    target_paths: StorePaths,
    *,
    registry: StoreRegistry,
) -> ImportResult:
    """Publish an already validated bundle while holding Folder locks."""

    _preflight_target(target_paths, profile.store_id, registry)

    container = Path(
        tempfile.mkdtemp(prefix=_IMPORT_STAGING_PREFIX, dir=target_paths.root)
    )
    removed_empty_target = False
    published_sidecar = False
    published_wbuddy = False
    manifest_snapshot = None
    published_manifest: bytes | None = None
    try:
        staged = _build_staged_store(container, bundle, profile)
        staged_sidecar = staged.paths.sidecar.resolve()
        staged_wbuddy = staged_sidecar.parent
        if staged_wbuddy.parent != container.resolve():
            raise TruthImportError("staged sidecar escaped its import container")
        target_wbuddy = target_paths.sidecar.parent
        from work_buddy.cowork.project_store import (
            FolderLifecycleError,
            _assert_managed_layout_safe,
        )

        try:
            _assert_managed_layout_safe(target_paths.root)
        except FolderLifecycleError as exc:
            raise TruthImportError(
                "the import Folder contains redirected or unsupported Work Buddy data"
            ) from exc
        if not target_wbuddy.exists():
            os.replace(staged_wbuddy, target_wbuddy)
            published_wbuddy = True
        else:
            from work_buddy.cowork.project_store import (
                patch_cowork_manifest,
                read_manifest,
            )

            manifest_snapshot = read_manifest(target_paths.root)
            if target_paths.sidecar.exists():
                if any(target_paths.sidecar.iterdir()):
                    raise TruthImportError("truth import target changed during import")
                target_paths.sidecar.rmdir()
                removed_empty_target = True
            os.replace(staged_sidecar, target_paths.sidecar)
            published_sidecar = True
            _, published_manifest = patch_cowork_manifest(
                target_paths.root,
                expected_sha256=manifest_snapshot.sha256,
            )

        from work_buddy.cowork.project_store import read_manifest

        manifest = read_manifest(target_paths.root)
        if not manifest.has_cowork:
            raise TruthImportError(
                "portable import did not publish a valid Co-work Folder manifest"
            )
        restored = TruthStore.open(target_paths.sidecar)
    except Exception:
        if published_manifest is not None and manifest_snapshot is not None:
            from work_buddy.cowork.project_store import _restore_manifest

            _restore_manifest(manifest_snapshot, published_manifest)
        if published_wbuddy and target_paths.sidecar.parent.exists():
            shutil.rmtree(target_paths.sidecar.parent)
        elif published_sidecar and target_paths.sidecar.exists():
            shutil.rmtree(target_paths.sidecar)
        if removed_empty_target and not target_paths.sidecar.exists():
            target_paths.sidecar.mkdir(parents=True)
        raise
    finally:
        _remove_staging(container, target_paths.root)

    return ImportResult(
        store=restored,
        source_format_version=bundle.source_format_version,
        record_count=len(bundle.records),
        blob_count=len(bundle.blobs),
    )


__all__ = [
    "ExportResult",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "ImportResult",
    "OLDEST_FORMAT_VERSION",
    "StoreIdentityCollision",
    "StoreRegistry",
    "TruthExportError",
    "TruthImportError",
    "UncompactedDocumentError",
    "export_store",
    "import_store",
]
