"""Tests for the Stage 5 v2 grouped Chrome-scrape spawn path.

v2 model: one umbrella thread per scrape (parent_relationship='group')
+ N child sub-threads (one per cluster), with each child holding its
cluster's tabs as ``context_items``. Items move between siblings via
``threads.group.move_item``.

The legacy single-decompose-parent shape is preserved when callers
opt out via ``use_grouping=False`` or when no clusters are supplied.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import source_pipelines, store


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
# v2 grouped path: umbrella + N group children
# ---------------------------------------------------------------------------


class TestGroupedSpawn:
    def test_spawns_one_umbrella_with_one_child_per_cluster(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 5)]
        clusters = [
            {"label": "Code", "item_ids": ["t1", "t2"]},
            {"label": "Research", "item_ids": ["t3", "t4"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out is not None
        assert out["umbrella_id"]
        assert out["child_count"] == 2  # no leftovers, no Ungrouped
        assert out["total_count"] == 4
        # Umbrella is parent_relationship='group'
        umbrella = store.get_thread(out["umbrella_id"])
        assert umbrella.parent_relationship == "group"

    def test_children_hold_tabs_as_context_items(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 4)]
        clusters = [
            {"label": "A", "item_ids": ["t1"]},
            {"label": "B", "item_ids": ["t2", "t3"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        children = store.list_threads(parent_id=out["umbrella_id"])
        # Each child carries its cluster's tabs as ContextItems
        # (no per-tab sub-threads; tabs are not threads).
        labels_to_items = {
            c.inciting_event_summary.get("cluster_label"):
            sorted(it.id for it in c.context_items)
            for c in children
        }
        assert labels_to_items["A"] == ["t1"]
        assert labels_to_items["B"] == ["t2", "t3"]

    def test_unassigned_tabs_become_ungrouped_child(self, fresh_db):
        tabs = [_tab(f"t{i}") for i in range(1, 5)]
        clusters = [
            {"label": "Just-one", "item_ids": ["t1"]},
        ]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out["child_count"] == 2  # Just-one + Ungrouped
        children = store.list_threads(parent_id=out["umbrella_id"])
        labels = [
            c.inciting_event_summary.get("cluster_label") for c in children
        ]
        assert "Just-one" in labels
        assert "Ungrouped" in labels
        ungrouped = next(
            c for c in children
            if c.inciting_event_summary.get("cluster_label") == "Ungrouped"
        )
        # Three leftover tabs t2, t3, t4
        assert len(ungrouped.context_items) == 3

    def test_legacy_tab_ids_key_still_accepted(self, fresh_db):
        # Backward compatibility: the older Chrome-clustering output
        # used "tab_ids" instead of "item_ids". group_thread accepts
        # both so existing callers don't break.
        tabs = [_tab("t1"), _tab("t2")]
        clusters = [{"label": "Legacy", "tab_ids": ["t1", "t2"]}]
        out = source_pipelines.spawn_threads_from_chrome_scrape(
            tabs=tabs, clusters=clusters,
        )
        assert out["child_count"] == 1
        assert out["total_count"] == 2

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
