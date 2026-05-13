"""Span-compat check warning paths in sessions/inspector.

Covers the three observable states of ``_check_span_compatibility``
(no-warning / not-in-index / chunk-mismatch), confirms that narrow
runtime failures from the IR store are swallowed so they cannot block
session inspection, and guards that programming errors propagate
instead of being silently absorbed by an over-broad exception clause.
"""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path

import pytest

from work_buddy.ir.sources.base import Document
from work_buddy.ir.store import get_connection, upsert_documents


def _stub_session(session_id: str, span_count: int) -> types.SimpleNamespace:
    """Minimal duck-type for _check_span_compatibility.

    The function reads only ``session_id`` and ``_span_map``, so a
    SimpleNamespace is sufficient and avoids touching the filesystem
    via ``ConversationSession.__init__``.
    """
    return types.SimpleNamespace(
        session_id=session_id,
        _span_map={i: (i, i + 1) for i in range(span_count)},
    )


@pytest.fixture
def tmp_ir_db(tmp_path, monkeypatch):
    """Point ir.store at a fresh SQLite file under ``tmp_path``."""
    db_path = tmp_path / "ir.db"
    monkeypatch.setattr(
        "work_buddy.ir.store._db_path",
        lambda cfg=None: db_path,
    )
    return db_path


def _index_spans(session_id: str, count: int) -> None:
    """Insert ``count`` conversation spans for ``session_id`` into the IR DB."""
    docs = [
        Document(
            doc_id=f"{session_id}:{i}",
            source="conversation",
            fields={"body": f"turn {i}"},
            dense_text=f"turn {i}",
            display_text=f"turn {i}",
        )
        for i in range(count)
    ]
    conn = get_connection()
    try:
        upsert_documents(conn, docs, item_id=f"/fake/{session_id}.jsonl")
    finally:
        conn.close()


def test_check_span_compat_returns_none_when_counts_match(tmp_ir_db) -> None:
    from work_buddy.sessions.inspector import _check_span_compatibility

    _index_spans("aaa", count=3)
    warning = _check_span_compatibility(_stub_session("aaa", span_count=3))
    assert warning is None


def test_check_span_compat_warns_when_session_not_indexed(tmp_ir_db) -> None:
    from work_buddy.sessions.inspector import _check_span_compatibility

    # DB exists but is empty for this session.
    warning = _check_span_compatibility(_stub_session("bbb", span_count=5))
    assert warning is not None
    assert "not in IR index" in warning


def test_check_span_compat_warns_on_chunk_mismatch(tmp_ir_db) -> None:
    from work_buddy.sessions.inspector import _check_span_compatibility

    _index_spans("ccc", count=4)
    warning = _check_span_compatibility(_stub_session("ccc", span_count=7))
    assert warning is not None
    assert "Chunk mismatch" in warning
    assert "4" in warning and "7" in warning


def test_check_span_compat_swallows_sqlite_failure(tmp_ir_db, monkeypatch) -> None:
    """A locked/corrupt IR DB must not block session inspection."""
    from work_buddy.sessions import inspector

    def _broken_get_connection(cfg=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "work_buddy.sessions.inspector.get_connection",
        _broken_get_connection,
    )
    warning = inspector._check_span_compatibility(
        _stub_session("ddd", span_count=2)
    )
    assert warning is None


def test_check_span_compat_propagates_programming_errors(
    tmp_ir_db, monkeypatch
) -> None:
    """Programming errors (e.g. AttributeError from a renamed symbol)
    must propagate. The exception clause inside
    ``_check_span_compatibility`` is intentionally narrow so code
    mistakes surface as test failures rather than silent no-ops.
    """
    from work_buddy.sessions import inspector

    def _bad_load_documents(*args, **kwargs):
        raise AttributeError("simulated typo in caller")

    monkeypatch.setattr(
        "work_buddy.sessions.inspector.load_documents",
        _bad_load_documents,
    )
    with pytest.raises(AttributeError):
        inspector._check_span_compatibility(_stub_session("eee", span_count=1))
