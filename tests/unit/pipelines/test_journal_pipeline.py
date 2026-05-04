"""Tests for ``work_buddy.pipelines.journal.JournalBacklogPipeline``.

The pipeline composes existing journal collectors / manifest /
clustering primitives. Tests stub those externals so the pipeline
itself can be exercised without touching the LLM, embedding service,
or Obsidian vault.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.pipelines.journal import (
    JOURNAL_ACTION_LIBRARY,
    JOURNAL_ACTIONS,
    JournalBacklogPipeline,
)
from work_buddy.pipelines.types import CapturedItem


# ---------------------------------------------------------------------------
# Action library
# ---------------------------------------------------------------------------


class TestActionLibrary:
    def test_library_contains_per_group_routes(self):
        names = {d.capability_name for d in JOURNAL_ACTION_LIBRARY}
        assert "journal_route_to_tasks" in names
        assert "journal_route_to_considerations" in names
        assert "journal_append_to_note" in names
        assert "journal_rewrite_running_notes" in names

    def test_per_group_actions_count(self):
        per_group = JOURNAL_ACTION_LIBRARY.per_group_actions()
        # tasks + considerations + append-to-note are per_group
        assert len(per_group) == 3

    def test_umbrella_actions_count(self):
        umbrella = JOURNAL_ACTION_LIBRARY.umbrella_actions()
        # rewrite_running_notes is the umbrella-level cleanup
        assert len(umbrella) == 1
        assert umbrella[0].capability_name == "journal_rewrite_running_notes"

    def test_pipeline_exposes_library(self):
        p = JournalBacklogPipeline()
        assert p.action_library is JOURNAL_ACTION_LIBRARY


# ---------------------------------------------------------------------------
# Stage methods
# ---------------------------------------------------------------------------


class _FakeTriageItem:
    def __init__(self, item_id, text, label, metadata=None):
        self.id = item_id
        self.text = text
        self.label = label
        self.metadata = metadata or {}


class TestCollect:
    def test_collect_wraps_triage_items_as_captured_items(self):
        fake_items = [
            _FakeTriageItem(
                "journal_t_abc", "Some content here",
                "todo: refactor X",
                metadata={"journal_date": "2026-04-01", "line_count": 1},
            ),
            _FakeTriageItem(
                "journal_t_def", "More content", "another item",
            ),
        ]
        with patch(
            "work_buddy.clarify.adapters.journal.collect_same_day_candidates",
            return_value=(fake_items, "fake-hash"),
        ), patch(
            "work_buddy.clarify.config.load_triage_config",
            return_value={},
        ), patch(
            "work_buddy.clarify.config.resolve_profile",
            return_value="local_general",
        ):
            p = JournalBacklogPipeline()
            captured = p.collect(journal_date="2026-04-01")
        assert len(captured) == 2
        assert all(isinstance(c, CapturedItem) for c in captured)
        assert captured[0].id == "journal_t_abc"
        assert captured[0].source == "journal_segment"
        assert captured[0].type == "todo_line"
        assert captured[0].label == "todo: refactor X"
        assert (
            captured[0].payload.get("raw_text") == "Some content here"
        )

    def test_collect_returns_empty_when_segmenter_finds_nothing(self):
        with patch(
            "work_buddy.clarify.adapters.journal.collect_same_day_candidates",
            return_value=([], "empty-hash"),
        ), patch(
            "work_buddy.clarify.config.load_triage_config",
            return_value={},
        ), patch(
            "work_buddy.clarify.config.resolve_profile",
            return_value="local_general",
        ):
            p = JournalBacklogPipeline()
            captured = p.collect(journal_date="2026-04-01")
        assert captured == []


class TestAnnotateItems:
    def test_annotate_augments_with_tags_and_summary(self):
        items = [
            CapturedItem(
                id="i0", source="journal_segment", type="todo_line",
                label="Do X",
                payload={"raw_text": "Do X tomorrow", "line_count": 1},
            ),
            CapturedItem(
                id="i1", source="journal_segment", type="todo_line",
                label="Do Y", payload={"raw_text": "Y next", "line_count": 1},
            ),
        ]
        manifest = [
            {"id": "i0", "tags": ["wb/todo"], "summary": "task X"},
            {"id": "i1", "tags": ["paper/ecg"], "summary": "task Y"},
        ]
        with patch(
            "work_buddy.journal_backlog.manifest.build_thread_manifest",
            return_value=manifest,
        ):
            p = JournalBacklogPipeline()
            annotated = p.annotate_items(items)
        assert annotated[0].tags == ("wb/todo",)
        assert annotated[0].summary == "task X"
        assert annotated[1].tags == ("paper/ecg",)

    def test_annotate_passes_through_on_manifest_failure(self):
        items = [
            CapturedItem(
                id="i0", source="journal_segment", type="todo_line",
                label="Do X", payload={"raw_text": "Do X", "line_count": 1},
            ),
        ]
        with patch(
            "work_buddy.journal_backlog.manifest.build_thread_manifest",
            side_effect=RuntimeError("LLM down"),
        ):
            p = JournalBacklogPipeline()
            out = p.annotate_items(items)
        # Items pass through unannotated.
        assert out[0].tags == ()
        assert out[0].summary is None

    def test_annotate_empty_input_short_circuits(self):
        p = JournalBacklogPipeline()
        assert p.annotate_items([]) == []


class TestPrecluster:
    def test_precluster_returns_empty_for_empty_input(self):
        p = JournalBacklogPipeline()
        assert p.precluster([]) == []

    def test_precluster_falls_back_on_louvain_failure(self):
        items = [
            CapturedItem(
                id=f"i{i}", source="journal_segment", type="todo_line",
                label=f"Item {i}",
                payload={"raw_text": f"text {i}"},
            )
            for i in range(3)
        ]
        with patch(
            "work_buddy.embedding.client.embed_for_ir",
            return_value=[None, None, None],
        ), patch(
            "work_buddy.ml.clustering.compute_pairwise_similarity",
            side_effect=RuntimeError("blew up"),
        ):
            p = JournalBacklogPipeline()
            clusters = p.precluster(items)
        assert len(clusters) == 1
        assert clusters[0].label == "Ungrouped"
        assert set(clusters[0].item_ids) == {"i0", "i1", "i2"}

    def test_precluster_happy_path_uses_ml_clustering(self):
        items = [
            CapturedItem(
                id=f"i{i}", source="journal_segment", type="todo_line",
                label=f"Item {i}",
                payload={"raw_text": f"text {i}"},
                tags=("wb/todo",),
            )
            for i in range(2)
        ]
        # Stub embeddings (one per item) and make ml.clustering produce
        # one cluster containing both.
        with patch(
            "work_buddy.embedding.client.embed_for_ir",
            return_value=[[0.1] * 8, [0.1] * 8],
        ), patch(
            "work_buddy.ml.clustering.compute_pairwise_similarity",
            return_value=[
                {
                    "id_a": "i0", "id_b": "i1",
                    "fused": 0.9, "embedding_sim": 1.0,
                    "tag_sim": 1.0, "proximity": 0.0,
                }
            ],
        ), patch(
            "work_buddy.ml.clustering.cluster_items",
            return_value=[{
                "cluster_id": 0,
                "thread_ids": ["i0", "i1"],
                "label": "Test cluster",
                "internal_cohesion": 0.9,
                "cross_cluster_edges": [],
            }],
        ):
            p = JournalBacklogPipeline()
            clusters = p.precluster(items)
        assert len(clusters) == 1
        assert clusters[0].label == "Test cluster"
        assert set(clusters[0].item_ids) == {"i0", "i1"}


class TestUmbrellaSummary:
    def test_summary_carries_journal_date(self):
        p = JournalBacklogPipeline()
        s = p.umbrella_summary({"journal_date": "2026-04-01", "scan_id": "abc"})
        assert s["source"] == "journal_backlog"
        assert "2026-04-01" in s["title"]
        assert s["scan_id"] == "abc"

    def test_summary_handles_missing_date(self):
        p = JournalBacklogPipeline()
        s = p.umbrella_summary({})
        assert "unknown" in s["title"]
