"""``load_documents`` ``metadata_filter`` semantics.

Pins both the legacy scalar-value behavior and the new
list/tuple/set-value behavior added so the dashboard chats view can
pre-filter the IR scoring corpus by an eligible-session_id set.
"""

from __future__ import annotations

import sqlite3

import pytest

from work_buddy.ir.sources.base import Document
from work_buddy.ir.store import (
    get_connection,
    load_documents,
    upsert_documents,
)


@pytest.fixture
def tmp_ir_db(tmp_path, monkeypatch):
    """Point the IR store at a fresh SQLite file under ``tmp_path``."""
    db_file = tmp_path / "ir.db"
    monkeypatch.setattr(
        "work_buddy.ir.store._db_path",
        lambda cfg=None: db_file,
    )
    return db_file


def _seed(conn: sqlite3.Connection) -> None:
    """Insert three conversation docs with distinct session_ids."""
    docs = [
        Document(
            doc_id=f"{sid}:0",
            source="conversation",
            fields={"body": f"hello from {sid}"},
            dense_text=f"hello from {sid}",
            display_text=f"hello from {sid}",
            metadata={"session_id": sid, "project_name": project},
        )
        for sid, project in [
            ("aaa", "alpha"),
            ("bbb", "alpha"),
            ("ccc", "beta"),
        ]
    ]
    upsert_documents(conn, docs, item_id="seed")


# ---------------------------------------------------------------------------
# Scalar-value path (regression)
# ---------------------------------------------------------------------------


def test_scalar_metadata_filter_exact_match(tmp_ir_db) -> None:
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": "aaa"},
        )
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "aaa:0"


def test_scalar_metadata_filter_is_case_insensitive(tmp_ir_db) -> None:
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": "AAA"},
        )
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["metadata"]["session_id"] == "aaa"


# ---------------------------------------------------------------------------
# Set-value path (new behavior)
# ---------------------------------------------------------------------------


def test_list_metadata_filter_matches_any(tmp_ir_db) -> None:
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": ["aaa", "ccc"]},
        )
    finally:
        conn.close()
    sids = sorted(r["metadata"]["session_id"] for r in rows)
    assert sids == ["aaa", "ccc"]


def test_set_metadata_filter_matches_any(tmp_ir_db) -> None:
    """Plain ``set`` value is also accepted (dashboard uses sets)."""
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": {"bbb", "ccc"}},
        )
    finally:
        conn.close()
    sids = sorted(r["metadata"]["session_id"] for r in rows)
    assert sids == ["bbb", "ccc"]


def test_empty_collection_short_circuits_to_no_results(tmp_ir_db) -> None:
    """Empty eligible-set must NOT silently behave like 'no filter'."""
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": []},
        )
    finally:
        conn.close()
    assert rows == []


def test_list_metadata_filter_is_case_insensitive(tmp_ir_db) -> None:
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": ["AAA", "BBB"]},
        )
    finally:
        conn.close()
    sids = sorted(r["metadata"]["session_id"] for r in rows)
    assert sids == ["aaa", "bbb"]


# ---------------------------------------------------------------------------
# Mixed dict (multiple keys ANDed; keys can mix scalar + list)
# ---------------------------------------------------------------------------


def test_mixed_dict_ands_keys_and_supports_both_value_types(tmp_ir_db) -> None:
    """A scalar key + a set key must ANDed correctly."""
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={
                "project_name": "alpha",
                "session_id": ["aaa", "ccc"],
            },
        )
    finally:
        conn.close()
    sids = sorted(r["metadata"]["session_id"] for r in rows)
    # ccc is in the eligible-set but its project is beta → excluded.
    assert sids == ["aaa"]


def test_filter_returns_empty_when_no_metadata_matches(tmp_ir_db) -> None:
    """Set membership against an unknown sid returns nothing without
    error.
    """
    conn = get_connection()
    try:
        _seed(conn)
        rows = load_documents(
            conn,
            source="conversation",
            metadata_filter={"session_id": ["zzz", "yyy"]},
        )
    finally:
        conn.close()
    assert rows == []
