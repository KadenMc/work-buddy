"""Retention tests for the live messages artifact.

The age-based sweep keeps a pending row only while it is an *actionable* action
item; acknowledgement-disposition rows (auto-acks of in-band-handled work) are
allowed to reap on the normal TTL even while pending, instead of accumulating
forever. Resolved/terminal rows of any disposition reap on the TTL.

These exercise the live ``_messages_retention`` predicate and the artifact it is
wired into — distinct from the standalone ``prune_messages_db`` pruner.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.artifacts import (
    Artifact,
    Delete,
    Lifecycle,
    PerRecordTtl,
    SessionTagged,
    SqliteRowsStorage,
)
from work_buddy.messaging.models import (
    DISPOSITION_ACKNOWLEDGEMENT,
    DISPOSITION_ACTIONABLE,
    _messages_retention,
    get_connection,
)


# ---------------------------------------------------------------------------
# Predicate (the rule)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,disposition,kept",
    [
        ("pending", DISPOSITION_ACTIONABLE, True),       # unresolved action item
        ("pending", DISPOSITION_ACKNOWLEDGEMENT, False),  # ack reaps even while pending
        ("pending", None, True),                          # missing -> conservative keep
        ("resolved", DISPOSITION_ACTIONABLE, False),      # terminal never kept
        ("resolved", DISPOSITION_ACKNOWLEDGEMENT, False),
        ("read", DISPOSITION_ACTIONABLE, False),          # any non-pending is terminal here
    ],
)
def test_retention_predicate(status, disposition, kept):
    assert _messages_retention({"status": status, "disposition": disposition}) is kept


# ---------------------------------------------------------------------------
# Artifact wiring (end-to-end prune on a real DB)
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "messages.db"
    import work_buddy.messaging.models as mmod
    original = mmod._db_path
    mmod._db_path = lambda c=None: db_path
    try:
        get_connection().close()  # create schema
    finally:
        mmod._db_path = original
    return db_path


def _messages_artifact(db_path: Path) -> Artifact:
    """Mirror the live registration (``_register_messages_artifact``) against a
    temp DB so the prune exercises the real predicate + storage."""
    return Artifact(
        name="messages-test",
        storage=SqliteRowsStorage(
            db_path=db_path,
            table="messages",
            id_column="id",
            post_delete_sql=[
                "DELETE FROM message_reads "
                "WHERE message_id NOT IN (SELECT id FROM messages)"
            ],
            vacuum_on_delete=True,
        ),
        lifecycle=Lifecycle(
            trigger=PerRecordTtl(ttl_field="created_at", default_ttl_days=30),
            action=Delete(),
            retention_predicate=_messages_retention,
        ),
        provenance=SessionTagged(
            session_columns=["sender_session", "recipient_session"],
        ),
    )


def _seed(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            """INSERT INTO messages (id, sender, recipient, type, status, subject,
                                     created_at, disposition)
               VALUES (?, 'test', 'x', 'result', ?, 's', ?, ?)""",
            (r["id"], r["status"], r["created_at"], r["disposition"]),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = _fresh_db(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    _seed(db_path, [
        {"id": "old-pend-actionable", "status": "pending", "created_at": old, "disposition": DISPOSITION_ACTIONABLE},
        {"id": "old-pend-ack",        "status": "pending", "created_at": old, "disposition": DISPOSITION_ACKNOWLEDGEMENT},
        {"id": "old-pend-null",       "status": "pending", "created_at": old, "disposition": None},
        {"id": "old-resolved-ack",    "status": "resolved", "created_at": old, "disposition": DISPOSITION_ACKNOWLEDGEMENT},
        {"id": "recent-pend-ack",     "status": "pending", "created_at": recent, "disposition": DISPOSITION_ACKNOWLEDGEMENT},
        {"id": "recent-pend-actionable", "status": "pending", "created_at": recent, "disposition": DISPOSITION_ACTIONABLE},
    ])
    return db_path


def test_sweep_reaps_old_acknowledgement_pending_but_keeps_actionable(seeded_db):
    """The core R1 behaviour: an old acknowledgement-pending row (which the old
    keep-all-pending rule pinned forever) now reaps, while actionable-pending and
    within-TTL rows survive."""
    artifact = _messages_artifact(seeded_db)
    artifact.prune(dry_run=False)

    conn = sqlite3.connect(str(seeded_db))
    survivors = {row[0] for row in conn.execute("SELECT id FROM messages")}
    conn.close()

    assert survivors == {
        "old-pend-actionable",     # actionable + pending -> kept forever
        "old-pend-null",           # missing disposition -> conservative keep
        "recent-pend-ack",         # ack but within TTL -> not yet expired
        "recent-pend-actionable",  # within TTL
    }
    # The behaviour change: this one is gone (was immortal under keep-all-pending).
    assert "old-pend-ack" not in survivors


def test_sweep_dry_run_keeps_everything(seeded_db):
    artifact = _messages_artifact(seeded_db)
    artifact.prune(dry_run=True)
    conn = sqlite3.connect(str(seeded_db))
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert n == 6
