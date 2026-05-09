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


# ── enrich_plan project_status ──────────────────────────────────


@pytest.fixture
def _patch_list_projects(monkeypatch):
    """Patch the project registry's list_projects in the namespace_suggest
    module so enrich_plan reads from a controlled list."""
    def _patch(projects):
        import work_buddy.projects.store as proj_store
        monkeypatch.setattr(proj_store, "list_projects", lambda: list(projects))
    return _patch


class TestEnrichPlanProjectStatus:
    def test_no_project_proposed(self, _isolated_store, _patch_list_projects):
        _patch_list_projects([
            {"slug": "work-buddy", "name": "Work Buddy", "status": "active"},
        ])
        out = enrich_plan({"task_text": "untargeted task"})
        ps = out["project_status"]
        assert ps["proposed_slug"] is None
        assert ps["slug_exists"] is False
        assert ps["near_subtrees"] == []
        assert ps["subtree_matches"] == []
        # known_projects is always populated
        assert any(p["slug"] == "work-buddy" for p in ps["known_projects"])

    def test_proposed_slug_via_project_field_exists(
        self, _isolated_store, _patch_list_projects,
    ):
        _patch_list_projects([
            {"slug": "work-buddy", "name": "Work Buddy", "status": "active"},
        ])
        out = enrich_plan({
            "task_text": "do a work-buddy thing",
            "project": "work-buddy",
        })
        ps = out["project_status"]
        assert ps["proposed_slug"] == "work-buddy"
        assert ps["slug_exists"] is True

    def test_proposed_slug_via_project_field_unknown(
        self, _isolated_store, _patch_list_projects,
    ):
        _patch_list_projects([
            {"slug": "work-buddy", "name": "Work Buddy", "status": "active"},
        ])
        out = enrich_plan({
            "task_text": "ambiguous",
            "project": "typo-slug",
        })
        ps = out["project_status"]
        assert ps["proposed_slug"] == "typo-slug"
        assert ps["slug_exists"] is False

    def test_full_subtree_path_via_proposed_tags(
        self, _isolated_store, _patch_list_projects,
    ):
        """If the agent proposes `projects/<slug>/<subtree>` directly, the
        enrichment extracts the slug AND surfaces existing subtrees."""
        _patch_list_projects([
            {"slug": "work-buddy", "name": "Work Buddy", "status": "active"},
        ])
        _seed({
            "t-a": [("projects/work-buddy/systems/knowledge", True)],
            "t-b": [("projects/work-buddy/systems/projects", True)],
        })
        out = enrich_plan({
            "task_text": "wire up an artifact pipeline",
            "proposed_tags": ["projects/work-buddy/systems/artifacts"],
        })
        ps = out["project_status"]
        assert ps["proposed_slug"] == "work-buddy"
        assert ps["slug_exists"] is True
        # The two existing siblings under work-buddy should surface.
        assert "projects/work-buddy/systems/knowledge" in ps["near_subtrees"]
        assert "projects/work-buddy/systems/projects" in ps["near_subtrees"]
        # subtree_matches uses namespace_lookup ranker — non-empty when
        # similar paths exist.
        assert isinstance(ps["subtree_matches"], list)

    def test_near_subtrees_only_under_proposed_slug(
        self, _isolated_store, _patch_list_projects,
    ):
        """near_subtrees must not leak subtrees from other projects."""
        _patch_list_projects([
            {"slug": "work-buddy", "name": "Work Buddy", "status": "active"},
            {"slug": "electricrag", "name": "ElectricRag", "status": "active"},
        ])
        _seed({
            "t-a": [("projects/work-buddy/systems/knowledge", True)],
            "t-b": [("projects/electricrag/experiments", True)],
        })
        out = enrich_plan({
            "task_text": "anything",
            "project": "work-buddy",
        })
        near = out["project_status"]["near_subtrees"]
        assert "projects/work-buddy/systems/knowledge" in near
        assert "projects/electricrag/experiments" not in near


# NOTE: The earlier `create_task_from_plan` helper was removed — the
# task-new workflow now calls task_create via wb_run in the apply step,
# so there's no orchestration wrapper to test here.
