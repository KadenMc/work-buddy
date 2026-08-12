"""Typed contracts for the Truth-to-Hindsight derived projection.

Nothing in this module makes Hindsight authoritative.  Truth supplies a
content-free desired-state intent and, only when an upsert is current, an exact
authoritative claim snapshot.  The projection worker preserves the Truth
identity and policy decision while treating every Hindsight document as a
replaceable semantic derivative.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol, Sequence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{1,255}$")


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _required_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProjectionValidationError(f"{field_name} is not a safe opaque identifier")
    return value


def _required_ref(value: str, field_name: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProjectionValidationError(f"{field_name} is not a safe reference")
    return value


def _required_token(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ProjectionValidationError(f"{field_name} is not a valid token")
    return value


def _required_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProjectionValidationError(f"{field_name} must be a lowercase SHA-256")
    return value


def _timestamp(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProjectionValidationError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectionValidationError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ProjectionValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionValidationError(f"{field_name} must be an object")
    copied = dict(value)
    encoded = canonical_json(copied)
    if len(encoded.encode("utf-8")) > 32_768:
        raise ProjectionValidationError(f"{field_name} is too large")
    return copied


class ProjectionError(RuntimeError):
    """Base class for projection failures."""

    error_code = "hindsight_projection_error"


class ProjectionValidationError(ProjectionError, ValueError):
    error_code = "invalid_hindsight_projection"


class ProjectionConflict(ProjectionError):
    error_code = "hindsight_projection_conflict"


class ProjectionNotFound(ProjectionError):
    error_code = "hindsight_projection_not_found"


class ProjectionLeaseConflict(ProjectionError):
    error_code = "hindsight_projection_lease_conflict"


class ProjectionIneligible(ProjectionError):
    error_code = "hindsight_projection_ineligible"


class ProjectionDeliveryNotStarted(ProjectionError):
    error_code = "hindsight_projection_delivery_not_started"


class ProjectionAuthorizationUnavailable(ProjectionDeliveryNotStarted):
    error_code = "hindsight_projection_authorization_unavailable"


class ProjectionDeliveryAmbiguous(ProjectionError):
    error_code = "hindsight_projection_delivery_ambiguous"


class ProjectionDestinationError(ProjectionError):
    error_code = "hindsight_projection_destination_error"


class DesiredProjectionState(str, Enum):
    UPSERT = "upsert"
    REMOVE = "remove"


class OutboxState(str, Enum):
    PENDING = "pending"
    DELIVERING = "delivering"
    RECONCILING = "reconciling"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"


class ReceiptState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"


class DestinationObservationState(str, Enum):
    PRESENT_MATCH = "present_match"
    PRESENT_OTHER = "present_other"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ReconciliationState(str, Enum):
    APPLIED = "applied"
    NOT_STARTED = "not_started"
    SENT_DESTINATION_MISSING = "sent_destination_missing"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ProjectionIntentSpec:
    """Content-minimized desired projection written in a Truth transaction."""

    claim_id: str
    claim_generation: str
    policy_id: str
    desired_state: DesiredProjectionState
    reason_code: str
    eligibility_sha256: str
    authorization_ref: str
    purge_projection_source: bool = False
    requested_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _required_id(self.claim_id, "claim_id"))
        object.__setattr__(
            self,
            "claim_generation",
            _required_digest(self.claim_generation, "claim_generation"),
        )
        object.__setattr__(self, "policy_id", _required_token(self.policy_id, "policy_id"))
        if not isinstance(self.desired_state, DesiredProjectionState):
            try:
                object.__setattr__(
                    self, "desired_state", DesiredProjectionState(self.desired_state)
                )
            except (TypeError, ValueError) as exc:
                raise ProjectionValidationError("desired_state is invalid") from exc
        object.__setattr__(
            self, "reason_code", _required_token(self.reason_code, "reason_code")
        )
        object.__setattr__(
            self,
            "eligibility_sha256",
            _required_digest(self.eligibility_sha256, "eligibility_sha256"),
        )
        object.__setattr__(
            self,
            "authorization_ref",
            _required_ref(self.authorization_ref, "authorization_ref"),
        )
        if not isinstance(self.purge_projection_source, bool):
            raise ProjectionValidationError("purge_projection_source must be boolean")
        _timestamp(self.requested_at, "requested_at")
        if self.desired_state is DesiredProjectionState.UPSERT and self.purge_projection_source:
            raise ProjectionValidationError("an upsert cannot purge its projection source")

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            "schema": "wb.truth-hindsight-intent/v1",
            "claim_id": self.claim_id,
            "claim_generation": self.claim_generation,
            "policy_id": self.policy_id,
            "desired_state": self.desired_state.value,
            "reason_code": self.reason_code,
            "eligibility_sha256": self.eligibility_sha256,
            "authorization_ref": self.authorization_ref,
            "purge_projection_source": self.purge_projection_source,
        }

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.fingerprint_payload())


@dataclass(frozen=True, slots=True)
class SourceDependency:
    """One exact source dependency retained for redaction accounting."""

    source_ref: str
    representation_id: str
    content_sha256: str
    relation: str
    selector: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not self.source_ref.startswith(
            "wb-source://"
        ):
            raise ProjectionValidationError("source_ref must be a canonical SourceRef URI")
        if len(self.source_ref) > 1024 or any(ord(ch) < 32 for ch in self.source_ref):
            raise ProjectionValidationError("source_ref is invalid")
        object.__setattr__(
            self,
            "representation_id",
            _required_id(self.representation_id, "representation_id"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _required_digest(self.content_sha256, "content_sha256"),
        )
        object.__setattr__(self, "relation", _required_token(self.relation, "relation"))
        object.__setattr__(self, "selector", _json_object(self.selector, "selector"))

    def to_dict(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "representation_id": self.representation_id,
            "content_sha256": self.content_sha256,
            "relation": self.relation,
            "selector": dict(self.selector),
        }


@dataclass(frozen=True, slots=True)
class ProjectionClaimSnapshot:
    """Exact current Truth snapshot resolved only for a current upsert intent."""

    claim_id: str
    policy_id: str
    claim_generation: str
    claim_canonical_sha256: str
    proposition: str
    claim_kind: str
    lifecycle_status: str
    lifecycle_event_id: str
    applicability_scope: Mapping[str, Any]
    valid_from: str | None
    valid_to: str | None
    current: bool
    policy_eligible: bool
    source_state: str
    eligibility_sha256: str
    evaluated_at: str
    source_dependencies: Sequence[SourceDependency] = ()
    projection_method: str = "hindsight_llm_retain_v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _required_id(self.claim_id, "claim_id"))
        object.__setattr__(self, "policy_id", _required_token(self.policy_id, "policy_id"))
        for name in (
            "claim_generation",
            "claim_canonical_sha256",
            "eligibility_sha256",
        ):
            object.__setattr__(self, name, _required_digest(getattr(self, name), name))
        if not isinstance(self.proposition, str) or not self.proposition:
            raise ProjectionValidationError("proposition must be non-empty exact text")
        if len(self.proposition.encode("utf-8")) > 1_000_000:
            raise ProjectionValidationError("proposition is too large for projection")
        object.__setattr__(self, "claim_kind", _required_token(self.claim_kind, "claim_kind"))
        object.__setattr__(
            self,
            "lifecycle_status",
            _required_token(self.lifecycle_status, "lifecycle_status"),
        )
        object.__setattr__(
            self,
            "lifecycle_event_id",
            _required_id(self.lifecycle_event_id, "lifecycle_event_id"),
        )
        object.__setattr__(
            self,
            "applicability_scope",
            _json_object(self.applicability_scope, "applicability_scope"),
        )
        _timestamp(self.valid_from, "valid_from")
        _timestamp(self.valid_to, "valid_to")
        if self.valid_from and self.valid_to:
            assert _timestamp(self.valid_from, "valid_from") is not None
            assert _timestamp(self.valid_to, "valid_to") is not None
            if _timestamp(self.valid_from, "valid_from") >= _timestamp(
                self.valid_to, "valid_to"
            ):
                raise ProjectionValidationError("valid time must be a forward interval")
        if not isinstance(self.current, bool) or not isinstance(self.policy_eligible, bool):
            raise ProjectionValidationError("current and policy_eligible must be boolean")
        object.__setattr__(
            self, "source_state", _required_token(self.source_state, "source_state")
        )
        _timestamp(self.evaluated_at, "evaluated_at")
        object.__setattr__(
            self,
            "projection_method",
            _required_token(self.projection_method, "projection_method"),
        )
        dependencies = tuple(self.source_dependencies)
        if len(dependencies) > 256 or not all(
            isinstance(item, SourceDependency) for item in dependencies
        ):
            raise ProjectionValidationError("source_dependencies are invalid")
        object.__setattr__(self, "source_dependencies", dependencies)

    @property
    def proposition_bytes(self) -> bytes:
        return self.proposition.encode("utf-8")

    @property
    def proposition_sha256(self) -> str:
        return hashlib.sha256(self.proposition_bytes).hexdigest()

    def validate_for(self, intent: ProjectionIntentSpec, *, at: str | None = None) -> None:
        if intent.desired_state is not DesiredProjectionState.UPSERT:
            raise ProjectionIneligible("a content snapshot cannot satisfy a remove intent")
        if (
            self.claim_id != intent.claim_id
            or self.policy_id != intent.policy_id
            or self.claim_generation != intent.claim_generation
            or self.eligibility_sha256 != intent.eligibility_sha256
        ):
            raise ProjectionIneligible("Truth snapshot no longer matches the intent")
        if self.lifecycle_status != "confirmed" or not self.current or not self.policy_eligible:
            raise ProjectionIneligible("only current policy-eligible confirmed claims project")
        if self.source_state not in {"clean", "current"}:
            raise ProjectionIneligible("source state does not permit projection")
        moment = _timestamp(at or utc_now(), "at")
        assert moment is not None
        valid_from = _timestamp(self.valid_from, "valid_from")
        valid_to = _timestamp(self.valid_to, "valid_to")
        if valid_from is not None and moment < valid_from:
            raise ProjectionIneligible("claim is not yet valid")
        if valid_to is not None and moment >= valid_to:
            raise ProjectionIneligible("claim is no longer valid")


@dataclass(frozen=True, slots=True)
class DependencyUsage:
    usage_id: str
    source_ref: str
    representation_id: str
    redaction_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage_id", _required_id(self.usage_id, "usage_id"))
        object.__setattr__(
            self,
            "representation_id",
            _required_id(self.representation_id, "representation_id"),
        )
        if not isinstance(self.source_ref, str) or not self.source_ref.startswith(
            "wb-source://"
        ):
            raise ProjectionValidationError("dependency source_ref is invalid")
        if isinstance(self.redaction_epoch, bool) or not isinstance(
            self.redaction_epoch, int
        ) or self.redaction_epoch < 0:
            raise ProjectionValidationError("redaction_epoch must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "usage_id": self.usage_id,
            "source_ref": self.source_ref,
            "representation_id": self.representation_id,
            "redaction_epoch": self.redaction_epoch,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DependencyUsage":
        return cls(
            usage_id=str(value["usage_id"]),
            source_ref=str(value["source_ref"]),
            representation_id=str(value["representation_id"]),
            redaction_epoch=int(value["redaction_epoch"]),
        )


@dataclass(frozen=True, slots=True)
class ProjectionEffect:
    effect_id: str
    spec: ProjectionIntentSpec
    state: OutboxState
    attempt_count: int
    lease_owner: str | None
    lease_expires_at: str | None
    last_error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProjectionLease:
    effect: ProjectionEffect
    attempt_no: int
    worker_id: str
    reconcile_existing: bool = False


@dataclass(frozen=True, slots=True)
class DestinationReceipt:
    document_id: str
    claim_generation: str
    acknowledged_at: str


@dataclass(frozen=True, slots=True)
class DestinationObservation:
    state: DestinationObservationState
    document_id: str
    observed_generation: str | None = None
    observed_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class DisclosureDeliveryReceipt:
    destination: DestinationReceipt
    captured_source_ref: str
    captured_representation_id: str
    content_sha256: str
    byte_length: int
    disclosure_run_id: str
    disclosure_entry_id: str
    disclosure_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class DisclosureReconciliation:
    state: ReconciliationState
    receipt: DisclosureDeliveryReceipt | None = None


@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    claim_id: str
    policy_id: str
    claim_generation: str
    state: ReceiptState
    document_id: str
    projection_method: str
    lifecycle_status: str
    applicability_scope: Mapping[str, Any]
    valid_from: str | None
    valid_to: str | None
    captured_source_ref: str | None
    captured_representation_id: str | None
    content_sha256: str | None
    disclosure_run_id: str | None
    disclosure_entry_id: str | None
    disclosure_manifest_sha256: str | None
    dependency_usages: tuple[DependencyUsage, ...]
    last_effect_id: str
    observed_at: str


class TruthProjectionReader(Protocol):
    """Narrow authoritative Truth read seam used by the projection worker."""

    def desired_for_claim(
        self, claim_id: str, policy_id: str, *, at: str
    ) -> ProjectionIntentSpec:
        """Return current desired state, including an explicit remove."""

    def resolve_snapshot(
        self, intent: ProjectionIntentSpec, *, at: str
    ) -> ProjectionClaimSnapshot:
        """Return exact current content only for a matching upsert intent."""

    def iter_desired(self, *, at: str) -> Iterable[ProjectionIntentSpec]:
        """Enumerate current projection desires for deterministic reconciliation."""


class ProjectionDestination(Protocol):
    """Replaceable Hindsight destination; never a Truth read authority."""

    def document_id(self, claim_id: str, policy_id: str) -> str: ...

    def upsert(
        self, snapshot: ProjectionClaimSnapshot, exact_content: bytes
    ) -> DestinationReceipt: ...

    def remove(self, document_id: str) -> DestinationReceipt: ...

    def inspect(
        self, document_id: str, expected_generation: str
    ) -> DestinationObservation: ...


class ProjectionDisclosureTransport(Protocol):
    """Agent Execution-owned exact-content disclosure boundary."""

    def deliver(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
        destination: ProjectionDestination,
    ) -> DisclosureDeliveryReceipt: ...

    def reconcile(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
        destination: ProjectionDestination,
    ) -> DisclosureReconciliation: ...

    def redact_captured_source(
        self, source_ref: str, *, authorization_ref: str, reason_code: str
    ) -> None: ...


class ProjectionDependencyRegistry(Protocol):
    """Sources-owned semantic-derivative usage handshake."""

    def reserve(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
    ) -> tuple[DependencyUsage, ...]: ...

    def acknowledge(self, usages: Sequence[DependencyUsage]) -> None: ...

    def release(self, usages: Sequence[DependencyUsage]) -> None: ...


def projection_generation_sha256(
    *,
    claim_canonical_sha256: str,
    lifecycle_event_id: str,
    lifecycle_status: str,
    policy_id: str,
    source_state_sha256: str,
) -> str:
    """Build the generation fence from Truth and source-lifecycle state."""

    return canonical_sha256(
        {
            "schema": "wb.truth-hindsight-generation/v1",
            "claim_canonical_sha256": _required_digest(
                claim_canonical_sha256, "claim_canonical_sha256"
            ),
            "lifecycle_event_id": _required_id(lifecycle_event_id, "lifecycle_event_id"),
            "lifecycle_status": _required_token(lifecycle_status, "lifecycle_status"),
            "policy_id": _required_token(policy_id, "policy_id"),
            "source_state_sha256": _required_digest(
                source_state_sha256, "source_state_sha256"
            ),
        }
    )
