"""Tests for the lww_meta sidecar — SqliteLwwLog + the v10/v7 migrations.

Covers:
- The task (v10) and projects (v7) migration ladders both create the
  lww_meta table and are idempotent.
- SqliteLwwLog record / latest / history round-trip, including the
  actor OR-set surviving JSON serialization.
- A MarkdownDB wired with a real SqliteLwwLog resolves drift by
  timestamp (store-wins when the store has the newer write).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.markdown_db import SqliteLwwLog, WriteProvenance
from work_buddy.markdown_db.sqlite_lww import ensure_lww_meta

# Reuse the toy subclass + store from the base-class test module.
from tests.unit.test_markdown_db_base import ToyMarkdownDB, ToyStore, _seed_master


# ════════════════════════════════════════════════════════════════════
# Migration ladders create lww_meta
# ════════════════════════════════════════════════════════════════════


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_task_migration_v10_creates_lww_meta(tmp_path: Path) -> None:
    from work_buddy.obsidian.tasks.migrations import TASK_MIGRATIONS

    conn = sqlite3.connect(str(tmp_path / "task.db"))
    try:
        TASK_MIGRATIONS.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "lww_meta" in _tables(conn)
        # Idempotent: a second run is a clean no-op.
        TASK_MIGRATIONS.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        conn.close()


def test_projects_migration_v7_creates_lww_meta(tmp_path: Path) -> None:
    from work_buddy.projects.migrations import PROJECT_MIGRATIONS

    conn = sqlite3.connect(str(tmp_path / "projects.db"))
    try:
        PROJECT_MIGRATIONS.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "lww_meta" in _tables(conn)
        PROJECT_MIGRATIONS.run(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════
# SqliteLwwLog round-trip
# ════════════════════════════════════════════════════════════════════


def _factory(db_path: Path):
    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(str(db_path))
    return _connect


def test_sqlite_lww_record_and_latest(tmp_path: Path) -> None:
    log = SqliteLwwLog(_factory(tmp_path / "x.db"))
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=2)

    log.record(
        table="projects", pk="p1", field="name", ts=t0,
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
        to_surface="store",
    )
    log.record(
        table="projects", pk="p1", field="name", ts=t1,
        provenance=WriteProvenance.drift(),
        to_surface="store",
    )

    entry = log.latest(table="projects", pk="p1", field="name", surface="store")
    assert entry is not None
    assert entry.ts == t1                       # newest wins
    assert entry.provenance.process == "drift"
    # Different surface → no entry.
    assert log.latest(
        table="projects", pk="p1", field="name", surface="markdown",
    ) is None


def test_sqlite_lww_actor_orset_survives_roundtrip(tmp_path: Path) -> None:
    log = SqliteLwwLog(_factory(tmp_path / "x.db"))
    ts = datetime(2026, 3, 3, tzinfo=timezone.utc)
    log.record(
        table="t", pk="p", field="f", ts=ts,
        provenance=WriteProvenance(
            actor=frozenset({"user", "agent"}), process="drift",
        ),
        to_surface="store",
    )
    entry = log.latest(table="t", pk="p", field="f", surface="store")
    assert entry is not None
    assert entry.provenance.actor == frozenset({"user", "agent"})

    # Empty OR-set (honest unknown) round-trips as empty.
    log.record(
        table="t", pk="p", field="f2", ts=ts,
        provenance=WriteProvenance(actor=frozenset(), process="drift"),
        to_surface="store",
    )
    e2 = log.latest(table="t", pk="p", field="f2", surface="store")
    assert e2 is not None and e2.provenance.actor == frozenset()


def test_sqlite_lww_history_is_append_only(tmp_path: Path) -> None:
    log = SqliteLwwLog(_factory(tmp_path / "x.db"))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        log.record(
            table="t", pk="p", field="f", ts=base + timedelta(hours=i),
            provenance=WriteProvenance.drift(), to_surface="store",
        )
    hist = log.history(table="t", pk="p", field="f")
    assert len(hist) == 5                       # every write retained
    # Oldest-first ordering.
    assert hist[0]["ts"] < hist[-1]["ts"]


def test_ensure_lww_meta_idempotent(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    try:
        ensure_lww_meta(conn)
        ensure_lww_meta(conn)               # no error on second call
        assert "lww_meta" in _tables(conn)
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════
# MarkdownDB drift resolution with a real SqliteLwwLog
# ════════════════════════════════════════════════════════════════════


def test_markdowndb_with_sqlite_lww_store_wins_by_timestamp(
    tmp_path: Path,
) -> None:
    """A store write newer than the markdown mtime wins the drift, and
    the store value is written back into the markdown."""
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Old MD Name | active | n"])
    store = ToyStore()
    store.create("p1", name="Fresh Store Name", status="active", note="n")

    log = SqliteLwwLog(_factory(tmp_path / "lww.db"))
    future = datetime.now(timezone.utc) + timedelta(days=1)
    log.record(
        table="toy", pk="p1", field="name", ts=future,
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
        to_surface="store",
    )
    db = ToyMarkdownDB(master, store, lww=log)

    report = db.reconcile_drift()

    assert report.drift["name"][0]["winner"] == "store"
    parsed = db.parse_all_from_markdown()
    assert parsed["p1"].fields["name"] == "Fresh Store Name"


def test_markdowndb_apply_mutation_records_to_sqlite_lww(
    tmp_path: Path,
) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, [])
    store = ToyStore()
    log = SqliteLwwLog(_factory(tmp_path / "lww.db"))
    db = ToyMarkdownDB(master, store, lww=log)

    db.apply_mutation(
        "p1", {"name": "Proj", "status": "active", "note": "x"},
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
    )

    # 3 fields recorded on both surfaces.
    for field in ("name", "status", "note"):
        for surface in ("markdown", "store"):
            entry = log.latest(
                table="toy", pk="p1", field=field, surface=surface,
            )
            assert entry is not None, f"missing lww for {field}/{surface}"
            assert entry.provenance.process == "mutation"
            assert entry.provenance.actor == frozenset({"user"})
