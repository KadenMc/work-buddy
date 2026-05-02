"""v5 Stage 4.12 + 4.13 — source-pipeline spawn helpers."""

from __future__ import annotations

import pytest

from work_buddy.threads import source_pipelines, store
from work_buddy.threads.events import KIND_INCITING_EVENT, KIND_THREAD_CREATED


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# Journal spawner (4.12)
# ---------------------------------------------------------------------------


class TestSpawnFromJournal:
    def test_basic_spawn(self, fresh_db):
        item = {
            "id": "journal_42",
            "text": "- [ ] Buy gift for Sarah\n  More notes here",
            "label": "Buy gift for Sarah",
            "source": "journal_thread",
            "metadata": {
                "thread_id": "42",
                "line_count": 2,
                "journal_date": "2026-05-12",
            },
        }
        tid = source_pipelines.spawn_thread_from_journal_item(item)
        assert tid is not None and tid.startswith("th-")
        thread = store.get_thread(tid)
        assert thread is not None
        assert thread.inciting_event_summary["source"] == "journal_note"
        assert thread.inciting_event_summary["note_path"] == "Daily/2026-05-12.md"
        # First non-empty line as line_text
        assert "Buy gift for Sarah" in thread.inciting_event_summary["line_text"]

    def test_explicit_note_path_overrides(self, fresh_db):
        item = {
            "id": "x", "text": "stuff", "label": "x",
            "source": "journal_thread",
            "metadata": {"journal_date": "2026-01-01"},
        }
        tid = source_pipelines.spawn_thread_from_journal_item(
            item, note_path="Other/path.md",
        )
        thread = store.get_thread(tid)
        assert thread.inciting_event_summary["note_path"] == "Other/path.md"

    def test_no_journal_date_no_note_path_returns_none(self, fresh_db):
        item = {"id": "x", "text": "y", "label": "y",
                "source": "journal_thread", "metadata": {}}
        assert source_pipelines.spawn_thread_from_journal_item(item) is None

    def test_inciting_and_thread_created_events_recorded(self, fresh_db):
        item = {
            "id": "x", "text": "todo", "label": "todo",
            "source": "journal_thread",
            "metadata": {"journal_date": "2026-05-12"},
        }
        tid = source_pipelines.spawn_thread_from_journal_item(item)
        kinds = [e.kind for e in store.list_events(tid)]
        assert KIND_INCITING_EVENT in kinds
        assert KIND_THREAD_CREATED in kinds

    def test_context_item_carries_raw_text(self, fresh_db):
        item = {
            "id": "x", "text": "raw raw raw", "label": "x",
            "source": "journal_thread",
            "metadata": {"journal_date": "2026-05-12"},
        }
        tid = source_pipelines.spawn_thread_from_journal_item(item)
        thread = store.get_thread(tid)
        assert len(thread.context_items) == 1
        assert thread.context_items[0].payload.get("raw_text") == "raw raw raw"

    def test_bulk_spawn(self, fresh_db):
        items = [
            {"id": f"j{i}", "text": f"todo {i}", "label": f"todo {i}",
             "source": "journal_thread",
             "metadata": {"journal_date": "2026-05-12"}}
            for i in range(3)
        ]
        ids = source_pipelines.spawn_threads_from_journal_scan(
            items, journal_date="2026-05-12",
        )
        assert len(ids) == 3
        for tid in ids:
            t = store.get_thread(tid)
            assert t.inciting_event_summary["note_path"] == "Daily/2026-05-12.md"


# ---------------------------------------------------------------------------
# Chrome spawner (4.13)
# ---------------------------------------------------------------------------


class TestChromeSpawner:
    def test_parent_thread_created(self, fresh_db):
        tid = source_pipelines.spawn_parent_thread_from_chrome_scrape(
            scrape_id="scrape-123",
            summary="ECG paper research",
        )
        assert tid is not None
        thread = store.get_thread(tid)
        assert thread.inciting_event_summary["source"] == "chrome_scrape"
        assert thread.inciting_event_summary["scrape_id"] == "scrape-123"

    def test_chrome_tab_to_context_item(self):
        tab = {
            "id": "tab-1",
            "title": "GitHub - work-buddy",
            "url": "https://github.com/...",
            "window_id": "w1",
            "group_id": "research",
            "tab_index": 5,
        }
        ci = source_pipelines.chrome_tab_to_context_item(tab)
        assert ci.id == "tab-1"
        assert ci.label == "GitHub - work-buddy"
        assert ci.source == "chrome_tab"
        assert ci.payload["url"] == "https://github.com/..."
        assert ci.payload["window_id"] == "w1"

    def test_chrome_tab_with_no_title_uses_url(self):
        tab = {"id": "tab-2", "url": "https://x.com"}
        ci = source_pipelines.chrome_tab_to_context_item(tab)
        assert ci.label == "https://x.com"
