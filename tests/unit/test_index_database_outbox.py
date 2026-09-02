"""DB partition registration, build-then-ack replay, and native visibility."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import numpy as np

from work_buddy.index.config import IndexConfig
from work_buddy.index.model import Document, ItemRef, Query
from work_buddy.index.partition import PartitionRegistry
from work_buddy.index.partitioned import UnifiedIndex
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.store import IndexStore


class _UnusedEncoder:
    def encode_query(self, texts, *_args, **_kwargs):
        return np.ones((len(texts), 4), dtype=np.float32)

    def encode_documents(self, texts, *_args, **_kwargs):
        return np.ones((len(texts), 4), dtype=np.float32)


@dataclass(frozen=True)
class _Event:
    event_id: str


class _OutboxPartition:
    name = "native_fixture"
    change_key = "hash"

    def __init__(self) -> None:
        self.items = {"record-1": "first body"}
        self.events = [_Event("event-1")]
        self.acknowledged: list[str] = []
        self.fail_parse = False
        self.append_during_parse = False
        self.mutate_during_parse = False
        self.ack_probe = None

    def field_weights(self):
        return {}

    def projection_schema(self):
        return {}

    def discover(self):
        return [ItemRef(item_id=key, content_hash=value) for key, value in self.items.items()]

    def parse(self, item_id):
        if self.fail_parse:
            raise RuntimeError("synthetic index failure")
        if self.append_during_parse:
            self.append_during_parse = False
            self.events.append(_Event("event-2"))
        value = self.items[item_id]
        if self.mutate_during_parse:
            self.mutate_during_parse = False
            self.items[item_id] = "second body"
            self.events.append(_Event("event-2"))
        return [
            Document(
                doc_id=f"native_fixture:{item_id}",
                partition=self.name,
                fields={"title": item_id, "body": value},
            )
        ]

    def pending_search_events(self, *, limit=1000):
        return list(self.events[:limit])

    def pending_search_event_count(self):
        return len(self.events)

    def acknowledge_search_events(self, events):
        if self.ack_probe is not None:
            self.ack_probe()
        ids = {event.event_id for event in events}
        self.acknowledged.extend(sorted(ids))
        self.events = [event for event in self.events if event.event_id not in ids]


def _unified(tmp_path, partition):
    registry = PartitionRegistry()
    registry.register(partition.name, lambda: partition)
    return UnifiedIndex(
        store=IndexStore(tmp_path / "index.db"),
        encoder=_UnusedEncoder(),
        config=IndexConfig(enabled=True),
        residents=ResidentCacheRegistry(),
        registry=registry,
    )


def test_build_then_ack_snapshots_events_and_replays_concurrent_lag(tmp_path):
    from work_buddy.utils.index_lock import is_locked

    partition = _OutboxPartition()
    partition.append_during_parse = True
    index = _unified(tmp_path, partition)
    lock_observations = []
    partition.ack_probe = lambda: lock_observations.append(
        (
            is_locked(tmp_path / "index.db.build"),
            is_locked(tmp_path / "index.db.native_fixture"),
        )
    )

    first = index.build(partition.name)
    assert first["outbox"] == {
        "schema": "wb.search-outbox-delivery/v1",
        "pending_before": 1,
        "delivered": 1,
        "pending_after": 1,
        "mode": "incremental_replay",
        "source_items": 1,
        "indexed_items": 1,
        "parity_mismatches": 0,
        "ready": False,
    }
    assert partition.acknowledged == ["event-1"]
    assert lock_observations == [(True, True)]

    replay = index.build(partition.name)
    assert replay["changed"] == 0
    assert replay["outbox"]["delivered"] == 1
    assert replay["outbox"]["pending_after"] == 0
    assert replay["outbox"]["ready"] is True
    assert partition.acknowledged == ["event-1", "event-2"]
    assert lock_observations == [(True, True), (True, True)]


def test_failed_build_never_acknowledges_and_force_is_partition_backfill(tmp_path):
    partition = _OutboxPartition()
    partition.fail_parse = True
    index = _unified(tmp_path, partition)

    with pytest.raises(RuntimeError, match="synthetic index failure"):
        index.build(partition.name)
    assert partition.events == [_Event("event-1")]
    assert partition.acknowledged == []

    partition.fail_parse = False
    recovered = index.build(partition.name, force=True)
    assert recovered["changed"] == 1
    assert recovered["outbox"]["mode"] == "backfill"
    assert recovered["outbox"]["pending_after"] == 0
    assert recovered["outbox"]["ready"] is True
    assert index.search(
        Query(text="first", method="lexical"), partitions=[partition.name]
    )


def test_parity_mismatch_retains_entire_snapshot_until_replay(tmp_path):
    partition = _OutboxPartition()
    partition.mutate_during_parse = True
    index = _unified(tmp_path, partition)

    raced = index.build(partition.name)
    assert raced["outbox"]["parity_mismatches"] == 1
    assert raced["outbox"]["delivered"] == 0
    assert raced["outbox"]["pending_after"] == 2
    assert raced["outbox"]["ready"] is False
    assert partition.acknowledged == []

    replayed = index.build(partition.name)
    assert replayed["changed"] == 1
    assert replayed["outbox"]["parity_mismatches"] == 0
    assert replayed["outbox"]["delivered"] == 2
    assert replayed["outbox"]["ready"] is True
    assert partition.acknowledged == ["event-1", "event-2"]


def test_contract_and_personal_partitions_register_with_bootstrap():
    from work_buddy.index.partition import get_partition_registry
    from work_buddy.index.partitions.bootstrap import ensure_partitions_registered

    ensure_partitions_registered()
    names = get_partition_registry().names()
    assert "contracts" in names
    assert "personal_knowledge" in names
    assert "journal" in names
    assert "projects" in names


def test_contract_partition_publishes_and_delivers_after_native_seal(tmp_path):
    from work_buddy.contracts_domain.partition import ContractsPartition
    from work_buddy.contracts_domain.service import ContractService
    from work_buddy.contracts_domain.store import ContractStore

    store = ContractStore.create(tmp_path / "contracts.db")
    service = ContractService(store)
    created = service.create(
        {
            "title": "Release brief",
            "status": "draft",
            "type": "delivery",
            "privacy_class": "private",
            "aliases": [{"alias": "release-brief", "kind": "logical_name"}],
            "body_roles": [
                {
                    "role": "brief",
                    "mode": "plain",
                    "plain_body": "Verify the bounded release path.",
                    "interaction_contract_id": "human_value",
                    "interaction_contract_version": 1,
                    "privacy_class": "private",
                }
            ],
        },
        actor="user:test",
        intent_id="contract-search-create",
    )
    partition = ContractsPartition(store)
    assert list(partition.discover()) == []

    with store.write_transaction() as connection:
        connection.execute(
            "UPDATE contract_authority SET state='native',authority_epoch=1,"
            "sealed_cohort_id='cohort',coordinator_decision_id='decision',"
            "coordinator_decision_sha256=?,sealed_at=? WHERE singleton=1",
            ("a" * 64, "2026-08-27T00:00:00+00:00"),
        )

    index = _unified(tmp_path / "contract-index", partition)
    result = index.build("contracts")
    assert result["outbox"]["delivered"] == 1
    assert result["outbox"]["ready"] is True
    assert result["doc_count"] == 1
    hits = index.search(
        Query(text="bounded release", method="lexical"), partitions=["contracts"]
    )
    assert hits[0].metadata["contractId"] == created["contract_id"]
    assert service.pending_search_events() == []


def test_personal_partition_publishes_and_delivers_after_sqlite_seal(tmp_path):
    from work_buddy.knowledge.personal.partition import PersonalKnowledgePartition
    from work_buddy.knowledge.personal.store import PersonalKnowledgeStore

    store = PersonalKnowledgeStore(tmp_path / "personal.db")
    created = store.create_unit(
        logical_path="personal/preferences/response-shape",
        name="Response shape",
        description="A neutral formatting preference.",
        summary="Prefer a compact summary before detail.",
        body="# Response shape\n\nUse a compact summary.",
        categories=["preference"],
        aliases=["compact summary"],
        tags=["formatting"],
        idempotency_key="personal-search-create",
    )
    partition = PersonalKnowledgePartition(store)
    assert list(partition.discover()) == []

    connection = store.connect()
    try:
        connection.execute(
            "UPDATE personal_knowledge_authority SET authority='sqlite',"
            "authority_epoch=2,sealed_cohort_id='cohort',sealed_at=?,updated_at=? "
            "WHERE singleton=1",
            ("2026-08-27T00:00:00Z", "2026-08-27T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()

    index = _unified(tmp_path / "personal-index", partition)
    result = index.build("personal_knowledge")
    assert result["outbox"]["delivered"] == 1
    assert result["outbox"]["ready"] is True
    assert result["doc_count"] == 1
    hits = index.search(
        Query(text="compact summary", method="lexical"),
        partitions=["personal_knowledge"],
    )
    assert hits[0].metadata["unitId"] == created["unit_id"]
    assert store.pending_outbox() == []
