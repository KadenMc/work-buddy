"""SQLite-backed store for inline watchers and invocation history.

Pattern-matches :mod:`work_buddy.conversations.store`: WAL journal, auto-init
on import, row_factory dicts, and CRUD helpers that manage their own
connections when one isn't passed in.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.inline.models import InlineInvocation, PersistentWatcher
from work_buddy.paths import data_dir

logger = logging.getLogger(__name__)


_DB_PATH = data_dir("agents") / "inline.db"

_INVOCATION_RETAIN = 500


def _get_db_path() -> Path:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watchers (
            watcher_id   TEXT PRIMARY KEY,
            command_name TEXT NOT NULL,
            file_path    TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_line     INTEGER,
            params       TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL,
            last_run_at  TEXT,
            schedule     TEXT,
            enabled      INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_watchers_file ON watchers(file_path);
        CREATE INDEX IF NOT EXISTS idx_watchers_cmd  ON watchers(command_name);

        CREATE TABLE IF NOT EXISTS invocations (
            invocation_id TEXT PRIMARY KEY,
            command_name  TEXT NOT NULL,
            surface       TEXT NOT NULL,
            context       TEXT NOT NULL DEFAULT '{}',
            status        TEXT NOT NULL DEFAULT 'pending',
            result        TEXT,
            created_at    TEXT NOT NULL,
            completed_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_invocations_created ON invocations(created_at);
        """
    )


try:  # auto-init
    _c = get_connection()
    _ensure_schema(_c)
    _c.close()
except Exception as e:  # noqa: BLE001
    logger.warning("Inline store schema init failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _own_conn(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    c = get_connection()
    _ensure_schema(c)
    return c, True


# ---------------------------------------------------------------------------
# Watcher CRUD
# ---------------------------------------------------------------------------


def create_watcher(
    command_name: str,
    file_path: str,
    tag: str,
    tag_line: int | None = None,
    params: dict | None = None,
    schedule: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> PersistentWatcher:
    c, own = _own_conn(conn)
    try:
        w = PersistentWatcher(
            watcher_id=_new_id(),
            command_name=command_name,
            file_path=file_path,
            tag=tag,
            tag_line=tag_line,
            params=params or {},
            created_at=_now(),
            schedule=schedule,
            enabled=True,
        )
        c.execute(
            """INSERT INTO watchers
               (watcher_id, command_name, file_path, tag, tag_line,
                params, created_at, last_run_at, schedule, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                w.watcher_id,
                w.command_name,
                w.file_path,
                w.tag,
                w.tag_line,
                json.dumps(w.params),
                w.created_at,
                w.last_run_at,
                w.schedule,
                1 if w.enabled else 0,
            ),
        )
        c.commit()
        return w
    finally:
        if own:
            c.close()


def get_watcher(watcher_id: str, conn: sqlite3.Connection | None = None) -> PersistentWatcher | None:
    c, own = _own_conn(conn)
    try:
        row = c.execute(
            "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
        ).fetchone()
        return PersistentWatcher.from_row(dict(row)) if row else None
    finally:
        if own:
            c.close()


def list_watchers(
    command_name: str | None = None,
    file_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[PersistentWatcher]:
    c, own = _own_conn(conn)
    try:
        clauses = []
        args: list[Any] = []
        if command_name:
            clauses.append("command_name = ?")
            args.append(command_name)
        if file_path:
            clauses.append("file_path = ?")
            args.append(file_path)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(
            f"SELECT * FROM watchers {where} ORDER BY created_at DESC", args
        ).fetchall()
        return [PersistentWatcher.from_row(dict(r)) for r in rows]
    finally:
        if own:
            c.close()


def delete_watcher(watcher_id: str, conn: sqlite3.Connection | None = None) -> bool:
    c, own = _own_conn(conn)
    try:
        cur = c.execute("DELETE FROM watchers WHERE watcher_id = ?", (watcher_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        if own:
            c.close()


def touch_watcher_last_run(watcher_id: str, conn: sqlite3.Connection | None = None) -> None:
    c, own = _own_conn(conn)
    try:
        c.execute(
            "UPDATE watchers SET last_run_at = ? WHERE watcher_id = ?",
            (_now(), watcher_id),
        )
        c.commit()
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------------------
# Invocation log
# ---------------------------------------------------------------------------


def log_invocation(
    command_name: str,
    surface: str,
    context: dict,
    conn: sqlite3.Connection | None = None,
) -> str:
    c, own = _own_conn(conn)
    try:
        inv_id = _new_id()
        c.execute(
            """INSERT INTO invocations
               (invocation_id, command_name, surface, context, status,
                result, created_at, completed_at)
               VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL)""",
            (
                inv_id,
                command_name,
                surface,
                json.dumps(context or {}),
                _now(),
            ),
        )
        # Prune oldest beyond retention
        c.execute(
            f"""DELETE FROM invocations
                WHERE invocation_id IN (
                    SELECT invocation_id FROM invocations
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET {_INVOCATION_RETAIN}
                )"""
        )
        c.commit()
        return inv_id
    finally:
        if own:
            c.close()


def update_invocation(
    invocation_id: str,
    status: str,
    result: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    c, own = _own_conn(conn)
    try:
        c.execute(
            """UPDATE invocations
               SET status = ?, result = ?, completed_at = ?
               WHERE invocation_id = ?""",
            (
                status,
                json.dumps(result) if result is not None else None,
                _now(),
                invocation_id,
            ),
        )
        c.commit()
    finally:
        if own:
            c.close()


def list_invocations(limit: int = 50, conn: sqlite3.Connection | None = None) -> list[InlineInvocation]:
    c, own = _own_conn(conn)
    try:
        rows = c.execute(
            "SELECT * FROM invocations ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [InlineInvocation.from_row(dict(r)) for r in rows]
    finally:
        if own:
            c.close()
