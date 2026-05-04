"""Tests for ``work_buddy.pipelines.chrome.ChromeTriagePipeline``.

The pipeline composes existing Chrome adapters / clusterer. Tests
stub those externals so the pipeline exercises without touching the
ledger, the embedding service, or the Chrome native host.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.pipelines.chrome import (
    CHROME_ACTION_LIBRARY,
    CHROME_ACTIONS,
    ChromeTriagePipeline,
    _captured_from_triage_dict,
    _synthesised_tags,
)
from work_buddy.pipelines.types import CapturedItem


def _triage_dict(
    item_id: str = "tab_1",
    title: str = "GitHub - some/repo",
    url: str = "https://github.com/some/repo",
    domain: str = "github.com",
    group_title: str = "",
    window_id: int = 1,
    index: int = 0,
):
    return {
        "id": item_id,
        "text": f"{title} [{domain}]",
        "label": title,
        "source": "chrome_tab",
        "url": url,
        "metadata": {
            "title": title,
            "domain": domain,
            "tab_id": 12345,
            "window_id": window_id,
            "group_id": -1,
            "group_title": group_title,
            "index": index,
            "engaged_count": 0,
            "score": 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Action library shape
# ---------------------------------------------------------------------------


class TestActionLibrary:
    def test_contains_chrome_specific(self):
        names = {d.capability_name for d in CHROME_ACTION_LIBRARY}
        assert "chrome_tab_close" in names
        assert "chrome_tab_group" in names
        assert "chrome_tab_move" in names
        assert "chrome_route_to_tasks" in names
        assert "chrome_route_to_umbrella_task" in names

    def test_all_per_group(self):
        # All five Chrome actions are per_group cardinality.
        per_group = CHROME_ACTION_LIBRARY.per_group_actions()
        assert len(per_group) == 5

    def test_pipeline_exposes_library(self):
        p = ChromeTriagePipeline()
        assert p.action_library is CHROME_ACTION_LIBRARY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestCapturedFromTriageDict:
    def test_round_trip_essentials(self):
        td = _triage_dict()
        ci = _captured_from_triage_dict(td)
        assert ci.id == "tab_1"
        assert ci.source == "chrome_tab"
        assert ci.type == "tab"
        assert ci.payload["url"] == "https://github.com/some/repo"
        assert ci.payload["domain"] == "github.com"
        assert ci.payload["window_id"] == 1
        # text → summary
        assert "GitHub" in (ci.summary or "")


class TestSynthesisedTags:
    def test_domain_tag(self):
        tags = _synthesised_tags({"domain": "github.com"})
        assert "domain:github.com" in tags

    def test_group_title_becomes_tag(self):
        tags = _synthesised_tags({
            "domain": "github.com", "group_title": "Research",
        })
        assert "chrome_group:Research" in tags

    def test_empty_payload(self):
        assert _synthesised_tags({}) == ()


# ---------------------------------------------------------------------------
# Stage methods
# ---------------------------------------------------------------------------


class TestCollect:
    def test_collect_wraps_chrome_tabs(self):
        chrome_result = {
            "success": True,
            "items": [
                _triage_dict("tab_a"),
                _triage_dict("tab_b", title="Another"),
            ],
            "tab_count": 2,
            "enriched_count": 1,
            "snapshot_time": "2026-04-01T12:00:00Z",
        }
        with patch(
            "work_buddy.clarify.adapters.chrome.chrome_tabs_to_items",
            return_value=chrome_result,
        ):
            p = ChromeTriagePipeline()
            captured = p.collect()
        assert len(captured) == 2
        assert all(isinstance(c, CapturedItem) for c in captured)

    def test_collect_returns_empty_on_failure(self):
        with patch(
            "work_buddy.clarify.adapters.chrome.chrome_tabs_to_items",
            return_value={"success": False, "error": "extension down"},
        ):
            p = ChromeTriagePipeline()
            assert p.collect() == []


class TestAnnotateItems:
    def test_annotate_synthesises_domain_tag(self):
        ci = _captured_from_triage_dict(_triage_dict())
        p = ChromeTriagePipeline()
        out = p.annotate_items([ci])
        assert "domain:github.com" in out[0].tags

    def test_annotate_empty_short_circuits(self):
        p = ChromeTriagePipeline()
        assert p.annotate_items([]) == []


class TestPrecluster:
    def test_precluster_empty_returns_empty(self):
        p = ChromeTriagePipeline()
        assert p.precluster([]) == []

    def test_precluster_falls_back_on_failure(self):
        items = [
            _captured_from_triage_dict(_triage_dict(f"tab_{i}"))
            for i in range(3)
        ]
        with patch(
            "work_buddy.clarify.cluster.cluster_items",
            side_effect=RuntimeError("clusterer broke"),
        ):
            p = ChromeTriagePipeline()
            clusters = p.precluster(items)
        assert len(clusters) == 1
        assert clusters[0].label == "Ungrouped"
        assert len(clusters[0].item_ids) == 3

    def test_precluster_happy_path(self):
        items = [
            _captured_from_triage_dict(_triage_dict(f"tab_{i}"))
            for i in range(2)
        ]

        # Stub the chrome cluster_items helper to return TriageCluster-
        # like objects with the items + label.
        class _FakeTriageCluster:
            def __init__(self, label, items):
                self.label = label
                self.items = items

        def fake_cluster_items(triage_items):
            return [_FakeTriageCluster("Test cluster", triage_items)]

        with patch(
            "work_buddy.clarify.cluster.cluster_items",
            side_effect=fake_cluster_items,
        ):
            p = ChromeTriagePipeline()
            clusters = p.precluster(items)
        assert len(clusters) == 1
        assert clusters[0].label == "Test cluster"
        assert set(clusters[0].item_ids) == {"tab_0", "tab_1"}


class TestUmbrellaSummary:
    def test_with_summary(self):
        p = ChromeTriagePipeline()
        s = p.umbrella_summary({"summary": "Research session"})
        assert s["title"] == "Chrome triage: Research session"
        assert s["source"] == "chrome_triage"

    def test_with_scrape_id(self):
        p = ChromeTriagePipeline()
        s = p.umbrella_summary({"scrape_id": "abc-123"})
        assert "abc-123" in s["title"]

    def test_default(self):
        p = ChromeTriagePipeline()
        s = p.umbrella_summary({})
        assert s["title"] == "Chrome triage"
