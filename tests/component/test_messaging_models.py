"""Component tests for messaging SQLite models — schema, CRUD, read tracking, TTL."""

import pytest
from datetime import datetime, timezone, timedelta
from freezegun import freeze_time

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
    def test_nonblocking_callback_never_blocks_stop_hook(self, tmp_messaging_db):
        """A consent-callback message must never block the Stop hook — not even once."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="obsidian-consent-modal", recipient="b",
            type="result", subject="consent_grant", priority="high",
            tags=["consent-callback", "from-obsidian"],
        )
        # Excluded on the very first render (no unread tax) and on every render after.
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert first == ""
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert second == ""

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_untagged_high_priority_still_blocks(self, tmp_messaging_db):
        """The discriminator is the tag, not the priority/subject: untagged high still blocks."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="obsidian-consent-modal", recipient="b",
            type="result", subject="consent_grant", priority="high",
        )
        first = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "consent_grant" in first  # surfaces, auto-marks read
        # High priority keeps blocking after read (would only release via resolve).
        second = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=True)
        assert "consent_grant" in second

    @freeze_time("2026-04-12T12:00:00+00:00")
    def test_nonblocking_callback_visible_in_context_summary(self, tmp_messaging_db):
        """Excluded only from the Stop-hook path: non-blocking summaries still show it."""
        conn, _ = tmp_messaging_db
        create_message(
            conn, sender="obsidian-consent-modal", recipient="b",
            type="result", subject="consent_grant", priority="high",
            tags=["consent-callback", "from-obsidian"],
        )
        # unread_only=False (SessionStart / UserPromptSubmit) keeps it as context.
        summary = summarize_pending(conn, "b", session="s1", include_instructions=False, unread_only=False)
        assert "consent_grant" in summary
