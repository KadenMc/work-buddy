"""aggregate_status: one failing index never blanks the panel."""
from __future__ import annotations

from work_buddy.indexing import status as st
from work_buddy.indexing.protocol import IndexStatus, PartitionStatus


class _Good:
    name = "good"

    def status(self):
        return IndexStatus(name="good", partitions=[PartitionStatus("p", 1, 1, 1, 0)])


def test_aggregate_tolerates_a_failing_adapter(monkeypatch):
    def fake_get(name):
        if name == "bad":
            raise RuntimeError("boom")
        return _Good()

    monkeypatch.setattr(st.registry, "index_names", lambda: ["good", "bad"])
    monkeypatch.setattr(st.registry, "get_index", fake_get)

    out = {ix.name: ix for ix in st.aggregate_status()}
    assert out["good"].partitions[0].total_items == 1            # healthy one survives
    assert out["bad"].partitions[0].health == "error"           # failing one is degraded
    assert "boom" in (out["bad"].partitions[0].detail or "")


def test_aggregate_runs_all_registered(monkeypatch):
    monkeypatch.setattr(st.registry, "index_names", lambda: ["good"])
    monkeypatch.setattr(st.registry, "get_index", lambda name: _Good())
    assert [ix.name for ix in st.aggregate_status()] == ["good"]
