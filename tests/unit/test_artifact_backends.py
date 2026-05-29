"""Structural tests for the six storage backends.

These exercise the Storage protocol surface (capabilities,
iter_records, ref_for, delete_record, delete_where, size_bytes) on
each backend — proves that construction works, the file/db format is
read correctly, and capability declarations match implementation
behavior.

End-to-end pruning smoke tests (with a real Lifecycle composing a
Trigger + ExpiryAction) live in ``test_artifact_protocol.py``, added in
Phase C.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from work_buddy.artifacts import (
    StorageTrait,
    DirectoryTreeStorage,
    DirShape,
    FilesystemStorage,
    JsonRecordsShape,
    JsonRecordsStorage,
    JsonlStorage,
    SqliteRollupStorage,
    SqliteRowsStorage,
)


# ---------------------------------------------------------------------------
# FilesystemStorage
# ---------------------------------------------------------------------------


def test_filesystem_storage_capabilities() -> None:
    storage = FilesystemStorage(data_root=Path("/tmp/non-existent-test"))
    assert StorageTrait.ATOMIC_BLOBS in storage.capabilities
    assert StorageTrait.LISTABLE in storage.capabilities
    assert StorageTrait.DELETABLE in storage.capabilities
    assert StorageTrait.RECORDS not in storage.capabilities


def test_filesystem_storage_iter_and_size(tmp_path: Path) -> None:
    storage = FilesystemStorage(data_root=tmp_path)
    rec = storage.save("hello", "scratch", "test-iter", "txt")
    records = list(storage.iter_records())
    assert len(records) == 1
    assert records[0]["id"] == rec.id
    assert storage.size_bytes() > 0


def test_filesystem_storage_delete_record(tmp_path: Path) -> None:
    storage = FilesystemStorage(data_root=tmp_path)
    rec = storage.save("payload", "scratch", "test-del", "txt")
    ref = storage.ref_for(next(iter(storage.iter_records())))
    bytes_freed = storage.delete_record(ref)
    assert bytes_freed > 0
    assert list(storage.iter_records()) == []


# ---------------------------------------------------------------------------
# SqliteRowsStorage
# ---------------------------------------------------------------------------


def _make_sqlite_db(path: Path, table: str = "items") -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            f"CREATE TABLE {table} ("
            f"id TEXT PRIMARY KEY, status TEXT, created_at TEXT)"
        )
        conn.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?)",
            [
                ("a", "pending", "2026-05-01T00:00:00"),
                ("b", "done", "2026-05-02T00:00:00"),
                ("c", "done", "2026-05-03T00:00:00"),
            ],
        )
        conn.commit()


def test_sqlite_rows_capabilities(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    storage = SqliteRowsStorage(db_path=db, table="items")
    assert StorageTrait.RECORDS in storage.capabilities
    assert StorageTrait.TYPED_COLUMNS in storage.capabilities
    assert StorageTrait.BULK_PRUNEABLE in storage.capabilities


def test_sqlite_rows_iter_records(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _make_sqlite_db(db)
    storage = SqliteRowsStorage(db_path=db, table="items")
    records = list(storage.iter_records())
    assert len(records) == 3
    assert {r["id"] for r in records} == {"a", "b", "c"}


def test_sqlite_rows_delete_where(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _make_sqlite_db(db)
    storage = SqliteRowsStorage(db_path=db, table="items", vacuum_on_delete=False)
    n, _bytes = storage.delete_where(lambda r: r["status"] == "done")
    assert n == 2
    remaining = list(storage.iter_records())
    assert len(remaining) == 1
    assert remaining[0]["id"] == "a"


def test_sqlite_rows_delete_record(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _make_sqlite_db(db)
    storage = SqliteRowsStorage(db_path=db, table="items", vacuum_on_delete=False)
    record = next(r for r in storage.iter_records() if r["id"] == "b")
    ref = storage.ref_for(record)
    storage.delete_record(ref)
    assert {r["id"] for r in storage.iter_records()} == {"a", "c"}


# ---------------------------------------------------------------------------
# JsonRecordsStorage
# ---------------------------------------------------------------------------


def test_json_records_dict_shape(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"k1": {"value": 1}, "k2": {"value": 2}}))
    storage = JsonRecordsStorage(path=path, shape=JsonRecordsShape.DICT)
    records = list(storage.iter_records())
    assert len(records) == 2
    keys = {r["_key"] for r in records}
    assert keys == {"k1", "k2"}


def test_json_records_dict_delete_where(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"k1": {"value": 1}, "k2": {"value": 2}, "k3": {"value": 3}}))
    storage = JsonRecordsStorage(path=path, shape=JsonRecordsShape.DICT)
    n, _bytes = storage.delete_where(lambda r: r["value"] >= 2)
    assert n == 2
    remaining = json.loads(path.read_text())
    assert list(remaining.keys()) == ["k1"]


def test_json_records_list_shape(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps([{"v": 1}, {"v": 2}, {"v": 3}]))
    storage = JsonRecordsStorage(path=path, shape=JsonRecordsShape.LIST)
    records = list(storage.iter_records())
    assert len(records) == 3


def test_json_records_list_wrapped_shape(tmp_path: Path) -> None:
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"snapshots": [{"v": 1}, {"v": 2}]}))
    storage = JsonRecordsStorage(
        path=path,
        shape=JsonRecordsShape.LIST_WRAPPED,
        wrapper_key="snapshots",
    )
    records = list(storage.iter_records())
    assert len(records) == 2


def test_json_records_list_wrapped_requires_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JsonRecordsStorage(
            path=tmp_path / "x.json",
            shape=JsonRecordsShape.LIST_WRAPPED,
            wrapper_key=None,
        )


# ---------------------------------------------------------------------------
# JsonlStorage
# ---------------------------------------------------------------------------


def test_jsonl_capabilities(tmp_path: Path) -> None:
    storage = JsonlStorage(path=tmp_path / "log.jsonl")
    assert StorageTrait.RECORDS in storage.capabilities
    assert StorageTrait.APPEND_ONLY in storage.capabilities
    assert StorageTrait.BULK_PRUNEABLE in storage.capabilities


def test_jsonl_iter_records(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"id": "a", "v": 1}) + "\n"
        + json.dumps({"id": "b", "v": 2}) + "\n"
    )
    storage = JsonlStorage(path=path)
    records = list(storage.iter_records())
    assert len(records) == 2


def test_jsonl_delete_where_preserves_malformed(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"id": "a", "v": 1}) + "\n"
        + "not-valid-json\n"
        + json.dumps({"id": "b", "v": 2}) + "\n"
    )
    storage = JsonlStorage(path=path, preserve_malformed_lines=True)
    n, _bytes = storage.delete_where(lambda r: r.get("v", 0) >= 2)
    assert n == 1
    text = path.read_text()
    assert "not-valid-json" in text
    assert '"id": "a"' in text
    assert '"id": "b"' not in text


# ---------------------------------------------------------------------------
# SqliteRollupStorage
# ---------------------------------------------------------------------------


def _make_rollup_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE turns ("
            "id INTEGER PRIMARY KEY, day TEXT, model TEXT, tokens INTEGER)"
        )
        conn.execute(
            "CREATE TABLE turns_daily ("
            "day TEXT, model TEXT, total_tokens INTEGER)"
        )
        conn.executemany(
            "INSERT INTO turns (day, model, tokens) VALUES (?, ?, ?)",
            [("2026-01-01", "sonnet", 100), ("2026-01-01", "sonnet", 200),
             ("2026-05-01", "haiku", 50)],
        )
        conn.commit()


def test_sqlite_rollup_capabilities(tmp_path: Path) -> None:
    storage = SqliteRollupStorage(
        db_path=tmp_path / "x.db", source_table="turns", rollup_table="turns_daily"
    )
    assert StorageTrait.RECORDS in storage.capabilities
    assert StorageTrait.TYPED_COLUMNS in storage.capabilities
    assert StorageTrait.BULK_PRUNEABLE in storage.capabilities


def test_sqlite_rollup_iter_records(tmp_path: Path) -> None:
    db = tmp_path / "rollup.db"
    _make_rollup_db(db)
    storage = SqliteRollupStorage(
        db_path=db, source_table="turns", rollup_table="turns_daily"
    )
    records = list(storage.iter_records())
    assert len(records) == 3


def test_sqlite_rollup_open_connection(tmp_path: Path) -> None:
    db = tmp_path / "rollup.db"
    _make_rollup_db(db)
    storage = SqliteRollupStorage(
        db_path=db, source_table="turns", rollup_table="turns_daily"
    )
    conn = storage.open_connection()
    try:
        count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        assert count == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DirectoryTreeStorage
# ---------------------------------------------------------------------------


def test_directory_tree_session_dirs(tmp_path: Path) -> None:
    sess1 = tmp_path / "20260101-100000_aaaaaaaa"
    sess1.mkdir()
    (sess1 / "manifest.json").write_text(json.dumps({"session_id": "aaaa"}))
    sess2 = tmp_path / "20260202-100000_bbbbbbbb"
    sess2.mkdir()
    (sess2 / "manifest.json").write_text(json.dumps({"session_id": "bbbb"}))
    # Skip dirs without manifest
    (tmp_path / "no_manifest").mkdir()

    storage = DirectoryTreeStorage(root=tmp_path, shape=DirShape.SESSION_DIRS)
    records = list(storage.iter_records())
    assert len(records) == 2
    assert {r["session_id"] for r in records} == {"aaaa", "bbbb"}


def test_directory_tree_log_files(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("line1\nline2\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.log").write_text("nested\n")

    storage = DirectoryTreeStorage(root=tmp_path, shape=DirShape.LOG_FILES)
    records = list(storage.iter_records())
    assert len(records) == 2
    names = {r["_file_name"] for r in records}
    assert names == {"a.log", "b.log"}


def test_directory_tree_json_files(tmp_path: Path) -> None:
    (tmp_path / "n1.json").write_text(json.dumps({"id": "n1", "status": "pending"}))
    (tmp_path / "n2.json").write_text(json.dumps({"id": "n2", "status": "done"}))
    (tmp_path / "skip.txt").write_text("not-json")

    storage = DirectoryTreeStorage(root=tmp_path, shape=DirShape.JSON_FILES)
    records = list(storage.iter_records())
    assert len(records) == 2
    assert {r["id"] for r in records} == {"n1", "n2"}


def test_directory_tree_delete_record_session(tmp_path: Path) -> None:
    sess = tmp_path / "20260101-100000_zzzzzzzz"
    sess.mkdir()
    (sess / "manifest.json").write_text(json.dumps({"session_id": "zz"}))
    (sess / "data.txt").write_text("payload" * 100)

    storage = DirectoryTreeStorage(root=tmp_path, shape=DirShape.SESSION_DIRS)
    record = next(iter(storage.iter_records()))
    ref = storage.ref_for(record)
    bytes_freed = storage.delete_record(ref)
    assert bytes_freed > 0
    assert not sess.exists()
