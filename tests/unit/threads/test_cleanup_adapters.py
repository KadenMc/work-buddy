"""v5 Stage 4.4 — journal-note cleanup adapter + state-entry handler.

Pins:
- Journal adapter reads file → finds line by exact-text match →
  removes it → writes back.
- Missing line returns source_already_gone=True (success).
- Missing/unreachable file returns success=False.
- can_clean_up returns False without note_path or line_text.
- State-entry handler fires after CLEANING_UP entry, transitions
  Thread to DONE_CLEANUP_SUCCESSFUL or DONE_CLEANUP_UNSUCCESSFUL.
- bootstrap_v5 registers the journal adapter.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.threads import (
    bootstrap,
    cleanup,
    cleanup_adapters,
    cleanup_runner,
    engine,
    store,
)
from work_buddy.threads.cleanup import CleanupResult
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_CLEANUP_FAILED,
    KIND_SOURCE_CLEANED_UP,
)
from work_buddy.threads.fsm import TRIG_CLEANUP_REQUESTED
from work_buddy.threads.models import Thread


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    bootstrap.teardown_v5()
    yield
    bootstrap.teardown_v5()


# ---------------------------------------------------------------------------
# Journal-note adapter
# ---------------------------------------------------------------------------


class TestJournalNoteCanCleanUp:
    def test_yes_when_summary_complete(self):
        adapter = cleanup_adapters.JOURNAL_NOTE_ADAPTER
        t = Thread(inciting_event_summary={
            "source": "journal_note",
            "note_path": "Daily/2026-05-02.md",
            "line_text": "- [ ] Buy gift",
        })
        assert adapter.can_clean_up(t) is True

    def test_no_when_source_mismatch(self):
        adapter = cleanup_adapters.JOURNAL_NOTE_ADAPTER
        t = Thread(inciting_event_summary={
            "source": "chrome_tab",
            "note_path": "x", "line_text": "y",
        })
        assert adapter.can_clean_up(t) is False

    def test_no_when_note_path_missing(self):
        adapter = cleanup_adapters.JOURNAL_NOTE_ADAPTER
        t = Thread(inciting_event_summary={
            "source": "journal_note",
            "line_text": "x",
        })
        assert adapter.can_clean_up(t) is False

    def test_no_when_line_text_missing(self):
        adapter = cleanup_adapters.JOURNAL_NOTE_ADAPTER
        t = Thread(inciting_event_summary={
            "source": "journal_note",
            "note_path": "x.md",
        })
        assert adapter.can_clean_up(t) is False


class TestJournalNoteCleanup:
    def _thread(self, **inciting_extras):
        return Thread(inciting_event_summary={
            "source": "journal_note",
            "note_path": "Daily/2026-05-02.md",
            "line_text": "- [ ] Buy gift for Sarah",
            **inciting_extras,
        })

    def test_removes_matching_line(self):
        original = (
            "# Daily 2026-05-02\n"
            "- [ ] Talk to Anna\n"
            "- [ ] Buy gift for Sarah\n"
            "- [ ] Submit grant application\n"
        )
        captured = {}
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value=original) as _read, \
             patch("work_buddy.obsidian.bridge.write_file",
                   side_effect=lambda path, content, **kw:
                       (captured.update({"path": path, "content": content})
                        or True)) as _write:
            r = cleanup_adapters._journal_note_cleanup(self._thread())
        assert r.success is True
        assert r.source_already_gone is False
        assert "Buy gift for Sarah" not in captured["content"]
        assert "Talk to Anna" in captured["content"]
        assert "Submit grant application" in captured["content"]

    def test_line_not_found_is_source_already_gone(self):
        # Original doesn't contain the target line — user already
        # edited it out.
        original = (
            "# Daily 2026-05-02\n"
            "- [ ] Talk to Anna\n"
        )
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value=original), \
             patch("work_buddy.obsidian.bridge.write_file") as _write:
            r = cleanup_adapters._journal_note_cleanup(self._thread())
        assert r.success is True
        assert r.source_already_gone is True
        # Did NOT call write_file
        _write.assert_not_called()

    def test_file_unreachable_is_failure(self):
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value=None):
            r = cleanup_adapters._journal_note_cleanup(self._thread())
        assert r.success is False
        assert "could not read" in r.detail.lower()

    def test_write_failure_returns_failure(self):
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value="- [ ] Buy gift for Sarah\n"), \
             patch("work_buddy.obsidian.bridge.write_file",
                   return_value=False):
            r = cleanup_adapters._journal_note_cleanup(self._thread())
        assert r.success is False
        assert "write_file" in r.detail.lower()

    def test_only_removes_first_match(self):
        original = (
            "- [ ] Buy gift for Sarah\n"
            "- [ ] Buy gift for Sarah\n"
        )
        captured = {}
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value=original), \
             patch("work_buddy.obsidian.bridge.write_file",
                   side_effect=lambda path, content, **kw:
                       (captured.update({"content": content}) or True)):
            r = cleanup_adapters._journal_note_cleanup(self._thread())
        assert r.success is True
        # Exactly one match left
        assert captured["content"].count("Buy gift for Sarah") == 1


# ---------------------------------------------------------------------------
# Default-adapter registration
# ---------------------------------------------------------------------------


class TestDefaultAdapterRegistration:
    def test_register_default_adds_journal_adapter(self):
        cleanup.clear_cleanup_adapters()
        cleanup_adapters.register_default_adapters()
        a = cleanup.get_cleanup_adapter("journal_note")
        assert a is not None
        assert a.source == "journal_note"

    def test_bootstrap_registers_journal_adapter(self):
        cleanup.clear_cleanup_adapters()
        bootstrap.bootstrap_v5(clear_first=True)
        assert cleanup.get_cleanup_adapter("journal_note") is not None


# ---------------------------------------------------------------------------
# State-entry handler (cleanup_runner)
# ---------------------------------------------------------------------------


class TestCleanupRunnerHandler:
    def _setup_thread(self):
        # Thread sitting in CLEANING_UP, journal adapter registered
        cleanup.register_cleanup_adapter(cleanup_adapters.JOURNAL_NOTE_ADAPTER)
        t = Thread(
            fsm_state=FSMState.CLEANING_UP,
            inciting_event_summary={
                "source": "journal_note",
                "note_path": "Daily/x.md",
                "line_text": "- [ ] todo",
            },
        )
        store.insert_thread(t)
        return t

    def test_success_path_advances_to_done_cleanup_successful(self):
        t = self._setup_thread()
        cleanup_runner.register_cleanup_runner()
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value="- [ ] todo\n"), \
             patch("work_buddy.obsidian.bridge.write_file",
                   return_value=True):
            # Synthesize a TransitionResult and fire side effects
            engine.register_state_entry_handler(  # already registered, but ok
                FSMState.CLEANING_UP, cleanup_runner.cleanup_state_entry_handler,
            )
            result = engine._fire_side_effects(engine.TransitionResult(
                thread_id=t.thread_id,
                prev_state=FSMState.AWAITING_CONFIRMATION,
                next_state=FSMState.CLEANING_UP,
                trigger=TRIG_CLEANUP_REQUESTED,
                event_id=0,
                data={},
            ))
        # Thread should have advanced past CLEANING_UP
        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.DONE_CLEANUP_SUCCESSFUL
        # source_cleaned_up event recorded
        events = store.list_events(t.thread_id)
        kinds = [e.kind for e in events]
        assert KIND_SOURCE_CLEANED_UP in kinds

    def test_failure_path_advances_to_unsuccessful(self):
        t = self._setup_thread()
        cleanup_runner.register_cleanup_runner()
        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value=None):  # file unreachable
            engine._fire_side_effects(engine.TransitionResult(
                thread_id=t.thread_id,
                prev_state=FSMState.AWAITING_CONFIRMATION,
                next_state=FSMState.CLEANING_UP,
                trigger=TRIG_CLEANUP_REQUESTED,
                event_id=0,
                data={},
            ))
        fetched = store.get_thread(t.thread_id)
        assert fetched.fsm_state == FSMState.DONE_CLEANUP_UNSUCCESSFUL
        events = store.list_events(t.thread_id)
        kinds = [e.kind for e in events]
        assert KIND_CLEANUP_FAILED in kinds

    def test_handler_skips_non_cleaning_up_state(self):
        # Defensive: handler should be a no-op for other states
        cleanup_runner.cleanup_state_entry_handler(engine.TransitionResult(
            thread_id="th-irrelevant",
            prev_state=FSMState.PROPOSED,
            next_state=FSMState.AWAITING_INFERENCE,
            trigger="x",
            event_id=0,
            data={},
        ))
        # No exception = pass


# ---------------------------------------------------------------------------
# End-to-end: bootstrap + click Clean Up + result
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_bootstrap_then_cleanup_walks_full_flow(self):
        bootstrap.bootstrap_v5(clear_first=True)
        # Thread with valid inciting source
        t = Thread(
            fsm_state=FSMState.AWAITING_CONFIRMATION,
            inciting_event_summary={
                "source": "journal_note",
                "note_path": "Daily/test.md",
                "line_text": "- [ ] test todo",
            },
        )
        store.insert_thread(t)

        with patch("work_buddy.obsidian.bridge.read_file",
                   return_value="- [ ] test todo\n"), \
             patch("work_buddy.obsidian.bridge.write_file",
                   return_value=True):
            engine.transition(t.thread_id, TRIG_CLEANUP_REQUESTED)

        fetched = store.get_thread(t.thread_id)
        # CLEANING_UP entry handler ran the adapter, fired
        # cleanup_succeeded, transitioned to DONE_CLEANUP_SUCCESSFUL.
        assert fetched.fsm_state == FSMState.DONE_CLEANUP_SUCCESSFUL
