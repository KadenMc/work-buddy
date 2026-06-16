"""Component tests for messaging SQLite models — schema, CRUD, read tracking, TTL."""

import pytest
from datetime import datetime, timezone, timedelta
from freezegun import freeze_time

import sqlite3

from work_buddy.messaging.models import (
    create_message,
    get_message,
    query_messages,
    record_read,
    has_been_read_by,
    update_status,
    get_thread,
    create_reply,
    summarize_pending,
    _format_age,
    _classify_disposition,
    _migrate,
    DISPOSITION_ACTIONABLE,
    DISPOSITION_ACKNOWLEDGEMENT,
)


class TestMessageCRUD:
    def test_create_and_get(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(
            conn,
            sender="agent-a",
            recipient="agent-b",
            type="task",
            subject="Test subject",
            body="Test body",
        )
        assert msg["sender"] == "agent-a"
        assert msg["recipient"] == "agent-b"
        assert msg["status"] == "pending"
        assert msg["id"] is not None

        fetched = get_message(conn, msg["id"])
        assert fetched is not None
        assert fetched["subject"] == "Test subject"

    def test_get_nonexistent_returns_none(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        assert get_message(conn, "nonexistent-id") is None

    def test_query_by_recipient(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="b", type="task", subject="For B")
        create_message(conn, sender="a", recipient="c", type="task", subject="For C")

        results = query_messages(conn, recipient="b")
        assert len(results) == 1
        assert results[0]["subject"] == "For B"

    def test_query_by_status(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="S")
        update_status(conn, msg["id"], "resolved")

        pending = query_messages(conn, recipient="b", status="pending")
        assert len(pending) == 0

        resolved = query_messages(conn, recipient="b", status="resolved")
        assert len(resolved) == 1


class TestReadTracking:
    def test_record_and_check_read(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="S")
        assert not has_been_read_by(conn, msg["id"], "session-1")

        record_read(conn, msg["id"], "session-1", reader_project="b")
        assert has_been_read_by(conn, msg["id"], "session-1")

    def test_read_by_different_sessions(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="S")

        record_read(conn, msg["id"], "session-1", reader_project="b")
        assert has_been_read_by(conn, msg["id"], "session-1")
        assert not has_been_read_by(conn, msg["id"], "session-2")

    def test_duplicate_read_is_ignored(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="S")
        record_read(conn, msg["id"], "session-1")
        record_read(conn, msg["id"], "session-1")  # Should not raise


class TestThreads:
    def test_auto_thread_id(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="S")
        assert msg["thread_id"].startswith("thr-")

    def test_reply_inherits_thread(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        parent = create_message(conn, sender="a", recipient="b", type="task", subject="Original")
        reply = create_reply(conn, parent["id"], sender="b", body="Got it")
        assert reply["thread_id"] == parent["thread_id"]
        assert reply["in_reply_to"] == parent["id"]
        assert reply["subject"] == "Re: Original"

    def test_get_thread_chronological(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        parent = create_message(conn, sender="a", recipient="b", type="task", subject="S")
        create_reply(conn, parent["id"], sender="b", body="Reply 1")
        create_reply(conn, parent["id"], sender="b", body="Reply 2")

        thread = get_thread(conn, parent["thread_id"])
        assert len(thread) == 3
        # First message should be the original
        assert thread[0]["id"] == parent["id"]

    def test_reply_to_nonexistent_returns_none(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        result = create_reply(conn, "nonexistent", sender="b", body="Hello")
        assert result is None


class TestFormatAge:
    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_minutes_ago(self):
        ts = (datetime(2026, 4, 12, 11, 45, tzinfo=timezone.utc)).isoformat()
        assert _format_age(ts) == "15m ago"

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_hours_ago(self):
        ts = (datetime(2026, 4, 12, 9, 0, tzinfo=timezone.utc)).isoformat()
        assert _format_age(ts) == "3h ago"

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_days_ago(self):
        ts = (datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)).isoformat()
        assert _format_age(ts) == "2d ago"

    def test_invalid_timestamp(self):
        assert _format_age("not-a-date") == "unknown age"


class TestSummarizePending:
    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_empty_returns_empty_string(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        result = summarize_pending(conn, "agent-b")
        assert result == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_new_messages_appear_in_summary(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="b", type="task", subject="Important")
        result = summarize_pending(conn, "b", session="sess-1", include_instructions=False)
        assert "Important" in result
        assert "1 new" in result

    def test_read_messages_excluded_after_ttl(self, tmp_messaging_db):
        """Read messages outside the TTL window should be excluded from summary."""
        conn, _ = tmp_messaging_db
        # Create a message "in the past" and mark it read
        with freeze_time("2026-04-01T12:00:00+00:00"):
            msg = create_message(conn, sender="a", recipient="b", type="task", subject="Old")
            record_read(conn, msg["id"], "sess-1", reader_project="b")

        # Now check summary "days later" with a short TTL
        with freeze_time("2026-04-12T12:00:00+00:00"):
            result = summarize_pending(conn, "b", session="sess-2", ttl_days=1, include_instructions=False)
            assert result == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_unread_only_false_keeps_read_within_ttl(self, tmp_messaging_db):
        """Non-blocking summaries still show a read-but-recent message (context)."""
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="Recent")
        record_read(conn, msg["id"], "sess-1", reader_project="b")
        result = summarize_pending(
            conn, "b", session="sess-2", ttl_days=7,
            include_instructions=False, unread_only=False,
        )
        assert "Recent" in result
        assert "(read by" in result  # rendered as already-seen, not *NEW*

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_unread_only_true_excludes_read_within_ttl(self, tmp_messaging_db):
        """Stop-hook summary drops a read message so it cannot keep blocking."""
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="task", subject="Recent")
        record_read(conn, msg["id"], "sess-1", reader_project="b")
        result = summarize_pending(
            conn, "b", session="sess-2", ttl_days=7,
            include_instructions=False, unread_only=True,
        )
        assert result == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_unread_only_true_surfaces_unread(self, tmp_messaging_db):
        """An unread message still surfaces under unread_only (the one-shot block)."""
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="b", type="event", subject="Fresh")
        result = summarize_pending(
            conn, "b", session="sess-1",
            include_instructions=False, unread_only=True,
        )
        assert "Fresh" in result

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_surface_once_then_release(self, tmp_messaging_db):
        """First Stop render surfaces + auto-marks-read; the next render releases."""
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="b", type="event", subject="OneShot")
        first = summarize_pending(
            conn, "b", session="sess-1",
            include_instructions=False, unread_only=True,
        )
        assert "OneShot" in first
        assert has_been_read_by(conn, msg["id"], "sess-1")
        second = summarize_pending(
            conn, "b", session="sess-1",
            include_instructions=False, unread_only=True,
        )
        assert second == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_born_resolved_absent_from_pending(self, tmp_messaging_db):
        """A message created with a terminal status never enters the pending path."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="sidecar:retry_queue", recipient="b",
            type="retry_success", subject="Retry succeeded", status="resolved",
        )
        assert query_messages(conn, recipient="b", status="pending") == []
        result = summarize_pending(conn, "b", session="sess-1", include_instructions=False)
        assert result == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_high_priority_blocks_until_resolved(self, tmp_messaging_db):
        """High-priority read+pending keeps blocking until resolved; resolving releases."""
        conn, _ = tmp_messaging_db
        msg = create_message(
            conn, sender="sidecar:retry_queue", recipient="b",
            type="retry_exhausted", subject="Boom", priority="high",
        )
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "Boom" in first  # surfaces, auto-marks read
        assert has_been_read_by(conn, msg["id"], "s1")
        # Still blocks even though read, because it is high priority.
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "Boom" in second
        # Resolving it (the discoverable /tmp/wb/resolve exit) releases the block.
        update_status(conn, msg["id"], "resolved")
        third = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert third == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_normal_priority_releases_but_high_does_not(self, tmp_messaging_db):
        """Same read state: a normal message releases, a high one keeps blocking."""
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="b", type="task", subject="LowPri", priority="normal")
        create_message(conn, sender="a", recipient="b", type="event", subject="HighPri", priority="high")
        # First render surfaces both and marks them read.
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "LowPri" in first and "HighPri" in first
        # Second render: normal released, high still blocking.
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "LowPri" not in second
        assert "HighPri" in second

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_instructions_include_resolve_verb(self, tmp_messaging_db):
        """The resolve helper must be discoverable in the messaging instructions."""
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="b", type="task", subject="Hi")
        result = summarize_pending(conn, "b", session="s1", include_instructions=True)
        assert "/tmp/wb/resolve" in result

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_acknowledgement_never_blocks_stop_hook(self, tmp_messaging_db):
        """An acknowledgement message is excluded from the Stop path even unread+high."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="notification-system", recipient="b",
            type="result", subject="Plumbing", priority="high",
            disposition=DISPOSITION_ACKNOWLEDGEMENT,
        )
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert first == ""  # no unread tax
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert second == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_acknowledgement_visible_in_context_summary(self, tmp_messaging_db):
        """Excluded only from the Stop path: non-blocking summaries still show it."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="notification-system", recipient="b",
            type="result", subject="PlumbingCtx", priority="high",
            disposition=DISPOSITION_ACKNOWLEDGEMENT,
        )
        summary = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=False)
        assert "PlumbingCtx" in summary

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_actionable_high_blocks_until_resolved(self, tmp_messaging_db):
        """An actionable high-priority message still blocks until resolved."""
        conn, _ = tmp_messaging_db
        msg = create_message(
            conn, sender="a", recipient="b", type="task", subject="DoThis",
            priority="high", disposition=DISPOSITION_ACTIONABLE,
        )
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "DoThis" in first
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "DoThis" in second  # high + read still blocks
        update_status(conn, msg["id"], "resolved")
        third = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert third == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_null_disposition_treated_actionable(self, tmp_messaging_db):
        """A legacy row with NULL disposition must still block (no silent regression)."""
        conn, _ = tmp_messaging_db
        # Insert a row directly with NULL disposition, bypassing create_message.
        conn.execute(
            """INSERT INTO messages
               (id, thread_id, sender, recipient, type, priority, status,
                subject, created_at, updated_at, disposition)
               VALUES ('legacy-1', 'thr-legacy-1', 'a', 'b', 'task', 'high',
                       'pending', 'LegacyHigh', '2026-04-12T11:59:00+00:00',
                       '2026-04-12T11:59:00+00:00', NULL)""",
        )
        conn.commit()
        result = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "LegacyHigh" in result


class TestMessageDisposition:
    def test_classify_consent_grant_subject(self):
        assert _classify_disposition("result", "consent_grant", []) == DISPOSITION_ACKNOWLEDGEMENT

    def test_classify_consent_callback_tag(self):
        assert _classify_disposition(
            "result", "anything", ["consent-callback", "from-obsidian"]
        ) == DISPOSITION_ACKNOWLEDGEMENT

    def test_classify_consent_notification_response_body(self):
        body = '{"source": "notification_response", "title": "Consent: bundle:task_create"}'
        assert _classify_disposition(
            "result", "notification_response", ["notification-callback", "agent-ingest"], body=body
        ) == DISPOSITION_ACKNOWLEDGEMENT

    def test_classify_terminal_status_is_ack(self):
        assert _classify_disposition("retry_success", "Retry succeeded", ["retry"], status="resolved") == DISPOSITION_ACKNOWLEDGEMENT

    def test_classify_plain_request_is_actionable(self):
        body = '{"source": "notification_response", "title": "Pick a venue?"}'
        assert _classify_disposition(
            "result", "notification_response", ["notification-callback", "agent-ingest"], body=body
        ) == DISPOSITION_ACTIONABLE

    def test_classify_default_actionable(self):
        assert _classify_disposition("task", "Do the thing", []) == DISPOSITION_ACTIONABLE

    def test_create_message_infers_when_unset(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        ack = create_message(conn, sender="obsidian-consent-modal", recipient="b",
                             type="result", subject="consent_grant")
        act = create_message(conn, sender="a", recipient="b", type="task", subject="Real work")
        assert ack["disposition"] == DISPOSITION_ACKNOWLEDGEMENT
        assert act["disposition"] == DISPOSITION_ACTIONABLE

    def test_create_message_explicit_wins(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        # Subject would infer actionable, but explicit acknowledgement overrides.
        msg = create_message(conn, sender="a", recipient="b", type="task",
                            subject="Real work", disposition=DISPOSITION_ACKNOWLEDGEMENT)
        assert msg["disposition"] == DISPOSITION_ACKNOWLEDGEMENT

    def test_reply_defaults_to_acknowledgement(self, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        parent = create_message(conn, sender="a", recipient="b", type="task", subject="Q")
        reply = create_reply(conn, parent["id"], sender="b", body="ok")
        assert reply["disposition"] == DISPOSITION_ACKNOWLEDGEMENT


class TestDispositionMigration:
    def test_migrate_adds_and_backfills_disposition(self, tmp_path):
        """An old-schema DB gains the column and backfills consent rows as ack."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # Old schema: messages table WITHOUT disposition.
        conn.executescript(
            """CREATE TABLE messages (
                   id TEXT PRIMARY KEY, thread_id TEXT, sender TEXT NOT NULL,
                   sender_session TEXT, recipient TEXT NOT NULL, recipient_session TEXT,
                   type TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
                   status TEXT NOT NULL DEFAULT 'pending', subject TEXT NOT NULL,
                   body TEXT, in_reply_to TEXT, created_at TEXT NOT NULL,
                   updated_at TEXT, tags TEXT);
               CREATE TABLE message_reads (
                   message_id TEXT NOT NULL, session_id TEXT NOT NULL,
                   read_at TEXT NOT NULL, PRIMARY KEY (message_id, session_id));"""
        )
        conn.execute(
            "INSERT INTO messages (id, sender, recipient, type, status, subject, created_at) "
            "VALUES ('c1', 'obsidian-consent-modal', 'work-buddy', 'result', 'pending', 'consent_grant', '2026-04-12T11:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO messages (id, sender, recipient, type, status, subject, created_at) "
            "VALUES ('r1', 'a', 'work-buddy', 'task', 'pending', 'Real work', '2026-04-12T11:00:00+00:00')"
        )
        conn.commit()

        _migrate(conn)  # adds column + backfills

        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "disposition" in cols
        c1 = conn.execute("SELECT disposition FROM messages WHERE id='c1'").fetchone()[0]
        r1 = conn.execute("SELECT disposition FROM messages WHERE id='r1'").fetchone()[0]
        assert c1 == DISPOSITION_ACKNOWLEDGEMENT
        assert r1 == DISPOSITION_ACTIONABLE

        # Re-running is a no-op (column already present).
        _migrate(conn)
        assert conn.execute("SELECT disposition FROM messages WHERE id='c1'").fetchone()[0] == DISPOSITION_ACKNOWLEDGEMENT
        conn.close()
