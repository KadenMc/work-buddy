"""Tests for index/partitioned.py (UnifiedIndex) + the consolidated Index seam adapter."""

from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.config import IndexConfig
from work_buddy.index.model import (
    Document,
    ItemRef,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    Query,
)
from work_buddy.index.partition import PartitionRegistry
from work_buddy.index.partitioned import (
    UnifiedIndex,
    prewarm_resident_matrices,
    start_prewarm,
)
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.store import IndexStore


class FakeEncoder:
    def encode_query(self, texts, kind, model_key=None):
        return np.ones((len(texts), 4), dtype=np.float32)

    def encode_documents(self, texts, kind, model_key=None):
        return np.ones((len(texts), 4), dtype=np.float32)


class FakePartition:
    """Lexical-only partition (no projections → encoder unused)."""

    def __init__(self, name, items):
        self.name = name
        self.change_key = "hash"
        self._items = items  # {item_id: [(suffix, name_text)]}

    def field_weights(self):
        return {}

    def projection_schema(self):
        return {}

    def discover(self):
        return [ItemRef(item_id=i, content_hash=i) for i in self._items]

    def parse(self, item_id):
        return [
            Document(doc_id=f"{self.name}:{s}", partition=self.name,
                     fields={"name": n, "body": n}, display_text=n)
            for s, n in self._items[item_id]
        ]


@pytest.fixture
def unified(tmp_path):
    reg = PartitionRegistry()
    reg.register("p1", lambda: FakePartition("p1", {"i1": [("a", "alpha one")]}))
    reg.register("p2", lambda: FakePartition("p2", {"i2": [("b", "alpha two")]}))
    return UnifiedIndex(
        store=IndexStore(tmp_path / "uni.db"),
        encoder=FakeEncoder(),
        config=IndexConfig(enabled=True),
        residents=ResidentCacheRegistry(),
        registry=reg,
    )


class TestUnifiedIndex:
    def test_build_and_search_single_partition(self, unified):
        unified.build("p1")
        hits = unified.search(Query(text="alpha", top_k=5), partitions=["p1"])
        assert [h.doc_id for h in hits] == ["p1:a"]

    def test_federated_search_across_partitions(self, unified):
        unified.build("p1")
        unified.build("p2")
        hits = unified.search(Query(text="alpha", top_k=5))  # default: all built partitions
        assert {h.doc_id for h in hits} == {"p1:a", "p2:b"}

    def test_available_lists_registered(self, unified):
        assert set(unified.available()) == {"p1", "p2"}

    def test_hydrate_default_passthrough(self, unified):
        unified.build("p1")
        hits = unified.search(Query(text="alpha"), partitions=["p1"])
        # FakePartition has no hydrate() → passthrough returns the hits
        assert unified.hydrate("p1", hits) == hits

    def test_status_reports_partitions(self, unified):
        unified.build("p1")
        unified.build("p2")
        st = unified.status()
        assert st.name == "consolidated"
        keys = {p.key for p in st.partitions}
        assert keys == {"p1", "p2"}
        for p in st.partitions:
            assert p.total_items == 1

    def test_search_empty_when_nothing_built(self, unified):
        assert unified.search(Query(text="alpha")) == []

    def test_search_many_federated_and_ordered(self, unified):
        unified.build("p1")
        unified.build("p2")
        out = unified.search_many(["alpha", "zzznomatch"], top_k=5)
        assert len(out) == 2  # one list per query, in order
        assert {h.doc_id for h in out[0]} == {"p1:a", "p2:b"}  # "alpha" federates both
        assert out[1] == []  # no lexical match → empty, position preserved

    def test_search_many_partition_filter(self, unified):
        unified.build("p1")
        unified.build("p2")
        out = unified.search_many(["alpha"], partitions=["p1"], top_k=5)
        assert [h.doc_id for h in out[0]] == ["p1:a"]  # p2 excluded


class TestConsolidatedAdapter:
    def test_registered_in_seam(self):
        from work_buddy.indexing.registry import get_index, index_names
        assert "consolidated" in index_names()
        adapter = get_index("consolidated")
        assert adapter.name == "consolidated"

    def test_bulk_build_skips_when_disabled(self, monkeypatch):
        from work_buddy.index.config import IndexConfig
        from work_buddy.indexing.adapters.index_consolidated import ConsolidatedIndexAdapter
        # Pin the flag OFF. bulk_build reads the MACHINE config — unpinned, a machine
        # with index.enabled=true would really build every partition of the real DB
        # from inside this unit test (multi-hour at vault scale).
        monkeypatch.setattr(
            "work_buddy.index.config.load_index_config",
            lambda *a, **k: IndexConfig(enabled=False),
        )
        res = ConsolidatedIndexAdapter().bulk_build()
        assert res.ok is True
        assert "skipped" in res.stats

    def test_status_is_safe(self):
        from work_buddy.indexing.adapters.index_consolidated import ConsolidatedIndexAdapter
        st = ConsolidatedIndexAdapter().status()
        assert st.name == "consolidated"  # never raises, even pre-build


