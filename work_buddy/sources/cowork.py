"""Stable Sources provider for frozen Co-work action snapshots.

The provider resolves an action snapshot by its durable store and snapshot
identities.  It copies the already-frozen target bytes; live editor state is
never consulted and an origin is never recovered by searching for matching
text.
"""

from __future__ import annotations

from typing import Any

from work_buddy.cowork.verify import ActionSnapshot
from work_buddy.cowork.verify import store as verify_store
from work_buddy.security.actors import ActorRef
from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceAccessDenied,
    SourceIntegrityFailure,
    SourceNotFound,
    SourceOriginMismatch,
)
from work_buddy.sources.models import (
    AttributionAssertion,
    OriginRef,
    canonical_sha256,
    sha256_bytes,
    utc_now,
)
from work_buddy.sources.providers import NativeCapture, NativeObservation
from work_buddy.truth.registry import TruthStoreRegistry


COWORK_DOCUMENT_PROVIDER_ID = "cowork-document"


def cowork_action_snapshot_origin(
    *,
    store_id: str,
    action_snapshot_id: str,
    revision: str,
    part: str = "target",
) -> OriginRef:
    """Return the provider-native identity of one frozen action-snapshot part."""

    return OriginRef(
        provider_id=COWORK_DOCUMENT_PROVIDER_ID,
        native_item_id=action_snapshot_id,
        container_id=store_id,
        revision=revision,
        part=part,
    )


class CoworkActionSnapshotProvider:
    """Resolve retained Co-work snapshot bytes through the Sources boundary."""

    provider_id = COWORK_DOCUMENT_PROVIDER_ID
    version = "1"
    stable_occurrence_identity = True

    def __init__(
        self,
        *,
        tenant_scope_id: str,
        issuer: ActorRef,
        registry: TruthStoreRegistry | None = None,
    ) -> None:
        if issuer.tenant_scope_id != tenant_scope_id:
            raise InvalidSourceRequest()
        self.tenant_scope_id = tenant_scope_id
        self.issuer = issuer
        self.registry = registry or TruthStoreRegistry()

    def canonicalize_origin(self, origin_ref: OriginRef) -> OriginRef:
        if (
            origin_ref.provider_id != self.provider_id
            or not origin_ref.container_id
            or not origin_ref.native_item_id
            or not origin_ref.revision
            or origin_ref.part not in {"target", "projection"}
        ):
            raise SourceOriginMismatch()
        return OriginRef.from_dict(origin_ref.to_dict())

    def authorize(
        self, origin_ref: OriginRef, principal: ActorRef, purpose: str
    ) -> bool:
        try:
            self.canonicalize_origin(origin_ref)
        except SourceOriginMismatch:
            return False
        return (
            principal.tenant_scope_id == self.tenant_scope_id
            and isinstance(purpose, str)
            and bool(purpose)
        )

    def capture(self, origin_ref: OriginRef, purpose: str) -> NativeCapture:
        canonical = self.canonicalize_origin(origin_ref)
        action, content, digest = self._resolve(canonical)
        return NativeCapture(
            exact_content=content,
            media_type=(
                "text/plain; charset=utf-8"
                if canonical.part == "target"
                else "text/markdown; charset=utf-8"
            ),
            representation_kind="canonical_text",
            encoding="utf-8",
            source_role="document_selection",
            fidelity="exact_frozen_snapshot",
            native_revision=action.canonical_sha256,
            occurred_at=action.created_at,
            observed_at=utc_now(),
            authorization_fingerprint=canonical_sha256(
                {
                    "provider_id": self.provider_id,
                    "origin": canonical.to_dict(),
                    "purpose": purpose,
                    "content_sha256": digest,
                }
            ),
            attributions=(
                AttributionAssertion(
                    role="author",
                    actor=None,
                    state="unknown",
                    basis="frozen_document_snapshot_does_not_establish_authorship",
                    assurance="unknown",
                    asserted_by=self.issuer,
                ),
                AttributionAssertion(
                    role="issuer",
                    actor=self.issuer,
                    basis="cowork_action_snapshot_store",
                    assurance="trusted_component",
                    asserted_by=self.issuer,
                ),
            ),
        )

    def observe(self, origin_ref: OriginRef) -> NativeObservation:
        canonical = self.canonicalize_origin(origin_ref)
        action, _content, digest = self._resolve(canonical)
        return NativeObservation(
            kind="snapshot_integrity_ok",
            status="ok",
            observed_at=utc_now(),
            native_revision=action.canonical_sha256,
            native_content_sha256=digest,
        )

    def _resolve(self, origin_ref: OriginRef) -> tuple[ActionSnapshot, bytes, str]:
        try:
            store = self.registry.open_store(str(origin_ref.container_id))
        except Exception as exc:  # store identity is intentionally content-free
            raise SourceNotFound() from exc
        action = verify_store.get_record(
            store, ActionSnapshot, origin_ref.native_item_id
        )
        if action is None:
            raise SourceNotFound()
        if action.canonical_sha256 != origin_ref.revision:
            raise SourceOriginMismatch()
        digest = (
            action.target_blob_sha256
            if origin_ref.part == "target"
            else action.projection_blob_sha256
        )
        try:
            content = store.resolve_blob_path(f"blobs/{digest}").read_bytes()
        except OSError as exc:
            raise SourceNotFound() from exc
        if sha256_bytes(content) != digest:
            raise SourceIntegrityFailure()
        return action, content, digest


__all__ = [
    "COWORK_DOCUMENT_PROVIDER_ID",
    "CoworkActionSnapshotProvider",
    "cowork_action_snapshot_origin",
]
