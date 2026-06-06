"""The indexing-seam value types + protocol conformance."""
from __future__ import annotations

import dataclasses

from work_buddy.indexing.protocol import (
    Index,
    IndexStatus,
    PartitionStatus,
)


def test_partition_status_defaults_and_frozen():
    p = PartitionStatus(key="v", total_items=10, dense_eligible=10, vector_count=8, pending=2)
    assert p.health == "ok" and p.last_build is None and p.detail is None
    try:
        p.health = "error"  # frozen
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("PartitionStatus should be frozen")


def test_index_status_asdict_is_json_friendly():
    s = IndexStatus(
        name="x",
        partitions=[PartitionStatus("p", 1, 1, 1, 0)],
        size_on_disk_mb=2.5,
    )
    d = dataclasses.asdict(s)
    assert d["name"] == "x"
    assert d["partitions"][0]["key"] == "p"  # nested dataclass flattened
    assert d["size_on_disk_mb"] == 2.5


def test_adapters_conform_to_protocol():
    # runtime_checkable: presence of name/status/lock_key/bulk_build is enough.
    from work_buddy.indexing.adapters.ir import IRIndexAdapter
    from work_buddy.indexing.adapters.knowledge import KnowledgeIndexAdapter
    from work_buddy.indexing.adapters.vault import VaultIndexAdapter

    for adapter in (IRIndexAdapter(), VaultIndexAdapter(), KnowledgeIndexAdapter()):
        assert isinstance(adapter, Index)
