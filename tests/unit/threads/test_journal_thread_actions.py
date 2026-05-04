"""Tests for ``work_buddy.journal_backlog.thread_actions`` — the
per-thread route-to-tasks / route-to-considerations / append-to-note
capability backers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.journal_backlog.thread_actions import (
    JournalThreadActionError,
    journal_append_to_note,
    journal_route_to_considerations,
    journal_route_to_tasks,
)
from work_buddy.threads import models, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _make_thread_with_items(items: list[dict]):
    ctx_items = tuple(
        models.ContextItem(
            id=item["id"],
            source="journal_segment",
            type="todo_line",
            label=item.get("label", item["id"]),
            payload={
                "raw_text": item.get("raw_text", item.get("label", "")),
            },
        )
        for item in items
    )
    t = models.Thread(context_items=ctx_items)
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# journal_route_to_tasks
# ---------------------------------------------------------------------------


class TestRouteToTasks:
    def test_creates_one_task_per_item(self, fresh_db, tmp_path):
        t = _make_thread_with_items([
            {"id": "i0", "label": "Refactor X"},
            {"id": "i1", "label": "Test Y"},
        ])
        with patch(
            "work_buddy.journal_backlog.route._create_task_impl",
            return_value={"success": True, "task_line": "fake_line"},
        ) as mock_create:
            result = journal_route_to_tasks(t.thread_id, vault_root=tmp_path)
        assert mock_create.call_count == 2
        assert len(result["created"]) == 2
        assert result["failed"] == []

    def test_continues_on_per_item_failure(self, fresh_db, tmp_path):
        t = _make_thread_with_items([
            {"id": "i0", "label": "Good"},
            {"id": "i1", "label": "Bad"},
            {"id": "i2", "label": "Good"},
        ])
        outcomes = [
            {"success": True, "task_line": "ok"},
            RuntimeError("simulated failure"),
            {"success": True, "task_line": "ok"},
        ]
        def fake_create(**kwargs):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        with patch(
            "work_buddy.journal_backlog.route._create_task_impl",
            side_effect=fake_create,
        ):
            result = journal_route_to_tasks(t.thread_id, vault_root=tmp_path)
        assert len(result["created"]) == 2
        assert len(result["failed"]) == 1
        assert result["failed"][0]["item_id"] == "i1"

    def test_empty_thread_returns_skipped_empty(self, fresh_db, tmp_path):
        t = _make_thread_with_items([])
        result = journal_route_to_tasks(t.thread_id, vault_root=tmp_path)
        assert result["skipped_empty"] is True
        assert result["created"] == []

    def test_missing_thread_raises(self, fresh_db, tmp_path):
        with pytest.raises(JournalThreadActionError, match="not found"):
            journal_route_to_tasks("th-nope", vault_root=tmp_path)


# ---------------------------------------------------------------------------
# journal_route_to_considerations
# ---------------------------------------------------------------------------


class TestRouteToConsiderations:
    def test_creates_one_consideration_per_item(self, fresh_db, tmp_path):
        t = _make_thread_with_items([
            {"id": "i0", "label": "Question A", "raw_text": "Body A"},
            {"id": "i1", "label": "Question B", "raw_text": "Body B"},
        ])
        with patch(
            "work_buddy.journal_backlog.route._create_consideration_impl",
            return_value={"success": True, "file": "x.md"},
        ) as mock_create:
            result = journal_route_to_considerations(
                t.thread_id, vault_root=tmp_path, project="ecg",
            )
        assert mock_create.call_count == 2
        # Each call should pass the item's body as raw_text.
        first_call_kwargs = mock_create.call_args_list[0].kwargs
        assert first_call_kwargs["body"] == "Body A"
        assert first_call_kwargs["project"] == "ecg"
        assert len(result["created"]) == 2


# ---------------------------------------------------------------------------
# journal_append_to_note
# ---------------------------------------------------------------------------


class TestAppendToNote:
    def test_appends_all_items_as_bullets(self, fresh_db, tmp_path):
        t = _make_thread_with_items([
            {"id": "i0", "label": "Bullet A"},
            {"id": "i1", "label": "Bullet B"},
        ])
        captured = {}
        def fake_append(content, root, note_path):
            captured["content"] = content
            captured["note_path"] = note_path
            return {"success": True, "file": str(root / note_path)}
        with patch(
            "work_buddy.journal_backlog.route._append_to_note_impl",
            side_effect=fake_append,
        ):
            result = journal_append_to_note(
                t.thread_id, note_path="projects/ecg/main.md",
                vault_root=tmp_path,
            )
        assert "- Bullet A" in captured["content"]
        assert "- Bullet B" in captured["content"]
        assert captured["note_path"] == "projects/ecg/main.md"
        assert len(result["appended"]) == 2

    def test_failure_returns_per_item_errors(self, fresh_db, tmp_path):
        t = _make_thread_with_items([
            {"id": "i0", "label": "X"},
            {"id": "i1", "label": "Y"},
        ])
        with patch(
            "work_buddy.journal_backlog.route._append_to_note_impl",
            side_effect=RuntimeError("permission denied"),
        ):
            result = journal_append_to_note(
                t.thread_id, note_path="x.md", vault_root=tmp_path,
            )
        assert result["appended"] == []
        assert len(result["failed"]) == 2
