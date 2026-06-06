"""C — the /api/embeddings data function: System (IR+knowledge) + User (vaults)."""
from __future__ import annotations

from work_buddy.dashboard import api
from work_buddy.indexing.protocol import IndexStatus, PartitionStatus


def setup_function():
    api._embeddings_cache = None
    api._embeddings_cache_ts = 0.0


def _patch(monkeypatch, *, statuses, vaults, db_mb=400.0, locked=False):
    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", lambda: statuses)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: locked)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs", lambda: vaults)
    monkeypatch.setattr(
        "work_buddy.vault_index.status.index_status",
        lambda: {"status": "ok", "size_on_disk_mb": db_mb},
    )


def test_system_is_ir_and_knowledge_only(monkeypatch):
    _patch(
        monkeypatch,
        statuses=[
            IndexStatus(name="ir",
                        partitions=[PartitionStatus("conversation", 10, 8, 8, 0, size_on_disk_mb=1.2)],
                        size_on_disk_mb=1.2),
            IndexStatus(name="knowledge", partitions=[PartitionStatus("content", 5, 5, 5, 0)]),
            IndexStatus(name="vault_index",
                        partitions=[PartitionStatus("vault", 100, 100, 100, 0)], size_on_disk_mb=400.0),
        ],
        vaults=[{"id": "vault", "path": "/v", "is_default": True, "in_config": True,
                 "chunk_count": 100, "vector_count": 100, "pending": 0, "file_count": 5,
                 "health": "ok", "last_build": None}],
        locked=True,
    )
    out = api._build_embeddings_summary()
    assert {r["index"] for r in out["system"]} == {"ir", "knowledge"}  # vault_index excluded
    assert out["db_size_mb"] == 400.0
    assert out["vaults"][0]["building"] is True  # lock held → building surfaced on vault rows
    # IR partition size flows through; no fallback to index total for size-less rows
    conv = next(r for r in out["system"] if r["source"] == "conversation")
    assert conv["size_mb"] == 1.2


def test_caches_between_calls(monkeypatch):
    calls = {"n": 0}

    def agg():
        calls["n"] += 1
        return []

    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", agg)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: False)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs", lambda: [])
    monkeypatch.setattr("work_buddy.vault_index.status.index_status",
                        lambda: {"status": "ok", "size_on_disk_mb": 1})
    a = api.get_embeddings_summary()
    b = api.get_embeddings_summary()
    assert a == b and calls["n"] == 1  # second call served from cache


def test_degrades_per_section(monkeypatch):
    def boom():
        raise RuntimeError("agg fail")

    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", boom)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: False)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs",
                        lambda: [{"id": "v", "path": "/v"}])
    monkeypatch.setattr("work_buddy.vault_index.status.index_status",
                        lambda: {"status": "ok", "size_on_disk_mb": 1})
    out = api._build_embeddings_summary()
    assert "system_error" in out          # system failed...
    assert out["vaults"] and out["system"] == []  # ...but vaults still populated
