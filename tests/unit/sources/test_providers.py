from __future__ import annotations

from dataclasses import dataclass

import pytest

from work_buddy.sources import (
    ActorRef,
    NativeCapture,
    NativeObservation,
    OriginRef,
    ProviderRegistry,
    SourceOriginMismatch,
    reobserve_origin,
    source_capture_from_origin,
)


@dataclass
class FakeProvider:
    provider_id: str = "native-chat"
    version: str = "1"
    stable_occurrence_identity: bool = True
    content: bytes = b"first"
    revision: str = "revision-00000001"
    captures: int = 0

    def canonicalize_origin(self, origin_ref: OriginRef) -> OriginRef:
        return origin_ref

    def authorize(self, origin_ref: OriginRef, principal: ActorRef, purpose: str) -> bool:
        return purpose in {"truth_evidence", "recheck"}

    def capture(self, origin_ref: OriginRef, purpose: str) -> NativeCapture:
        self.captures += 1
        return NativeCapture(
            exact_content=self.content,
            media_type="text/plain",
            representation_kind="decoded_text",
            encoding="utf-8",
            source_role="conversation_message",
            fidelity="provider_exact",
            native_revision=self.revision,
            occurred_at="2026-08-09T10:00:00.000+00:00",
            observed_at="2026-08-09T12:00:00.000+00:00",
            authorization_fingerprint="d" * 64,
        )

    def observe(self, origin_ref: OriginRef) -> NativeObservation:
        return NativeObservation(
            kind="origin_unchanged",
            status="ok",
            observed_at="2026-08-09T12:05:00.000+00:00",
            native_revision=self.revision,
        )


def _capture(source_store, provider, registry, human, tenant_id, origin, **kwargs):
    return source_capture_from_origin(
        source_store,
        registry,
        provider_id=provider.provider_id,
        origin_ref=origin,
        principal=human,
        purpose="truth_evidence",
        tenant_scope_id=tenant_id,
        originating_surface="truth",
        **kwargs,
    )


def test_stable_provider_reuses_same_occurrence_revision_and_not_equal_text(
    source_store, human: ActorRef, tenant_id: str
) -> None:
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    origin = OriginRef(
        provider.provider_id,
        "message-00000001",
        container_id="thread-00000001",
    )
    first = _capture(source_store, provider, registry, human, tenant_id, origin)
    again = _capture(source_store, provider, registry, human, tenant_id, origin)
    assert again == first

    other_occurrence = OriginRef(
        provider.provider_id,
        "message-00000002",
        container_id="thread-00000001",
    )
    other = _capture(
        source_store, provider, registry, human, tenant_id, other_occurrence
    )
    assert other != first


def test_stable_provider_identity_mismatch_is_observed_without_rewrite(
    source_store, human: ActorRef, tenant_id: str
) -> None:
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    origin = OriginRef(provider.provider_id, "message-00000003")
    retained = _capture(source_store, provider, registry, human, tenant_id, origin)
    provider.content = b"different bytes under same revision"
    with pytest.raises(SourceOriginMismatch):
        _capture(source_store, provider, registry, human, tenant_id, origin)
    conn = source_store.connect()
    try:
        rows = conn.execute(
            "SELECT observation_kind FROM source_observations WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY observed_at, observation_id",
            (retained.authority_id, retained.item_id),
        ).fetchall()
        assert "identity_mismatch" in [row[0] for row in rows]
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_later_provider_revision_creates_new_item_and_derivation(
    source_store, human: ActorRef, tenant_id: str
) -> None:
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    origin = OriginRef(provider.provider_id, "message-00000004")
    first = _capture(source_store, provider, registry, human, tenant_id, origin)
    provider.content = b"second"
    provider.revision = "revision-00000002"
    second = _capture(source_store, provider, registry, human, tenant_id, origin)
    assert second != first
    conn = source_store.connect()
    try:
        row = conn.execute(
            "SELECT relation, input_authority_id, input_item_id FROM source_derivations "
            "WHERE derived_authority_id = ? AND derived_item_id = ?",
            (second.authority_id, second.item_id),
        ).fetchone()
        assert (row["relation"], row["input_authority_id"], row["input_item_id"]) == (
            "revised_from",
            first.authority_id,
            first.item_id,
        )
    finally:
        conn.close()


def test_unstable_provider_requires_explicit_idempotency_key(
    source_store, human: ActorRef, tenant_id: str
) -> None:
    provider = FakeProvider(provider_id="unstable-provider", stable_occurrence_identity=False)
    registry = ProviderRegistry()
    registry.register(provider)
    origin = OriginRef(provider.provider_id, "opaque-item-00000001")
    with pytest.raises(SourceOriginMismatch):
        _capture(source_store, provider, registry, human, tenant_id, origin)
    first = _capture(
        source_store,
        provider,
        registry,
        human,
        tenant_id,
        origin,
        client_mutation_id="provider-mutation-00000001",
    )
    again = _capture(
        source_store,
        provider,
        registry,
        human,
        tenant_id,
        origin,
        client_mutation_id="provider-mutation-00000001",
    )
    assert again == first


def test_reobservation_appends_current_origin_state(
    source_store, human: ActorRef, tenant_id: str
) -> None:
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    ref = _capture(
        source_store,
        provider,
        registry,
        human,
        tenant_id,
        OriginRef(provider.provider_id, "message-00000005"),
    )
    observation = reobserve_origin(
        source_store, registry, source_ref=ref, principal=human
    )
    assert observation.kind == "origin_unchanged"
