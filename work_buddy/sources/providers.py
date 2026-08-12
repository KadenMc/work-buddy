"""Native-origin provider registry and exact capture/re-observation protocol."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from work_buddy.sources.errors import (
    SourceAccessDenied,
    SourceOriginMismatch,
    SourceProviderConflict,
    SourceProviderNotFound,
)
from work_buddy.sources.models import (
    ActorRef,
    AttributionAssertion,
    OriginRef,
    SourceObservation,
    SourceRef,
    canonical_json,
    canonical_sha256,
    new_id,
    sha256_bytes,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.store import SourceStore


@dataclass(frozen=True, slots=True)
class NativeCapture:
    exact_content: bytes
    media_type: str
    representation_kind: str
    encoding: str | None
    source_role: str
    fidelity: str
    native_revision: str | None
    occurred_at: str | None
    observed_at: str
    authorization_fingerprint: str
    attributions: tuple[AttributionAssertion, ...] = ()
    schema_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exact_content, bytes):
            from work_buddy.sources.errors import InvalidSourceRequest

            raise InvalidSourceRequest()
        validate_sha256(self.authorization_fingerprint)


@dataclass(frozen=True, slots=True)
class NativeObservation:
    kind: str
    status: str
    observed_at: str
    native_revision: str | None = None
    native_content_sha256: str | None = None
    error_code: str | None = None


@runtime_checkable
class SourceProvider(Protocol):
    provider_id: str
    version: str
    stable_occurrence_identity: bool

    def canonicalize_origin(self, origin_ref: OriginRef) -> OriginRef: ...

    def authorize(
        self, origin_ref: OriginRef, principal: ActorRef, purpose: str
    ) -> bool: ...

    def capture(self, origin_ref: OriginRef, purpose: str) -> NativeCapture: ...

    def observe(self, origin_ref: OriginRef) -> NativeObservation: ...


class ProviderRegistry:
    """Process-local registry; providers remain responsible for native access."""

    def __init__(self) -> None:
        self._providers: dict[str, SourceProvider] = {}

    def register(self, provider: SourceProvider) -> None:
        # OriginRef performs the provider namespace validation without exposing
        # native coordinates in an error.
        OriginRef(provider.provider_id, "registration-probe")
        existing = self._providers.get(provider.provider_id)
        if existing is not None and existing is not provider:
            raise SourceProviderConflict()
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> SourceProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise SourceProviderNotFound() from exc


def source_capture_from_origin(
    store: SourceStore,
    registry: ProviderRegistry,
    *,
    provider_id: str,
    origin_ref: OriginRef,
    principal: ActorRef,
    purpose: str,
    tenant_scope_id: str,
    originating_surface: str,
    expected_revision: str | None = None,
    expected_digest: str | None = None,
    client_mutation_id: str | None = None,
    namespace: str | None = None,
    sensitivity_class: str = "private",
    retention_class: str = "durable",
) -> SourceRef:
    """Capture one provider-native occurrence without deduplicating by text."""

    provider = registry.get(provider_id)
    if principal.tenant_scope_id != tenant_scope_id:
        raise SourceAccessDenied()
    if origin_ref.provider_id != provider_id:
        raise SourceOriginMismatch()
    canonical = provider.canonicalize_origin(origin_ref)
    if canonical.provider_id != provider_id:
        raise SourceOriginMismatch()
    if not provider.authorize(canonical, principal, purpose):
        raise SourceAccessDenied()

    provider_actor = ActorRef(
        issuer_authority_id=principal.issuer_authority_id,
        subject=f"provider-{provider_id}",
        kind="service",
        tenant_scope_id=tenant_scope_id,
    )
    unstable_request_hash = canonical_sha256(
        {
            "provider_id": provider_id,
            "origin": canonical.to_dict(),
            "principal": principal.to_dict(),
            "purpose": purpose,
            "expected_revision": expected_revision,
            "expected_digest": expected_digest,
            "namespace": namespace,
            "sensitivity_class": sensitivity_class,
            "retention_class": retention_class,
        }
    )
    if not provider.stable_occurrence_identity:
        if not client_mutation_id:
            raise SourceOriginMismatch()
        conn = store.connect()
        try:
            result = store.idempotency_result(
                conn,
                tenant_scope_id=tenant_scope_id,
                issuer=provider_actor,
                principal=principal,
                client_mutation_id=client_mutation_id,
                request_sha256=unstable_request_hash,
            )
        finally:
            conn.close()
        if result is not None:
            return SourceRef.from_dict(result["source_ref"])

    captured = provider.capture(canonical, purpose)
    digest = sha256_bytes(captured.exact_content)
    if expected_digest is not None and digest != expected_digest:
        raise SourceOriginMismatch()
    if expected_revision is not None and captured.native_revision != expected_revision:
        raise SourceOriginMismatch()
    ref: SourceRef | None = None
    mismatch = False
    with store.write_transaction() as conn:
        if not provider.stable_occurrence_identity:
            assert client_mutation_id is not None
            existing = store.idempotency_result(
                conn,
                tenant_scope_id=tenant_scope_id,
                issuer=provider_actor,
                principal=principal,
                client_mutation_id=client_mutation_id,
                request_sha256=unstable_request_hash,
            )
            if existing is not None:
                return SourceRef.from_dict(existing["source_ref"])

        revision_key = captured.native_revision or canonical.revision or ""
        part_key = canonical.part or ""
        existing_row = None
        if provider.stable_occurrence_identity:
            existing_row = conn.execute(
                "SELECT * FROM source_origin_identities WHERE provider_id = ? "
                "AND occurrence_key = ? AND native_part = ? AND native_revision = ?",
                (provider_id, canonical.occurrence_key, part_key, revision_key),
            ).fetchone()
        if existing_row is not None:
            ref = SourceRef(
                str(existing_row["authority_id"]), str(existing_row["source_item_id"])
            )
            if existing_row["content_sha256"] != digest:
                store._add_observation(
                    conn,
                    ref,
                    kind="identity_mismatch",
                    resolver_id=provider_id,
                    resolver_version=provider.version,
                    status="conflict",
                    native_revision=captured.native_revision,
                    native_content_sha256=digest,
                    error_code="origin_identity_content_mismatch",
                    observed_at=captured.observed_at,
                )
                mismatch = True
            else:
                store._add_observation(
                    conn,
                    ref,
                    kind="origin_unchanged",
                    resolver_id=provider_id,
                    resolver_version=provider.version,
                    status="ok",
                    native_revision=captured.native_revision,
                    native_content_sha256=digest,
                    retained_sha256=digest,
                    observed_at=captured.observed_at,
                )
        else:
            staged = store._stage_if_needed(captured.exact_content, conn=conn)
            prior = None
            if provider.stable_occurrence_identity:
                prior = conn.execute(
                    "SELECT authority_id, source_item_id FROM source_origin_identities "
                    "WHERE provider_id = ? AND occurrence_key = ? AND native_part = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (provider_id, canonical.occurrence_key, part_key),
                ).fetchone()
            item = store._capture_source(
                conn,
                content=captured.exact_content,
                staged_blob=staged,
                source_role=captured.source_role,
                tenant_scope_id=tenant_scope_id,
                originating_surface=originating_surface,
                media_type=captured.media_type,
                representation_kind=captured.representation_kind,
                encoding=captured.encoding,
                schema_type=captured.schema_type,
                origin_ref=canonical,
                native_revision=captured.native_revision,
                fidelity=captured.fidelity,
                namespace=namespace,
                sensitivity_class=sensitivity_class,
                retention_class=retention_class,
                occurred_at=captured.occurred_at,
                provider_observed_at=captured.observed_at,
                received_at=utc_now(),
                attributions=captured.attributions,
                producer=provider_actor,
            )
            ref = item.source_ref
            store._grant_access(
                conn,
                source_ref=ref,
                principal=principal,
                purpose=purpose,
                access_mode="content",
                authorization_fingerprint=captured.authorization_fingerprint,
                scope={"tenant_scope_id": tenant_scope_id},
                trusted_service_id=provider_id,
            )
            if provider.stable_occurrence_identity:
                conn.execute(
                    "INSERT INTO source_origin_identities "
                    "(provider_id, occurrence_key, native_part, native_revision, "
                    " authority_id, source_item_id, content_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        provider_id,
                        canonical.occurrence_key,
                        part_key,
                        revision_key,
                        ref.authority_id,
                        ref.item_id,
                        digest,
                        utc_now(),
                    ),
                )
                if prior is not None:
                    conn.execute(
                        "INSERT INTO source_derivations "
                        "(derivation_id, derived_authority_id, derived_item_id, "
                        " input_authority_id, input_item_id, relation, producer_ref_json, "
                        " activity_id, method_json, fidelity, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'revised_from', ?, ?, '{}', 'provider_revision', ?)",
                        (
                            new_id(),
                            ref.authority_id,
                            ref.item_id,
                            prior["authority_id"],
                            prior["source_item_id"],
                            canonical_json(provider_actor.to_dict()),
                            f"provider-revision-{new_id()}",
                            utc_now(),
                        ),
                    )
            else:
                assert client_mutation_id is not None
                store.record_idempotency(
                    conn,
                    tenant_scope_id=tenant_scope_id,
                    issuer=provider_actor,
                    principal=principal,
                    client_mutation_id=client_mutation_id,
                    request_sha256=unstable_request_hash,
                    result={"source_ref": ref.to_dict()},
                )
    if mismatch:
        raise SourceOriginMismatch()
    assert ref is not None
    return ref


def reobserve_origin(
    store: SourceStore,
    registry: ProviderRegistry,
    *,
    source_ref: SourceRef,
    principal: ActorRef,
    purpose: str = "recheck",
) -> SourceObservation:
    item = store.get_item(source_ref)
    if item is None or item.origin_ref is None:
        raise SourceProviderNotFound()
    provider = registry.get(item.origin_ref.provider_id)
    canonical = provider.canonicalize_origin(item.origin_ref)
    if not provider.authorize(canonical, principal, purpose):
        raise SourceAccessDenied()
    observation = provider.observe(canonical)
    return store.add_observation(
        source_ref,
        kind=observation.kind,
        resolver_id=provider.provider_id,
        resolver_version=provider.version,
        status=observation.status,
        observed_at=observation.observed_at,
        native_revision=observation.native_revision,
        native_content_sha256=observation.native_content_sha256,
        error_code=observation.error_code,
    )
