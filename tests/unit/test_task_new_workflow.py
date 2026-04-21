"""Unit tests for the task-new workflow adapter helpers (Phase 8)."""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks import namespace_suggest as ns_mod
from work_buddy.obsidian.tasks.namespace_suggest import enrich_plan


@pytest.fixture(autouse=True)
def _force_tokens_path(monkeypatch):
    """Keep embedding service out of test determinism."""
    monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "tasks.sqlite")


def _seed(ids_with_tags):
    for tid, tags in ids_with_tags.items():
        store.create(task_id=tid, state="inbox")
        store.set_task_tags(tid, tags)


# ── enrich_plan ─────────────────────────────────────────────────


class TestEnrichPlan:
    def test_bad_input_shape(self):
        out = enrich_plan("not a dict")  # type: ignore[arg-type]
        assert "error" in out
        assert out["plan"] == {}

    def test_empty_universe(self, _isolated_store):
        out = enrich_plan({"task_text": "draft something"})
        assert out["universe_size"] == 0
        assert out["suggestions"] == []
        assert out["tag_status"] == {}

    def test_existing_tags_flagged_as_exists(self, _isolated_store):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
        })
        out = enrich_plan({
            "task_text": "ECG paper draft",
            "proposed_tags": ["paper/ecg-classifier"],
        })
        status = out["tag_status"]["paper/ecg-classifier"]
        assert status["exists"] is True

    def test_new_tag_flagged_as_not_exists(self, _isolated_store):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
        })
        out = enrich_plan({
            "task_text": "sleep experiment",
            "proposed_tags": ["wellness/sleep"],
        })
        status = out["tag_status"]["wellness/sleep"]
        assert status["exists"] is False
        # Near-matches is a list; it's allowed to be empty when nothing is close,
        # but the key must be present.
        assert isinstance(status["near_matches"], list)

    def test_proposed_tag_with_leading_hash_normalized(self, _isolated_store):
        _seed({
            "t-a": [("admin", True)],
            "t-b": [("admin", True)],
        })
        out = enrich_plan({
            "task_text": "file taxes",
            "proposed_tags": ["#admin"],
        })
        # Keyed by normalized tag (no leading #).
        assert out["tag_status"]["admin"]["exists"] is True

    def test_non_list_proposed_tags_tolerated(self, _isolated_store):
        out = enrich_plan({"task_text": "anything", "proposed_tags": "not a list"})
        assert out["tag_status"] == {}


# NOTE: The earlier `create_task_from_plan` helper was removed — the
# task-new workflow now calls task_create via wb_run in the apply step,
# so there's no orchestration wrapper to test here.
