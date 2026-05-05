"""Unit tests for ``prune_messages_db``."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.artifacts import prune_messages_db
from work_buddy.messaging.models import get_connection


def _seed(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO messages (id, sender, recipient, type, status, subject,
                                     body, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"], r.get("sender", "test"), r.get("recipient", "x"),
                r.get("type", "msg"), r["status"], r.get("subject", "s"),
                r.get("body", "b"), r["created_at"],
            ),
        )
    conn.commit()


def _fresh_db(tmp_path: Path) -> Path:
    """Create a real messaging DB at a temp path with the proper schema."""
    db_path = tmp_path / "messages.db"
    import work_buddy.messaging.models as mmod
    original = mmod._db_path
    mmod._db_path = lambda c=None: db_path
    try:
        conn = get_connection()  # creates tables
        conn.close()
    finally:
        mmod._db_path = original
    return db_path


@pytest.fixture
def messages_db(tmp_path):
    """Yield a fresh messaging DB path with mixed-age + mixed-status rows."""
    db_path = _fresh_db(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    conn = sqlite3.connect(str(db_path))
    _seed(conn, [
        # Old + terminal — should be deleted
        {"id": "old-resolved-1", "status": "resolved", "created_at": old},
        {"id": "old-resolved-2", "status": "resolved", "created_at": old},
        {"id": "old-read-1",     "status": "read",     "created_at": old},
        # Old + pending — must NOT be deleted
        {"id": "old-pending-1",  "status": "pending",  "created_at": old},
        # Recent + terminal — must NOT be deleted
        {"id": "new-resolved-1", "status": "resolved", "created_at": recent},
        # Recent + pending — must NOT be deleted
        {"id": "new-pending-1",  "status": "pending",  "created_at": recent},
    ])
    # Plant a message_reads row pointing at one of the to-be-deleted messages.
    conn.execute(
        """INSERT INTO message_reads (message_id, session_id, reader_project, read_at)
           VALUES (?, ?, ?, ?)""",
        ("old-resolved-1", "sess-A", "test", recent),
    )
    # And one pointing at a kept message (must survive).
    conn.execute(
        """INSERT INTO message_reads (message_id, session_id, reader_project, read_at)
           VALUES (?, ?, ?, ?)""",
        ("new-resolved-1", "sess-B", "test", recent),
    )
    conn.commit()
    conn.close()
    yield db_path


def test_dry_run_counts_but_does_not_delete(messages_db):
    """Dry-run reports the count but leaves the table intact."""
    result = prune_messages_db(messages_db, {"ttl_days": 30}, dry_run=True)
    assert result["pruned"] == 3  # old-resolved-1, old-resolved-2, old-read-1

    conn = sqlite3.connect(str(messages_db))
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert n == 6  # nothing actually deleted


def test_live_run_deletes_only_old_terminal_rows(messages_db):
    """Live run deletes old terminal rows; pending and recent rows survive."""
    bytes_before = messages_db.stat().st_size
    result = prune_messages_db(messages_db, {"ttl_days": 30}, dry_run=False)
    assert result["pruned"] == 3
    assert result["bytes_before"] == bytes_before
    # VACUUM ran — file size should have changed (almost always shrinks)
    assert "bytes_after" in result

    conn = sqlite3.connect(str(messages_db))
    surviving_ids = {row[0] for row in conn.execute("SELECT id FROM messages")}
    conn.close()
    assert surviving_ids == {
        "old-pending-1",     # pending — protected regardless of age
        "new-resolved-1",    # recent — protected by TTL
        "new-pending-1",     # pending + recent — doubly protected
    }


def test_orphaned_message_reads_cleaned(messages_db):
    """message_reads pointing at deleted messages get swept up."""
    prune_messages_db(messages_db, {"ttl_days": 30}, dry_run=False)
    conn = sqlite3.connect(str(messages_db))
    surviving_reads = {row[0] for row in conn.execute("SELECT message_id FROM message_reads")}
    conn.close()
    # 'old-resolved-1' got deleted, so its read entry must be gone too.
    # 'new-resolved-1' survived, so its read entry survives.
    assert surviving_reads == {"new-resolved-1"}


def test_empty_db_is_safe(tmp_path):
    """Pruning an empty DB returns zero counts and does not crash."""
    db_path = _fresh_db(tmp_path)
    result = prune_messages_db(db_path, {"ttl_days": 30}, dry_run=False)
    assert result["pruned"] == 0


def test_custom_ttl_honored(messages_db):
    """Passing ttl_days=1 catches more rows; ttl_days=999 catches none."""
    # ttl_days=999 → cutoff is way in the past → nothing to delete
    r1 = prune_messages_db(messages_db, {"ttl_days": 999}, dry_run=True)
    assert r1["pruned"] == 0

    # ttl_days=1 → cutoff is yesterday → both old AND recent terminal rows go
    # (recent is 5 days old, > 1 day TTL)
    r2 = prune_messages_db(messages_db, {"ttl_days": 1}, dry_run=True)
    # 3 old terminals + 1 new-resolved = 4
    assert r2["pruned"] == 4
