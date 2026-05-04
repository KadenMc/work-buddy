"""Tests for the Stage 5 grouped Chrome-scrape spawn path.

The legacy single-decompose-parent shape is preserved when callers
opt out via ``use_grouping=False`` or when no clusters are supplied.
The new grouped shape spawns one group-parent per cluster; tabs not
referenced fall into a synthetic "Ungrouped" sibling.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import models, source_pipelines, store
from work_buddy.threads.enums import FSMState


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _tab(tid: str, title: str = "", url: str = "") -> dict:
    return {
        "id": tid,
        "title": title or f"tab {tid}",
        "url": url or f"https://example/{tid}",
        "window_id": 1,
        "tab_index": int(tid[-1]) if tid[-1].isdigit() else 0,
    }


# ---------------------------------------------------------------------------
# Grouped path (the new Stage 5 default)
# ---------------------------------------------------------------------------


class TestGroupedSpawn:
    def test_spawns_one_parent_per_cluster(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 5)]
        clusters = [
            {"label": "Code", "tab_ids": ["t1", "t2"]},
            {"label": "Research", "tab_ids": ["t3", "t4"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out is not None
        assert "parents" in out
        assert out["parent_count"] == 2  # no leftovers, no Ungrouped
        labels = [p["label"] for p in out["parents"]]
        assert labels == ["Code", "Research"]
        assert out["total_count"] == 4

    def test_all_parents_share_originating_scrape_id(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 4)]
        clusters = [
            {"label": "A", "tab_ids": ["t1"]},
            {"label": "B", "tab_ids": ["t2", "t3"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out is not None
        scope = out["originating_scrape_id"]
        assert scope
        for p in out["parents"]:
            parent = store.get_thread(p["parent_id"])
            assert parent.originating_scrape_id == scope
            assert parent.parent_relationship == "group"
            assert parent.fsm_state == FSMState.MONITORING

    def test_unassigned_tabs_become_ungrouped_sibling(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 5)]
        clusters = [
            {"label": "Just-one", "tab_ids": ["t1"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out is not None
        assert out["parent_count"] == 2
        labels = [p["label"] for p in out["parents"]]
        assert "Just-one" in labels
        assert "Ungrouped" in labels
        ungrouped = next(
            p for p in out["parents"] if p["label"] == "Ungrouped"
        )
        assert ungrouped["count"] == 3

    def test_explicit_scrape_id_carried_through(self, fresh_db):
        tabs = [_tab("t1"), _tab("t2")]
        clusters = [{"label": "X", "tab_ids": ["t1", "t2"]}]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs,
            scrape_id="my-scrape-id",
            clusters=clusters,
        )
        # When the caller supplies scrape_id we use it verbatim as
        # the sibling-scope id.
        assert out["originating_scrape_id"] == "my-scrape-id"

    def test_cluster_metadata_in_inciting_summary(self, fresh_db):
        tabs = [_tab("t1"), _tab("t2"), _tab("t3")]
        clusters = [
            {"label": "Code", "tab_ids": ["t1", "t2"]},
            {"label": "Misc", "tab_ids": ["t3"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        for p in out["parents"]:
            parent = store.get_thread(p["parent_id"])
            inciting = parent.inciting_event_summary or {}
            assert inciting["title"] == p["label"]
            assert inciting["cluster_index"] == p["cluster_index"]
            assert inciting["cluster_size"] == 2  # two real clusters

    def test_empty_tabs_returns_none(self, fresh_db):
        out = source_pipelines.spawn_threads_from_chrome_scrape(tabs=[])
        assert out is None


# ---------------------------------------------------------------------------
# Legacy path — opt-out via use_grouping=False or no clusters
# ---------------------------------------------------------------------------


class TestLegacyShape:
    def test_use_grouping_false_falls_back_to_decompose_parent(self, fresh_db):
        tabs = [_tab("t1"), _tab("t2")]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, use_grouping=False,
        )
        assert out is not None
        # Legacy shape: parent_id, sub_thread_ids, count
        assert "parent_id" in out
        assert "parents" not in out
        # And the parent is decompose, not group.
        parent = store.get_thread(out["parent_id"])
        assert parent.parent_relationship == "decompose"

    def test_no_clusters_falls_back_to_decompose_parent(self, fresh_db):
        tabs = [_tab("t1")]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=None,
        )
        assert "parent_id" in out
        assert "parents" not in out

    def test_empty_clusters_falls_back_to_decompose_parent(self, fresh_db):
        tabs = [_tab("t1")]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=[],
        )
        assert "parent_id" in out
