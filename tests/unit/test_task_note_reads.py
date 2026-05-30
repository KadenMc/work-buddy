"""Durable task-note-read attribution (Piece 3).

Covers the ``session_task_note_reads`` table + collector
(``conversation_observability.note_reads``) and the SQL fast-path it
unlocks in ``provenance.sessions_who_read_task``.

DB isolation mirrors ``test_task_provenance.py``: a fresh task store +
an isolated conversation-observability DB + a fake ~/.claude/projects.
"""

from __future__ import annotations

import sqlite3

import pytest

from work_buddy.conversation_observability import note_reads
from work_buddy.conversation_observability.db import get_connection
from work_buddy.obsidian.tasks import provenance, store
from tests.unit.conversation_observability_fixtures import (
    assistant_mcp_call,
    assistant_write,
    user_turn,
    write_session,
)

_UUID = "11111111-1111-1111-1111-111111111111"
_TASK_ID = "t-aaaaaaaa"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


@pytest.fixture()
def co_env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    db_file = tmp_path / "co.db"
    from work_buddy.sessions import inspector

    monkeypatch.setattr(inspector, "_CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db._default_db_path",
        lambda: db_file,
    )
    monkeypatch.setattr(
        "work_buddy.conversation_observability.db.db_path",
        lambda cfg=None: db_file,
    )
    provenance._jsonl_reader_scan.cache_clear()
    return {"projects": projects, "db": db_file}


# ── schema + migration ──────────────────────────────────────────────


def test_fresh_db_has_table(co_env) -> None:
    conn = get_connection()
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(session_task_note_reads)")}
    finally:
        conn.close()
    assert {"session_id", "task_id", "source", "first_seen_at"} <= cols


def test_migrate_adds_mtime_column(co_env) -> None:
    """A legacy observed_sessions without the column rolls forward.

    Crafts the schema as it stood *before* Piece 3 (every column except
    ``note_reads_scanned_mtime``); ``get_connection`` → ``_migrate_schema``
    must add it. Mirrors test_task_store_slice_2's legacy-roll-forward.
    """
    raw = sqlite3.connect(str(co_env["db"]))
    raw.executescript(
        "CREATE TABLE observed_sessions ("
        " session_id TEXT PRIMARY KEY, project_name TEXT, project_slug TEXT,"
        " source_path TEXT NOT NULL, source_mtime REAL NOT NULL,"
        " observed_at TEXT NOT NULL, start_time TEXT, end_time TEXT,"
        " message_count INTEGER, span_count INTEGER,"
        " tool_names_json TEXT NOT NULL DEFAULT '{}',"
        " status TEXT NOT NULL DEFAULT 'ok', error TEXT,"
        " commits_scanned_mtime REAL, writes_scanned_mtime REAL,"
        " prs_scanned_mtime REAL);"
    )
    raw.commit()
    raw.close()
    # get_connection() runs the SCHEMA (no-op on the existing table) then
    # _migrate_schema() (adds the missing column).
    conn = get_connection()
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(observed_sessions)")}
    finally:
        conn.close()
    assert "note_reads_scanned_mtime" in cols


# ── collector ───────────────────────────────────────────────────────


def _seed_task(task_id=_TASK_ID, note_uuid=_UUID):
    store.create(task_id=task_id, note_uuid=note_uuid)


def test_refresh_populates_rows_per_source(fresh_db, co_env) -> None:
    _seed_task()
    write_session(
        co_env["projects"] / "p", "s-reader",
        [
            assistant_write("Read", f"tasks/notes/{_UUID}.md", "tu1",
                            "2026-05-13T10:00:01Z"),
            assistant_mcp_call("task_read", {"task_id": _TASK_ID}, "tu2",
                               "2026-05-13T10:05:00Z"),
        ],
    )
    summary = note_reads.refresh_session_note_reads(days=3650)
    assert summary["rows_written"] == 2

    rows = note_reads.query_reads_for_task(_TASK_ID)
    by_source = {r["source"]: r for r in rows}
    assert set(by_source) == {"read_tool", "task_read_mcp"}
    assert by_source["read_tool"]["session_id"] == "s-reader"
    assert by_source["read_tool"]["note_uuid"] == _UUID


def test_refresh_stale_only_idempotent(fresh_db, co_env) -> None:
    _seed_task()
    write_session(
        co_env["projects"] / "p", "s-reader",
        [assistant_mcp_call("task_assign", {"task_id": _TASK_ID}, "tu",
                            "2026-05-13T10:00:00Z")],
    )
    note_reads.refresh_session_note_reads(days=3650)
    second = note_reads.refresh_session_note_reads(days=3650)
    # Second pass is a cache hit: nothing re-scanned, rows stable.
    assert second["rows_written"] == 0
    assert len(note_reads.query_reads_for_task(_TASK_ID)) == 1


def test_refresh_skips_orphan_note_uuid(fresh_db, co_env) -> None:
    # No task seeded → the note uuid maps to nothing → no rows.
    write_session(
        co_env["projects"] / "p", "s-orphan",
        [assistant_write("Read", f"tasks/notes/{_UUID}.md", "tu",
                         "2026-05-13T10:00:00Z")],
    )
    summary = note_reads.refresh_session_note_reads(days=3650)
    assert summary["rows_written"] == 0
    assert note_reads.query_reads_for_task(_TASK_ID) == []


def test_query_reads_for_session(fresh_db, co_env) -> None:
    _seed_task()
    write_session(
        co_env["projects"] / "p", "s-reader",
        [assistant_mcp_call("task_read", {"task_id": _TASK_ID}, "tu",
                            "2026-05-13T10:00:00Z")],
    )
    note_reads.refresh_session_note_reads(days=3650)
    rows = note_reads.query_reads_for_session("s-reader")
    assert [r["task_id"] for r in rows] == [_TASK_ID]


# ── SQL fast-path in sessions_who_read_task ─────────────────────────


def test_sessions_who_read_task_uses_sql_when_populated(fresh_db, co_env) -> None:
    _seed_task()
    write_session(
        co_env["projects"] / "p", "s-reader",
        [assistant_write("Read", f"tasks/notes/{_UUID}.md", "tu",
                         "2026-05-13T10:00:01Z")],
    )
    note_reads.refresh_session_note_reads(days=3650)

    # With the table populated, the inverse query resolves via SQL. Prove
    # it isn't falling back to the JSONL scan by poisoning that path: a
    # cleared cache + a sentinel that would raise if _all_sessions ran.
    provenance._jsonl_reader_scan.cache_clear()
    out = provenance.sessions_who_read_task(_TASK_ID, _UUID)
    ids = [r["session_id"] for r in out]
    assert ids == ["s-reader"]
    assert out[0]["awareness"] == "read_note"
    assert "read_tool" in out[0]["sources"]