class ProjectedPartition:
    """Partition with one dense projection, so a build writes vectors and prewarm
    has a resident matrix to load (the lexical-only FakePartition has none)."""

    def __init__(self, name, items):
        self.name = name
        self.change_key = "hash"
        self._items = items  # {item_id: [(suffix, text)]}

    def field_weights(self):
        return {}

    def projection_schema(self):
        return {"default": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def discover(self):
        return [ItemRef(item_id=i, content_hash=i) for i in self._items]

    def parse(self, item_id):
        return [
            Document(
                doc_id=f"{self.name}:{s}", partition=self.name,
                fields={"name": t, "body": t}, display_text=t,
                projections={"default": Projection(text=t)},
            )
            for s, t in self._items[item_id]
        ]


class CountingStore(IndexStore):
    """IndexStore that records load_all_vectors calls (the resident-cache loader)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.load_calls = 0
        self.load_order: list[str] = []

    def load_all_vectors(self, partition, projection):
        self.load_calls += 1
        self.load_order.append(partition)
        return super().load_all_vectors(partition, projection)


class TestPrewarm:
    def _unified(self, tmp_path, store=None):
        reg = PartitionRegistry()
        reg.register("p1", lambda: ProjectedPartition("p1", {"i1": [("a", "alpha one")]}))
        reg.register("p2", lambda: ProjectedPartition("p2", {"i2": [("b", "alpha two")]}))
        return UnifiedIndex(
            store=store or IndexStore(tmp_path / "uni.db"),
            encoder=FakeEncoder(),
            config=IndexConfig(enabled=True),
            residents=ResidentCacheRegistry(),
            registry=reg,
        )

    def test_loads_each_built_partition_matrix_once(self, tmp_path):
        store = CountingStore(tmp_path / "uni.db")
        ui = self._unified(tmp_path, store=store)
        ui.build("p1")
        ui.build("p2")
        store.load_calls = 0  # builds don't load the matrix; reset to be exact

        warmed = prewarm_resident_matrices(
            IndexConfig(enabled=True), index_factory=lambda cfg: ui
        )

        # One projection warmed per built partition; loader invoked once per partition.
        assert warmed == {"p1": 1, "p2": 1}
        assert store.load_calls == 2
        assert ui._residents.get("p1:default").is_cached()
        assert ui._residents.get("p2:default").is_cached()

    def test_is_idempotent_served_from_ram(self, tmp_path):
        store = CountingStore(tmp_path / "uni.db")
        ui = self._unified(tmp_path, store=store)
        ui.build("p1")
        store.load_calls = 0

        prewarm_resident_matrices(IndexConfig(enabled=True), index_factory=lambda cfg: ui)
        prewarm_resident_matrices(IndexConfig(enabled=True), index_factory=lambda cfg: ui)

        # Second pass serves the cached matrix (version unchanged) — no reload.
        assert store.load_calls == 1

    def test_gated_off_when_disabled(self):
        called = {"n": 0}

        def factory(cfg):
            called["n"] += 1
            raise AssertionError("factory must not be built when index is disabled")

        warmed = prewarm_resident_matrices(
            IndexConfig(enabled=False), index_factory=factory
        )
        assert warmed == {}
        assert called["n"] == 0

    def test_warms_largest_partition_first(self, tmp_path):
        reg = PartitionRegistry()
        reg.register("small", lambda: ProjectedPartition("small", {"i1": [("a", "x one")]}))
        reg.register("big", lambda: ProjectedPartition(
            "big", {"i1": [("a", "x one")], "i2": [("b", "y two"), ("c", "z three")]}))
        store = CountingStore(tmp_path / "uni.db")
        ui = UnifiedIndex(
            store=store, encoder=FakeEncoder(), config=IndexConfig(enabled=True),
            residents=ResidentCacheRegistry(), registry=reg,
        )
        ui.build("small")  # 1 doc
        ui.build("big")    # 3 docs
        store.load_order.clear()

        prewarm_resident_matrices(IndexConfig(enabled=True), index_factory=lambda cfg: ui)

        # The bigger partition (slowest to load) is warmed before the smaller one.
        assert store.load_order == ["big", "small"]

    def test_skips_when_no_partition_built(self, tmp_path):
        ui = self._unified(tmp_path)  # registered but nothing built
        warmed = prewarm_resident_matrices(
            IndexConfig(enabled=True), index_factory=lambda cfg: ui
        )
        assert warmed == {}

    def test_one_failing_partition_does_not_abort_the_rest(self, tmp_path):
        store = CountingStore(tmp_path / "uni.db")
        ui = self._unified(tmp_path, store=store)
        ui.build("p1")
        ui.build("p2")

        real_partition = ui.partition

        def flaky_partition(name):
            if name == "p1":
                raise RuntimeError("p1 boom")
            return real_partition(name)

        ui.partition = flaky_partition  # p1 raises; p2 must still warm
        warmed = prewarm_resident_matrices(
            IndexConfig(enabled=True), index_factory=lambda cfg: ui
        )
        assert "p1" not in warmed
        assert warmed.get("p2") == 1

    def test_start_prewarm_runs_in_background_thread(self):
        # Disabled config → the thread runs prewarm (a no-op) and exits cleanly.
        t = start_prewarm(config=IndexConfig(enabled=False))
        t.join(timeout=5)
        assert not t.is_alive()
        assert t.daemon
