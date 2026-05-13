"""DB / schema / artifact registration for conversation_observability."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: redirect the DB to a temp file
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_co_db(tmp_path, monkeypatch):
    """Point conversation_observability.db at a fresh file under ``tmp_path``."""
    db_file = tmp_path / "co.db"
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path",
        lambda: db_file,
    )
    # Also intercept the cfg path — load_config might return a stale
    # explicit db_path from config.local.yaml.
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path",
        lambda cfg=None: db_file,
    )
    return db_file


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def test_get_connection_creates_all_five_tables(tmp_co_db) -> None:
    from work_buddy.conversation_observability.db import get_connection

    conn = get_connection()
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()

    assert {
        "observed_sessions",
        "session_commits",
        "session_file_writes",
        "topic_summaries",
        "session_summaries",
    }.issubset(tables)


def test_get_connection_is_idempotent(tmp_co_db) -> None:
    """Second connect must not fail or duplicate schema."""
    from work_buddy.conversation_observability.db import get_connection

    conn1 = get_connection()
    conn1.close()
    # Insert a row, then re-connect — must still be present.
    conn2 = get_connection()
    try:
        conn2.execute(
            "INSERT INTO observed_sessions "
            "(session_id, source_path, source_mtime, observed_at) "
            "VALUES (?, ?, ?, ?)",
            ("sid1", "/fake/path.jsonl", 1234.0, "2026-05-13T00:00:00Z"),
        )
        conn2.commit()
    finally:
        conn2.close()

    conn3 = get_connection()
    try:
        rows = conn3.execute(
            "SELECT session_id FROM observed_sessions"
        ).fetchall()
    finally:
        conn3.close()
    assert [r["session_id"] for r in rows] == ["sid1"]


def test_schema_enables_wal(tmp_co_db) -> None:
    from work_buddy.conversation_observability.db import get_connection

    conn = get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# Artifact registration
# ---------------------------------------------------------------------------


def test_artifact_appears_in_registry_after_import(tmp_co_db) -> None:
    """Importing conversation_observability registers the artifact.

    The registration runs at package-import time. We trigger a fresh
    registration call because the package may have been imported with
    a different (real) DB path during prior test collection.
    """
    from work_buddy.artifacts import list_artifact_names
    from work_buddy.conversation_observability.artifacts import (
        register_conversation_observability_artifact,
    )

    register_conversation_observability_artifact()
    assert "conversation-observability" in list_artifact_names()


def test_artifact_post_delete_cascades_child_rows(tmp_co_db) -> None:
    """Removing an observed_sessions row drops dependent rows in the
    same transaction via post_delete_sql.
    """
    from work_buddy.artifacts import get_artifact
    from work_buddy.conversation_observability.artifacts import (
        register_conversation_observability_artifact,
    )
    from work_buddy.conversation_observability.db import get_connection

    register_conversation_observability_artifact()

    # Seed parent + children.
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO observed_sessions "
            "(session_id, source_path, source_mtime, observed_at) "
            "VALUES (?, ?, ?, ?)",
            ("doomed", "/fake/d.jsonl", 1.0, "2026-05-13T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO session_commits "
            "(sha, short_sha, session_id, observed_at) VALUES (?, ?, ?, ?)",
            ("deadbeef0", "deadbee", "doomed", "2026-05-13T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO session_file_writes "
            "(id, session_id, file_path, tool_name, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("w1", "doomed", "/repo/a.py", "Write", "2026-05-13T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topic_summaries "
            "(id, session_id, topic_index, title, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t1", "doomed", 0, "topic", "body"),
        )
        conn.execute(
            "INSERT INTO session_summaries "
            "(session_id, tldr, generated_at, prompt_version, "
            " summary_schema_version, selection_version, cache_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("doomed", "tldr text", "2026-05-13T00:00:00Z", 1, 1, 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    # Delete the parent via the artifact API.
    artifact = get_artifact("conversation-observability")
    ref = next(
        r
        for r in (
            artifact.storage.ref_for(rec)
            for rec in artifact.storage.iter_records()
        )
        if r.id == "doomed"
    )
    artifact.storage.delete_record(ref)

    # All children should be gone too.
    conn = get_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_commits WHERE session_id='doomed'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_file_writes WHERE session_id='doomed'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM topic_summaries WHERE session_id='doomed'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_summaries WHERE session_id='doomed'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_artifact_uses_infinite_lifecycle(tmp_co_db) -> None:
    """The registration opts into infinite retention."""
    from work_buddy.artifacts import NeverExpires, get_artifact
    from work_buddy.conversation_observability.artifacts import (
        register_conversation_observability_artifact,
    )

    register_conversation_observability_artifact()
    artifact = get_artifact("conversation-observability")
    assert isinstance(artifact.lifecycle.trigger, NeverExpires)
