"""Authority-qualified value objects for the Sources bounded context."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from work_buddy.security.actors import ACTOR_REF_SCHEMA, ActorRef
from work_buddy.sources.errors import InvalidSourceReference, InvalidSourceRequest


SOURCE_REF_SCHEMA = "wb.source-ref/v1"
ORIGIN_REF_SCHEMA = "wb.origin-ref/v1"
RESOLUTION_RECORD_SCHEMA = "wb.source-resolution/v1"
EXPORT_SCHEMA = "wb.sources-export/v1"

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    """Serialize metadata deterministically without normalizing string content."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _opaque(value: str, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise InvalidSourceReference()
    return value


def _bounded(value: str | None, label: str, *, limit: int = 1024) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise InvalidSourceRequest()
    return value


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise InvalidSourceRequest()
    return value


@dataclass(frozen=True, slots=True)
class SourceRef:
    authority_id: str
    item_id: str
    schema: str = SOURCE_REF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SOURCE_REF_SCHEMA:
            raise InvalidSourceReference()
        _opaque(self.authority_id, "authority_id")
        _opaque(self.item_id, "item_id")

    @property
    def uri(self) -> str:
        return f"wb-source://{self.authority_id}/item/{self.item_id}"

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "authority_id": self.authority_id,
            "item_id": self.item_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceRef":
        if set(value) != {"schema", "authority_id", "item_id"}:
            raise InvalidSourceReference()
        return cls(
            schema=str(value["schema"]),
            authority_id=str(value["authority_id"]),
            item_id=str(value["item_id"]),
        )

    @classmethod
    def parse(cls, value: str) -> "SourceRef":
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise InvalidSourceReference() from exc
        parts = parsed.path.split("/")
        if (
            parsed.scheme != "wb-source"
            or not parsed.netloc
            or parts != ["", "item", parts[-1]]
            or len(parts) != 3
            or parsed.query
            or parsed.fragment
            or "@" in parsed.netloc
            or ":" in parsed.netloc
        ):
            raise InvalidSourceReference()
        ref = cls(authority_id=parsed.netloc, item_id=parts[2])
        if ref.uri != value:
            raise InvalidSourceReference()
        return ref


@dataclass(frozen=True, slots=True)
class OriginRef:
    provider_id: str
    native_item_id: str
    container_id: str | None = None
    revision: str | None = None
    part: str | None = None
    coordinates: Mapping[str, str] = field(default_factory=dict)
    schema: str = ORIGIN_REF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ORIGIN_REF_SCHEMA or not _PROVIDER_RE.fullmatch(self.provider_id):
            raise InvalidSourceReference()
        _bounded(self.native_item_id, "native_item_id", limit=2048)
        _bounded(self.container_id, "container_id", limit=2048)
        _bounded(self.revision, "revision", limit=512)
        _bounded(self.part, "part", limit=512)
        if len(self.coordinates) > 32:
            raise InvalidSourceRequest()
        for key, item in self.coordinates.items():
            if not _ROLE_RE.fullmatch(str(key)):
                raise InvalidSourceRequest()
            _bounded(str(item), "coordinate", limit=2048)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provider_id": self.provider_id,
            "container_id": self.container_id,
            "native_item_id": self.native_item_id,
            "revision": self.revision,
            "part": self.part,
            "coordinates": dict(self.coordinates),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OriginRef":
        required = {
            "schema",
            "provider_id",
            "container_id",
            "native_item_id",
            "revision",
            "part",
            "coordinates",
        }
        if set(value) != required or not isinstance(value["coordinates"], Mapping):
            raise InvalidSourceReference()
        return cls(
            schema=str(value["schema"]),
            provider_id=str(value["provider_id"]),
            container_id=(None if value["container_id"] is None else str(value["container_id"])),
            native_item_id=str(value["native_item_id"]),
            revision=(None if value["revision"] is None else str(value["revision"])),
            part=(None if value["part"] is None else str(value["part"])),
            coordinates={str(k): str(v) for k, v in value["coordinates"].items()},
        )

    @property
    def occurrence_key(self) -> str:
        return canonical_sha256(
            {
                "provider_id": self.provider_id,
                "container_id": self.container_id,
                "native_item_id": self.native_item_id,
                "part": self.part,
                "coordinates": dict(self.coordinates),
            }
        )


@dataclass(frozen=True, slots=True)
class AttributionAssertion:
    role: str
    actor: ActorRef | None
    state: str = "identified"
    basis: str = "unknown"
    assurance: str = "unknown"
    asserted_by: ActorRef | None = None
    selector: Mapping[str, Any] | None = None
    observed_at: str | None = None
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        if not _ROLE_RE.fullmatch(self.role):
            raise InvalidSourceRequest()
        if self.state not in {"identified", "unknown", "mixed"}:
            raise InvalidSourceRequest()
        if self.state == "identified" and self.actor is None:
            raise InvalidSourceRequest()
        if self.state != "identified" and self.actor is not None:
            raise InvalidSourceRequest()
        _bounded(self.basis, "basis", limit=128)
        _bounded(self.assurance, "assurance", limit=128)


