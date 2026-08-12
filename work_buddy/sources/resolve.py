"""Authorized retained-source resolution and redaction-epoch reservation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceContentTooLarge,
    SourceIntegrityFailure,
    SourceRedacted,
)
from work_buddy.sources.models import (
    ActorRef,
    ResolvedSource,
    SourceRef,
    UsageReservation,
    canonical_sha256,
    new_id,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.store import SourceStore, _parse_json_object


@dataclass(frozen=True, slots=True)
class ReservedResolution:
    reservation: UsageReservation
    resolved: ResolvedSource


def resolve_source(
    store: SourceStore,
    *,
    source_ref: SourceRef,
    principal: ActorRef,
    purpose: str,
    representation_id: str | None = None,
    expected_digest: str | None = None,
    access_mode: str = "content",
    external_recipient: str | None = None,
    model_id: str | None = None,
    egress_class: str | None = None,
    at: str | None = None,
) -> ResolvedSource:
    """Resolve exact retained bytes inside the trusted backend boundary."""

    with store.write_transaction() as conn:
        return _resolve_in_transaction(
            store,
            conn,
            source_ref=source_ref,
            principal=principal,
            purpose=purpose,
            representation_id=representation_id,
            expected_digest=expected_digest,
            access_mode=access_mode,
            external_recipient=external_recipient,
            model_id=model_id,
            egress_class=egress_class,
            at=at,
        )


def resolve_and_reserve_source(
    store: SourceStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    principal: ActorRef,
    purpose: str,
    consumer_domain: str,
    consumer_id: str,
    use_kind: str,
    disclosure_kind: str,
    redaction_policy: str,
    selector: Mapping[str, Any] | None = None,
    expected_digest: str | None = None,
    external_recipient: str | None = None,
    model_id: str | None = None,
    egress_class: str | None = None,
    at: str | None = None,
) -> ReservedResolution:
    """Reserve a use before making exact content available to its consumer."""

    with store.write_transaction() as conn:
        reservation = store._reserve_usage(
            conn,
            source_ref=source_ref,
            representation_id=representation_id,
            principal=principal,
            purpose=purpose,
            consumer_domain=consumer_domain,
            consumer_id=consumer_id,
            use_kind=use_kind,
            disclosure_kind=disclosure_kind,
            redaction_policy=redaction_policy,
            selector=selector,
            external_recipient=external_recipient,
            model_id=model_id,
            egress_class=egress_class,
            at=at,
        )
        resolved = _resolve_in_transaction(
            store,
            conn,
            source_ref=source_ref,
            principal=principal,
            purpose=purpose,
            representation_id=representation_id,
            expected_digest=expected_digest,
            access_mode=("metadata" if disclosure_kind == "metadata_only" else "content"),
            external_recipient=external_recipient,
            model_id=model_id,
            egress_class=egress_class,
            at=at,
        )
        if resolved.redaction_epoch != reservation.redaction_epoch:
            raise SourceIntegrityFailure()
        return ReservedResolution(reservation=reservation, resolved=resolved)


def _resolve_in_transaction(
    store: SourceStore,
    conn: sqlite3.Connection,
    *,
    source_ref: SourceRef,
    principal: ActorRef,
    purpose: str,
    representation_id: str | None,
    expected_digest: str | None,
    access_mode: str,
    external_recipient: str | None,
    model_id: str | None,
    egress_class: str | None,
    at: str | None,
) -> ResolvedSource:
    if access_mode not in {"metadata", "content"}:
        raise InvalidSourceRequest()
    if expected_digest is not None:
        validate_sha256(expected_digest)
    resolved_at = at or utc_now()
    item = store._get_item(conn, source_ref)
    assert item is not None
    if item.lifecycle_state == "redacted":
        raise SourceRedacted()
    binding = store._find_access_binding(
        conn,
        source_ref=source_ref,
        principal=principal,
        purpose=purpose,
        access_mode=access_mode,
        at=resolved_at,
        external_recipient=external_recipient,
        model_id=model_id,
        egress_class=egress_class,
    )
    row = store._representation_row(conn, source_ref, representation_id)
    representation = store._representation_from_row(row)
    if expected_digest is not None and representation.content_sha256 != expected_digest:
        raise SourceIntegrityFailure()
    boundary = (
        _parse_json_object(binding["content_boundary_json"])
        if binding["content_boundary_json"]
        else {}
    )
    allowed_representation = boundary.get("representation_id")
    if allowed_representation is not None and allowed_representation != representation.representation_id:
        from work_buddy.sources.errors import SourceAccessDenied

        raise SourceAccessDenied()
    max_bytes = boundary.get("max_bytes")
    if max_bytes is not None:
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise SourceIntegrityFailure()
        if representation.byte_length > max_bytes:
            raise SourceContentTooLarge()
    content = b"" if access_mode == "metadata" else store._read_representation_row(row)
    observation = store._add_observation(
        conn,
        source_ref,
        kind="snapshot_integrity_ok",
        resolver_id="sources-retained",
        resolver_version="1",
        status="ok",
        retained_sha256=representation.content_sha256,
        native_revision=item.native_revision,
        observed_at=resolved_at,
    )
    capture = conn.execute(
        "SELECT observation_id FROM source_observations "
        "WHERE authority_id = ? AND source_item_id = ? AND observation_kind = 'captured' "
        "ORDER BY observed_at, observation_id LIMIT 1",
        (source_ref.authority_id, source_ref.item_id),
    ).fetchone()
    authorization_context = canonical_sha256(
        {
            "binding_id": binding["binding_id"],
            "source_ref": source_ref.to_dict(),
            "representation_id": representation.representation_id,
            "content_sha256": representation.content_sha256,
            "purpose": purpose,
            "access_mode": access_mode,
            "external_recipient": external_recipient,
            "model_id": model_id,
            "egress_class": egress_class,
            "redaction_epoch": item.redaction_epoch,
        }
    )
    conn.execute(
        "INSERT INTO source_access_audit "
        "(audit_id, binding_id, authority_id, source_item_id, representation_id, "
        " access_mode, purpose, authorization_context_sha256, accessed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(),
            binding["binding_id"],
            source_ref.authority_id,
            source_ref.item_id,
            representation.representation_id,
            access_mode,
            purpose,
            authorization_context,
            resolved_at,
        ),
    )
    return ResolvedSource(
        source_ref=source_ref,
        representation=representation,
        content=content,
        origin_ref=item.origin_ref,
        native_revision=item.native_revision,
        attributions=store.current_attributions(conn, source_ref),
        fidelity=item.fidelity,
        resolver_id="sources-retained",
        resolver_version="1",
        capture_observation_id=(str(capture["observation_id"]) if capture else None),
        current_observation_id=observation.observation_id,
        authorization_context_sha256=authorization_context,
        redaction_epoch=item.redaction_epoch,
        resolved_at=resolved_at,
    )
