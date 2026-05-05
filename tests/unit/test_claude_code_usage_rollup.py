"""Unit tests for the claude_code_usage daily-rollup pruner."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.artifacts import prune_claude_code_usage_db
from work_buddy.llm.claude_code_usage.rollup import rollup_old_turns
from work_buddy.llm.claude_code_usage.scanner import init_db


def _seed_turns(conn: sqlite3.Connection, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO turns
               (session_id, timestamp, model,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["session_id"], r["timestamp"], r["model"],
                r.get("in", 0), r.get("out", 0),
                r.get("cr", 0), r.get("cc", 0),
                r.get("tool", "Read"), r.get("cwd", "/c/test"),
                r["message_id"],
            ),
        )
    conn.commit()


@pytest.fixture
def usage_db(tmp_path):
    """Fresh claude_code_usage.db with mixed-age rows seeded."""
    db_path = tmp_path / "claude_code_usage.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)

    now = datetime.now(timezone.utc)
    old_day1 = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old_day1_b = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.000Z").replace("T", "T01:")  # same day, different time
    old_day2 = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    boundary = (now - timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # within 90d → KEEP

    _seed_turns(conn, [
        # Old (>90 d) — same (session, day, model) triple, different times — should collapse
        {"session_id": "S1", "timestamp": old_day1,   "model": "sonnet", "in": 10, "out": 5,  "message_id": "m1"},
        {"session_id": "S1", "timestamp": old_day1_b, "model": "sonnet", "in": 20, "out": 8,  "message_id": "m2"},
        # Old, different day
        {"session_id": "S1", "timestamp": old_day2,   "model": "sonnet", "in": 30, "out": 9,  "message_id": "m3"},
        # Old, different model → separate group
        {"session_id": "S1", "timestamp": old_day2,   "model": "opus",   "in": 40, "out": 11, "message_id": "m4"},
        # Old, different session
        {"session_id": "S2", "timestamp": old_day1,   "model": "haiku",  "in": 50, "out": 14, "message_id": "m5"},
        # Recent — must NOT roll up
        {"session_id": "S3", "timestamp": recent,     "model": "sonnet", "in": 60, "out": 16, "message_id": "m6"},
        # Boundary (89 d) — must NOT roll up
        {"session_id": "S3", "timestamp": boundary,   "model": "sonnet", "in": 70, "out": 18, "message_id": "m7"},
    ])
    conn.close()
    yield db_path


def test_dry_run_counts_groups_no_changes(usage_db):
    """Dry-run reports group count but leaves rows intact."""
    result = prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=True)
    # 4 distinct (session, day, model) triples in the >90d set:
    # (S1, day1, sonnet), (S1, day2, sonnet), (S1, day2, opus), (S2, day1, haiku)
    assert result["rollup_groups"] == 4
    assert result["rolled_turns"] == -1  # dry-run sentinel

    conn = sqlite3.connect(str(usage_db))
    n_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    n_daily = conn.execute("SELECT COUNT(*) FROM turns_daily").fetchone()[0]
    conn.close()
    assert n_turns == 7  # nothing deleted
    assert n_daily == 0  # nothing rolled


def test_live_run_collapses_and_deletes(usage_db):
    """Live run aggregates correctly and deletes the originals."""
    result = prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=False)
    assert result["rollup_groups"] == 4
    assert result["rolled_turns"] == 5  # 5 old rows deleted

    conn = sqlite3.connect(str(usage_db))
    surviving_turns = conn.execute(
        "SELECT message_id FROM turns ORDER BY message_id"
    ).fetchall()
    conn.close()
    # Only the recent + boundary rows survive
    assert [r[0] for r in surviving_turns] == ["m6", "m7"]


def test_aggregation_sums_correctly(usage_db):
    """The two same-(session,day,model) old rows get summed in turns_daily."""
    prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=False)
    conn = sqlite3.connect(str(usage_db))
    # S1 + day1 + sonnet had two turns: in=10+20=30, out=5+8=13, count=2
    row = conn.execute(
        """SELECT input_tokens, output_tokens, turn_count
           FROM turns_daily
           WHERE session_id='S1' AND model='sonnet'
           ORDER BY day DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    # Latest day1 — but we have two old days for S1+sonnet. ORDER BY day DESC
    # gives day2 (more recent of the two olds). day2 had 1 row: in=30, out=9.
    assert row == (30, 9, 1)

    conn = sqlite3.connect(str(usage_db))
    row_day1 = conn.execute(
        """SELECT input_tokens, output_tokens, turn_count
           FROM turns_daily
           WHERE session_id='S1' AND model='sonnet'
           ORDER BY day ASC LIMIT 1"""
    ).fetchone()
    conn.close()
    # day1 had two rows: in=10+20=30, out=5+8=13, count=2
    assert row_day1 == (30, 13, 2)


def test_recent_rows_untouched(usage_db):
    """Rows within the 90-day window are completely untouched."""
    prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=False)
    conn = sqlite3.connect(str(usage_db))
    recent_row = conn.execute(
        "SELECT input_tokens, output_tokens FROM turns WHERE message_id='m6'"
    ).fetchone()
    boundary_row = conn.execute(
        "SELECT input_tokens, output_tokens FROM turns WHERE message_id='m7'"
    ).fetchone()
    conn.close()
    assert recent_row == (60, 16)
    assert boundary_row == (70, 18)


def test_idempotent_rerun(usage_db):
    """Running the rollup twice produces the same final state (no double-counting)."""
    prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=False)
    snapshot_after_first = sqlite3.connect(str(usage_db)).execute(
        "SELECT session_id, day, model, input_tokens, output_tokens, turn_count "
        "FROM turns_daily ORDER BY session_id, day, model"
    ).fetchall()

    # Second run: nothing eligible (originals already deleted), so nothing changes.
    result2 = prune_claude_code_usage_db(usage_db, {"days_to_keep_full": 90}, dry_run=False)
    assert result2["rollup_groups"] == 0
    assert result2["rolled_turns"] == 0

    snapshot_after_second = sqlite3.connect(str(usage_db)).execute(
        "SELECT session_id, day, model, input_tokens, output_tokens, turn_count "
        "FROM turns_daily ORDER BY session_id, day, model"
    ).fetchall()
    assert snapshot_after_first == snapshot_after_second


def test_empty_db_safe(tmp_path):
    """Pruning an empty DB is a no-op."""
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    init_db(conn)
    conn.close()
    result = prune_claude_code_usage_db(db_path, {"days_to_keep_full": 90}, dry_run=False)
    assert result["rollup_groups"] == 0
    assert result["rolled_turns"] == 0


def test_missing_db_safe(tmp_path):
    """Pruning a missing DB returns zero counts, doesn't raise."""
    nope = tmp_path / "does-not-exist.db"
    result = prune_claude_code_usage_db(nope, {"days_to_keep_full": 90}, dry_run=False)
    assert result == {"rollup_groups": 0, "rolled_turns": 0, "bytes_before": 0, "bytes_after": 0}