@dataclass(frozen=True, slots=True)
class SourceItem:
    source_ref: SourceRef
    custodian_authority_id: str
    primary_representation_id: str
    origin_ref: OriginRef | None
    native_revision: str | None
    source_role: str
    fidelity: str
    tenant_scope_id: str
    originating_surface: str
    namespace: str | None
    sensitivity_class: str
    retention_class: str
    occurred_at: str | None
    received_at: str
    committed_at: str
    lifecycle_state: str
    redaction_epoch: int
    redaction_event_id: str | None


@dataclass(frozen=True, slots=True)
class SourceRepresentation:
    representation_id: str
    source_ref: SourceRef
    kind: str
    media_type: str
    content_sha256: str
    byte_length: int
    character_length: int | None
    encoding: str | None
    schema_type: str | None
    inline: bool
    derivation_relation: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SourceObservation:
    observation_id: str
    source_ref: SourceRef
    kind: str
    resolver_id: str
    resolver_version: str
    observed_at: str
    status: str
    native_revision: str | None = None
    content_sha256: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDerivation:
    derivation_id: str
    derived_ref: SourceRef
    input_ref: SourceRef
    relation: str
    producer: ActorRef
    activity_id: str
    selector: Mapping[str, Any] | None
    method: Mapping[str, Any]
    fidelity: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AccessBinding:
    binding_id: str
    source_ref: SourceRef
    principal: ActorRef
    purpose: str
    access_mode: str
    scope: Mapping[str, str]
    external_recipient: str | None
    model_id: str | None
    egress_class: str | None
    content_boundary: Mapping[str, Any] | None
    authorization_fingerprint: str
    expires_at: str | None
    revoked_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class UsageReservation:
    usage_id: str
    source_ref: SourceRef
    representation_id: str
    redaction_epoch: int
    status: str
    request_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """Trust-bearing transient returned only by an internal authorized resolver."""

    source_ref: SourceRef
    representation: SourceRepresentation
    content: bytes
    origin_ref: OriginRef | None
    native_revision: str | None
    attributions: tuple[AttributionAssertion, ...]
    fidelity: str
    resolver_id: str
    resolver_version: str
    capture_observation_id: str | None
    current_observation_id: str
    authorization_context_sha256: str
    redaction_epoch: int
    resolved_at: str

    def to_resolution_record(
        self,
        *,
        selector: Mapping[str, Any] | None = None,
        include_excerpt: bool = False,
    ) -> "SourceResolutionRecord":
        return SourceResolutionRecord(
            source_ref=self.source_ref,
            representation_id=self.representation.representation_id,
            content_sha256=self.representation.content_sha256,
            media_type=self.representation.media_type,
            byte_length=self.representation.byte_length,
            selector=selector,
            excerpt=(self.content if include_excerpt else None),
            resolver_id=self.resolver_id,
            resolver_version=self.resolver_version,
            observation_id=self.current_observation_id,
            redaction_epoch=self.redaction_epoch,
            resolved_at=self.resolved_at,
        )


@dataclass(frozen=True, slots=True)
class SourceResolutionRecord:
    source_ref: SourceRef
    representation_id: str
    content_sha256: str
    media_type: str
    byte_length: int
    selector: Mapping[str, Any] | None
    excerpt: bytes | None
    resolver_id: str
    resolver_version: str
    observation_id: str
    redaction_epoch: int
    resolved_at: str
    schema: str = RESOLUTION_RECORD_SCHEMA


@dataclass(frozen=True, slots=True)
class OutboxEffect:
    effect_id: str
    command_id: str | None
    target_domain: str
    effect_type: str
    payload: Mapping[str, Any]
    payload_sha256: str
    authorization_fingerprint: str
    authorization_expires_at: str | None
    status: str
    attempts: int
    lease_owner: str | None
    lease_until: str | None
    result_ref: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    receipt_id: str
    effect_id: str
    target_domain: str
    result_ref: str
    result_sha256: str
    received_at: str


@dataclass(frozen=True, slots=True)
class RedactionResult:
    redaction_event_id: str
    source_ref: SourceRef
    redaction_epoch: int
    managed_copy_state: str
    issued_copy_state: str
    pending_effect_ids: tuple[str, ...]
    redacted_at: str


def actor_sequence_json(values: Sequence[ActorRef]) -> str:
    return canonical_json([item.to_dict() for item in values])
