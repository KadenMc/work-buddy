from __future__ import annotations

import pytest

from work_buddy.sources import (
    ActorRef,
    InvalidSourceReference,
    OriginRef,
    SourceRef,
)


def test_source_ref_round_trips_authority_qualified_uri() -> None:
    ref = SourceRef("authority-00000001", "item-000000000001")
    assert ref.uri == "wb-source://authority-00000001/item/item-000000000001"
    assert SourceRef.parse(ref.uri) == ref
    assert SourceRef.from_dict(ref.to_dict()) == ref


@pytest.mark.parametrize(
    "value",
    [
        "wb-source://authority-00000001/item/../../secret",
        "wb-source://authority-00000001@evil/item/item-000000000001",
        "wb-source://authority-00000001:80/item/item-000000000001",
        "wb-source://authority-00000001/item/item-000000000001?raw=secret",
        "WB-SOURCE://authority-00000001/item/item-000000000001",
        "wb-source://authority-00000001/item/%2e%2e",
    ],
)
def test_source_ref_rejects_hostile_or_noncanonical_uris(value: str) -> None:
    with pytest.raises(InvalidSourceReference):
        SourceRef.parse(value)


def test_actor_identity_is_issuer_and_tenant_qualified() -> None:
    left = ActorRef("authority-00000001", "profile-00000001", "human", "tenant-00000001")
    other_issuer = ActorRef(
        "authority-00000002", "profile-00000001", "human", "tenant-00000001"
    )
    other_tenant = ActorRef(
        "authority-00000001", "profile-00000001", "human", "tenant-00000002"
    )
    assert len({left.canonical_id, other_issuer.canonical_id, other_tenant.canonical_id}) == 3


def test_origin_ref_keeps_provider_and_occurrence_separate_from_source_ref() -> None:
    first = OriginRef("chat-a", "message-00000001", container_id="thread-00000001")
    second = OriginRef("chat-b", "message-00000001", container_id="thread-00000001")
    revised = OriginRef(
        "chat-a",
        "message-00000001",
        container_id="thread-00000001",
        revision="revision-00000002",
    )
    assert first.occurrence_key != second.occurrence_key
    assert first.occurrence_key == revised.occurrence_key
