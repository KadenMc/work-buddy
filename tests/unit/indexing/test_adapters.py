"""Adapters map each backend's native status onto the shared shape.

Each backend's status fn is monkeypatched to a canned shape so these tests verify
the *mapping*, not the live indexes.
"""
from __future__ import annotations


def test_ir_adapter_maps_sources_to_partitions(monkeypatch):
    from work_buddy.indexing.adapters import ir as ir_adapter

    canned = {
        "status": "ok",
        "total_docs": 100,
        "sources": {
            "conversation": {"doc_count": 80, "dense_eligible_docs": 40, "last_build": "2026-01-01T00:00:00"},
            "docs": {"doc_count": 20, "dense_eligible_docs": 20, "last_build": None},
        },
        "vectors": {
            "conversation": {"vector_count": 35, "pending_eligible": 5, "vector_file_mb": 1.2},
            "docs": {"vector_count": 20, "pending_eligible": 0, "vector_file_mb": 0.8},
        },
    }
    monkeypatch.setattr("work_buddy.ir.store.index_status", lambda: canned)
    st = ir_adapter.IRIndexAdapter().status()

    assert st.name == "ir"
    parts = {p.key: p for p in st.partitions}
    assert parts["conversation"].total_items == 80
    assert parts["conversation"].vector_count == 35
    assert parts["conversation"].pending == 5
    assert parts["docs"].pending == 0
    assert round(st.size_on_disk_mb, 1) == 2.0  # 1.2 + 0.8


def test_ir_adapter_no_index(monkeypatch):
    from work_buddy.indexing.adapters import ir as ir_adapter
    monkeypatch.setattr("work_buddy.ir.store.index_status", lambda: {"status": "no_index"})
    st = ir_adapter.IRIndexAdapter().status()
    assert st.partitions == []


def test_vault_adapter_maps_vaults(monkeypatch):
    from work_buddy.indexing.adapters import vault as vault_adapter

    canned = {
        "status": "ok",
        "size_on_disk_mb": 411.9,
        "last_build": "2026-06-05T14:50:05",
        "vaults": {
            "A": {"chunk_count": 100, "vector_count": 100, "pending": 0, "in_config": True, "health": "ok"},
            "B": {"chunk_count": 50, "vector_count": 30, "pending": 20, "in_config": False, "health": "unreachable"},
        },
    }
    monkeypatch.setattr("work_buddy.vault_index.status.index_status", lambda: canned)
    st = vault_adapter.VaultIndexAdapter().status()

    assert st.name == "vault_index"
    assert st.size_on_disk_mb == 411.9  # index-level (shared DB)
    parts = {p.key: p for p in st.partitions}
    assert parts["A"].dense_eligible == 100  # vector_count + pending
    assert parts["A"].size_on_disk_mb is None  # per-partition size not separable
    assert parts["B"].pending == 20
    assert parts["B"].health == "unreachable"
    assert parts["B"].detail == "not in config"


def test_knowledge_adapter_single_partition(monkeypatch):
    from work_buddy.indexing.adapters import knowledge as kn_adapter

    monkeypatch.setattr(kn_adapter, "_unit_count", lambda: 220)
    monkeypatch.setattr(kn_adapter, "_cached_vector_count", lambda: 218)
    monkeypatch.setattr(kn_adapter, "_content_cache_mtime_iso", lambda: "2026-06-05T13:38:17")
    st = kn_adapter.KnowledgeIndexAdapter().status()

    assert st.name == "knowledge"
    assert len(st.partitions) == 1
    p = st.partitions[0]
    assert p.total_items == 220 and p.vector_count == 218 and p.pending == 2


def test_knowledge_adapter_cache_failure_degrades(monkeypatch):
    from work_buddy.indexing.adapters import knowledge as kn_adapter

    monkeypatch.setattr(kn_adapter, "_unit_count", lambda: 220)

    def boom():
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr(kn_adapter, "_cached_vector_count", boom)
    monkeypatch.setattr(kn_adapter, "_content_cache_mtime_iso", lambda: None)
    st = kn_adapter.KnowledgeIndexAdapter().status()
    assert st.partitions[0].vector_count == 0  # degraded, not crashed
