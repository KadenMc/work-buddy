"""Unit tests for dashboard namespace endpoints (Phase 3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.dashboard import api as dashboard_api
from work_buddy.obsidian.tasks import store


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    db_file = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_file)
    return db_file


@pytest.fixture
def _fake_summary(monkeypatch):
    """Patch dashboard_api.get_tasks_summary to return fixed fake tasks."""
    fake = {
        "tasks": [
            {"id": "t-a", "text": "draft outline", "state": "inbox",
             "done": False, "urgency": "none", "note_id": "", "markers": []},
            {"id": "t-b", "text": "run augmentation", "state": "mit",
             "done": False, "urgency": "high", "note_id": "", "markers": []},
            {"id": "t-c", "text": "file taxes", "state": "inbox",
             "done": False, "urgency": "none", "note_id": "", "markers": []},
            {"id": "t-d", "text": "read paper", "state": "snoozed",
             "done": False, "urgency": "none", "note_id": "", "markers": []},
        ],
        "counts": {"inbox": 2, "mit": 1, "snoozed": 1},
    }
    monkeypatch.setattr(dashboard_api, "get_tasks_summary", lambda: fake)
    return fake


def _seed_tasks(ids_with_tags: dict[str, list[tuple[str, bool]]]):
    for tid, tags in ids_with_tags.items():
        store.create(task_id=tid, state="inbox")
        store.set_task_tags(tid, tags)


def test_list_namespaces_empty(_isolated_store):
    result = dashboard_api.list_namespaces()
    assert result["namespaces"] == []
    assert result["count"] == 0
    assert result["recent_days"] == 14


def test_list_namespaces_counts(_isolated_store):
    _seed_tasks({
        "t-a": [("paper/ecg", True)],
        "t-b": [("paper/ecg", True), ("admin", False)],
        "t-c": [("paper/other", True)],
    })
    result = dashboard_api.list_namespaces()
    by_tag = {n["tag"]: n["count"] for n in result["namespaces"]}
    assert by_tag == {"paper/ecg": 2, "paper/other": 1}
    assert result["count"] == 2


def test_by_namespace_descendants_included(_isolated_store, _fake_summary):
    _seed_tasks({
        "t-a": [("paper", True)],
        "t-b": [("paper/ecg", True)],
        "t-c": [("admin", True)],
        "t-d": [("paper/ecg/experiments", True)],
    })
    result = dashboard_api.get_tasks_by_namespace("paper", include_descendants=True)
    ids = {t["id"] for t in result["tasks"]}
    assert ids == {"t-a", "t-b", "t-d"}
    assert result["count"] == 3
    # Descendant summary shows immediate-child collapse.
    child_tags = {d["tag"] for d in result["descendants"]}
    assert "paper/ecg" in child_tags


def test_by_namespace_exact_only(_isolated_store, _fake_summary):
    _seed_tasks({
        "t-a": [("paper", True)],
        "t-b": [("paper/ecg", True)],
    })
    result = dashboard_api.get_tasks_by_namespace("paper", include_descendants=False)
    ids = {t["id"] for t in result["tasks"]}
    assert ids == {"t-a"}


def test_by_namespace_strips_leading_hash(_isolated_store, _fake_summary):
    _seed_tasks({"t-a": [("admin", True)]})
    result = dashboard_api.get_tasks_by_namespace("#admin")
    ids = {t["id"] for t in result["tasks"]}
    assert ids == {"t-a"}
    assert result["namespace"] == "admin"


def test_by_namespace_empty_input(_isolated_store, _fake_summary):
    result = dashboard_api.get_tasks_by_namespace("")
    assert result == {"namespace": "", "count": 0, "tasks": [], "descendants": []}
