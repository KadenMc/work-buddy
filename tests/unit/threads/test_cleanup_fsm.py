"""v5 Stage 4.0 — cleanup FSM states + transitions + new schema columns."""

from __future__ import annotations

import pytest

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.fsm import (
    TRIG_ACCEPT_CLEANUP_FAILURE,
    TRIG_CLEANUP_FAILED,
    TRIG_CLEANUP_REQUESTED,
    TRIG_CLEANUP_SUCCEEDED,
    TRIG_DISMISSED_BY_USER,
    TRIG_PARENT_FORCE_CLOSE,
    TRIG_RETRY_CLEANUP,
    lookup,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# New states are registered
# ---------------------------------------------------------------------------


class TestNewStatesRegistered:
    def test_cleaning_up_exists(self):
        assert FSMState.CLEANING_UP.value == "cleaning_up"

    def test_done_cleanup_successful_exists(self):
        assert FSMState.DONE_CLEANUP_SUCCESSFUL.value == "done_cleanup_successful"

    def test_done_cleanup_unsuccessful_exists(self):
        assert FSMState.DONE_CLEANUP_UNSUCCESSFUL.value == "done_cleanup_unsuccessful"

    def test_done_cleanup_successful_is_terminal(self):
        assert FSMState.DONE_CLEANUP_SUCCESSFUL.is_terminal

    def test_done_cleanup_unsuccessful_is_wait_state(self):
        assert FSMState.DONE_CLEANUP_UNSUCCESSFUL.is_wait_state
        assert not FSMState.DONE_CLEANUP_UNSUCCESSFUL.is_terminal

    def test_cleaning_up_is_neither_terminal_nor_wait(self):
        # It's an active state like inferring_* / executing
        assert not FSMState.CLEANING_UP.is_terminal
        assert not FSMState.CLEANING_UP.is_wait_state


# ---------------------------------------------------------------------------
# Cleanup transitions
# ---------------------------------------------------------------------------


class TestCleanupRequestedTransitions:
    @pytest.mark.parametrize("from_state", [
        FSMState.AWAITING_INTENT_CONFIRMATION,
        FSMState.AWAITING_CONTEXT_CONFIRMATION,
        FSMState.AWAITING_INTENT_CLARIFICATION,
        FSMState.AWAITING_CONTEXT_CLARIFICATION,
        FSMState.AWAITING_ACTION_CLARIFICATION,
        FSMState.AWAITING_CONFIRMATION,
        FSMState.AWAITING_REVIEW,
        FSMState.AWAITING_REDIRECT,
    ])
    def test_cleanup_requested_from_wait_states_goes_to_cleaning_up(self, from_state):
        out = lookup(from_state, TRIG_CLEANUP_REQUESTED)
        assert not out.unspecified
        assert out.next_state == FSMState.CLEANING_UP


class TestCleanupResultTransitions:
    def test_succeeded_goes_to_done_cleanup_successful(self):
        out = lookup(FSMState.CLEANING_UP, TRIG_CLEANUP_SUCCEEDED)
        assert out.next_state == FSMState.DONE_CLEANUP_SUCCESSFUL

    def test_failed_goes_to_done_cleanup_unsuccessful(self):
        out = lookup(FSMState.CLEANING_UP, TRIG_CLEANUP_FAILED)
        assert out.next_state == FSMState.DONE_CLEANUP_UNSUCCESSFUL

    def test_dismissed_mid_cleanup_works(self):
        out = lookup(FSMState.CLEANING_UP, TRIG_DISMISSED_BY_USER)
        assert out.next_state == FSMState.DISMISSED

    def test_force_close_mid_cleanup_works(self):
        out = lookup(FSMState.CLEANING_UP, TRIG_PARENT_FORCE_CLOSE)
        assert out.next_state == FSMState.DISMISSED


class TestRetryFailureTransitions:
    def test_retry_loops_back_to_cleaning_up(self):
        out = lookup(
            FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_RETRY_CLEANUP,
        )
        assert out.next_state == FSMState.CLEANING_UP

    def test_accept_failure_goes_to_done(self):
        out = lookup(
            FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_ACCEPT_CLEANUP_FAILURE,
        )
        assert out.next_state == FSMState.DONE

    def test_dismiss_from_unsuccessful_works(self):
        out = lookup(
            FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_DISMISSED_BY_USER,
        )
        assert out.next_state == FSMState.DISMISSED


class TestTerminalCleanup:
    def test_done_cleanup_successful_is_truly_terminal(self):
        for trig in (
            TRIG_RETRY_CLEANUP,
            TRIG_DISMISSED_BY_USER,
            TRIG_CLEANUP_REQUESTED,
        ):
            out = lookup(FSMState.DONE_CLEANUP_SUCCESSFUL, trig)
            assert out.unspecified, (
                f"DONE_CLEANUP_SUCCESSFUL should reject {trig}"
            )


# ---------------------------------------------------------------------------
# Schema columns (resurface_at, order_index, search_blob)
# ---------------------------------------------------------------------------


class TestStage4Schema:
    def test_resurface_at_column_present(self, fresh_db):
        conn = store.get_connection()
        try:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(threads)")
            }
        finally:
            conn.close()
        assert "resurface_at" in cols
        assert "order_index" in cols
        assert "search_blob" in cols

    def test_indexes_present(self, fresh_db):
        conn = store.get_connection()
        try:
            idxs = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()
        assert "idx_threads_parent_order" in idxs
        assert "idx_threads_resurface" in idxs

    def test_migration_idempotent_on_existing_db(self, tmp_path, monkeypatch):
        # Create a "pre-Stage-4" DB with the realistic Stage 1-3
        # schema (everything except resurface_at / order_index /
        # search_blob), then re-open to trigger _migrate_stage_4.
        import sqlite3
        db = tmp_path / "legacy.db"
        legacy = sqlite3.connect(str(db))
        try:
            legacy.execute(
                """CREATE TABLE threads (
                    thread_id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    subtype TEXT,
                    fsm_state TEXT NOT NULL DEFAULT 'proposed',
                    parent_event_id INTEGER,
                    autonomy_policy_json TEXT NOT NULL DEFAULT '{}',
                    context_items_json TEXT NOT NULL DEFAULT '[]',
                    risk_profile_json TEXT NOT NULL DEFAULT '{}',
                    inciting_event_summary_json TEXT NOT NULL DEFAULT '{}',
                    current_focus_thread_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                )"""
            )
            legacy.execute(
                """CREATE TABLE thread_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    inference_tier TEXT,
                    timestamp TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    parent_event_id INTEGER,
                    migration_id TEXT
                )"""
            )
            legacy.execute(
                "INSERT INTO threads (thread_id, created_at, updated_at) "
                "VALUES ('th-pre4', '2026-01-01', '2026-01-01')"
            )
            legacy.commit()
        finally:
            legacy.close()

        monkeypatch.setattr(store, "_db_path", lambda: db)
        conn = store.get_connection()
        try:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(threads)")
            }
            row = conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?", ("th-pre4",)
            ).fetchone()
        finally:
            conn.close()
        assert "resurface_at" in cols
        assert "order_index" in cols
        assert "search_blob" in cols
        # Pre-existing row gets defaults
        assert row["resurface_at"] is None
        assert row["order_index"] == 0
        assert row["search_blob"] == ""

    def test_migration_is_idempotent_on_already_migrated_db(self, fresh_db):
        # Re-opening a fresh (already-Stage-4-migrated) DB should not
        # error or duplicate columns.
        for _ in range(3):
            conn = store.get_connection()
            conn.close()
        conn = store.get_connection()
        try:
            cols = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(threads)")
            ]
        finally:
            conn.close()
        assert cols.count("resurface_at") == 1
        assert cols.count("order_index") == 1
