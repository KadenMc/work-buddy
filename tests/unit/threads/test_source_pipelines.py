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
        assert thread.inciting_event_summary["note_path"] == "journal/2026-05-12.md"
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

    def test_spawn_uses_plan_then_review_autonomy_default(self, fresh_db):
        """Phase 2: journal-spawned threads should default to
        plan_then_review (auto-advance intent + context, pause at
        action). The bare AutonomyPolicy() default would block every
        wait state — that's the regression this test guards against."""
        from work_buddy.threads.autonomy import PLAN_THEN_REVIEW
        item = {
            "id": "x", "text": "todo", "label": "todo",
            "source": "journal_thread",
            "metadata": {"journal_date": "2026-05-12"},
        }
        tid = source_pipelines.spawn_thread_from_journal_item(item)
        assert tid is not None
        thread = store.get_thread(tid)
        # The composed policy may differ on a few axes (e.g. budget)
        # if the user overrides via config; the structural axis we
        # care about is auto_advance_states matching plan_then_review.
        assert thread.autonomy_policy.auto_advance_states == \
            PLAN_THEN_REVIEW.auto_advance_states

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
            assert t.inciting_event_summary["note_path"] == "journal/2026-05-12.md"


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


class TestChromeScrapeEndToEnd:
    def test_creates_parent_and_sub_threads(self, fresh_db):
        # Stub embedding so linearization is deterministic
        from unittest.mock import patch
        from work_buddy.threads import linearization
        tabs = [
            {"id": "t1", "title": "GitHub", "url": "https://github.com"},
            {"id": "t2", "title": "Stack Overflow", "url": "https://stackoverflow.com"},
            {"id": "t3", "title": "ECG paper", "url": "https://example.com/ecg"},
        ]
        with patch.object(linearization, "_embed_texts", return_value=None):
            result = source_pipelines.spawn_threads_from_chrome_scrape(
                tabs=tabs, scrape_id="scrape-1", summary="Research session",
            )
        assert result is not None
        assert result["count"] == 3
        assert result["parent_id"].startswith("th-")
        # Parent has 3 children
        children = store.list_threads(parent_id=result["parent_id"])
        assert len(children) == 3
        # Each child carries the chrome_tab inciting source
        for c in children:
            ci = c.context_items[0] if c.context_items else None
            assert ci is not None
            assert ci.source == "chrome_tab"

    def test_empty_tabs_returns_none(self, fresh_db):
        assert source_pipelines.spawn_threads_from_chrome_scrape(tabs=[]) is None


class TestChromeTabCleanupAdapter:
    def test_register_then_can_clean_up(self):
        from work_buddy.threads import cleanup
        cleanup.clear_cleanup_adapters()
        source_pipelines.register_chrome_tab_cleanup_adapter()
        adapter = cleanup.get_cleanup_adapter("chrome_tab")
        assert adapter is not None

    def test_cleanup_returns_failure_with_clear_message(self, fresh_db):
        from work_buddy.threads import cleanup
        from work_buddy.threads.models import Thread
        cleanup.clear_cleanup_adapters()
        source_pipelines.register_chrome_tab_cleanup_adapter()
        t = Thread(inciting_event_summary={
            "source": "chrome_tab",
            "url": "https://x.com",
        })
        result = cleanup.perform_cleanup(t)
        assert result.success is False
        assert "not yet wired" in result.detail.lower()
