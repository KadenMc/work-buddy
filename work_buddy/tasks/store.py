"""SQLite storage and read models for the neutral task domain."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Sequence

from work_buddy.config import load_config
from work_buddy.paths import repo_root, resolve

from .migrations import migrate
from .models import (
    Tag,
    Task,
    TaskActionItem,
    TaskDocumentLink,
    TaskHistoryEntry,
    TaskQuery,
    TaskSystemState,
)


def default_task_db_path() -> Path:
    """Resolve the established ``db/tasks`` resource, including its override."""
    configured = (load_config().get("tasks") or {}).get("db_path")
    if configured:
        candidate = Path(str(configured)).expanduser()
        return candidate if candidate.is_absolute() else repo_root() / candidate
    return resolve("db/tasks")


class TaskStore:
    """Connection factory plus bridge-independent task queries.

    Writes are intentionally performed by :class:`TaskApplicationService` so
    a caller cannot accidentally bypass receipts, history, CAS, or the outbox.
    """

    def __init__(self, path: str | Path | None = None, *, timeout: float = 10.0) -> None:
        self.path = Path(path) if path is not None else default_task_db_path()
        self.timeout = timeout

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _tags_for_ids(
        conn: sqlite3.Connection,
        task_ids: Sequence[str],
    ) -> dict[str, tuple[Tag, ...]]:
        if not task_ids:
            return {}
        result: dict[str, list[Tag]] = {task_id: [] for task_id in task_ids}
        for start in range(0, len(task_ids), 900):
            chunk = list(task_ids[start : start + 900])
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT task_id, tag, is_namespace FROM task_tags "
                f"WHERE task_id IN ({placeholders}) ORDER BY tag",
                chunk,
            )
            for row in rows:
                result[str(row["task_id"])].append(
                    Tag(str(row["tag"]), bool(row["is_namespace"]))
                )
        return {task_id: tuple(tags) for task_id, tags in result.items()}

    @classmethod
    def task_from_row(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> Task:
        task_id = str(row["task_id"])
        tags = cls._tags_for_ids(conn, [task_id]).get(task_id, ())
        actions = cls._action_items_for_ids(conn, [task_id]).get(task_id, ())
        return Task.from_row(row, tags=tags, action_items=actions)

    @staticmethod
    def _action_items_for_ids(
        conn: sqlite3.Connection,
        task_ids: Sequence[str],
    ) -> dict[str, tuple[TaskActionItem, ...]]:
        if not task_ids:
            return {}
        result: dict[str, list[TaskActionItem]] = {task_id: [] for task_id in task_ids}
        for start in range(0, len(task_ids), 900):
            chunk = list(task_ids[start : start + 900])
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT * FROM task_action_items WHERE task_id IN ({placeholders}) "
                "AND deleted_at IS NULL ORDER BY task_id, sequence, id",
                chunk,
            )
            for row in rows:
                result[str(row["task_id"])].append(TaskActionItem.from_row(row))
        return {task_id: tuple(items) for task_id, items in result.items()}

    @classmethod
    def get_in_connection(
        cls,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        include_deleted: bool = False,
    ) -> Task | None:
        suffix = "" if include_deleted else " AND deleted_at IS NULL"
        row = conn.execute(
            f"SELECT * FROM task_metadata WHERE task_id = ?{suffix}",
            (task_id,),
        ).fetchone()
        return cls.task_from_row(conn, row) if row is not None else None

    def get(self, task_id: str, *, include_deleted: bool = False) -> Task | None:
        conn = self.connect()
        try:
            return self.get_in_connection(conn, task_id, include_deleted=include_deleted)
        finally:
            conn.close()

    def get_tasks_for_session(self, session_id: str) -> list[dict[str, str]]:
        """Return live task assignments for a session, oldest first."""
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT s.task_id, s.assigned_at
                FROM task_sessions s
                JOIN task_metadata t ON t.task_id = s.task_id
                WHERE s.session_id = ? AND t.deleted_at IS NULL
                ORDER BY s.assigned_at, s.task_id
                """,
                (session_id,),
            ).fetchall()
            return [
                {"task_id": str(row["task_id"]), "assigned_at": str(row["assigned_at"])}
                for row in rows
            ]
        finally:
            conn.close()

    def get_sessions(self, task_id: str) -> list[dict[str, str]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT session_id, assigned_at FROM task_sessions "
                "WHERE task_id = ? ORDER BY assigned_at, session_id",
                (task_id,),
            ).fetchall()
            return [
                {"session_id": str(row["session_id"]), "assigned_at": str(row["assigned_at"])}
                for row in rows
            ]
        finally:
            conn.close()

    @staticmethod
    def _query_sql(query: TaskQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.state:
            clauses.append("t.state = ?")
            params.append(query.state)
        else:
            if not query.include_done:
                clauses.append("t.state != 'done'")
            if not query.include_snoozed:
                clauses.append("t.state != 'snoozed'")
        if query.urgency:
            clauses.append("t.urgency = ?")
            params.append(query.urgency)
        if not query.include_archived:
            clauses.append("t.archived_at IS NULL")
        if not query.include_deleted:
            clauses.append("t.deleted_at IS NULL")
        if query.project:
            project = query.project.strip().strip("#/")
            clauses.append(
                "EXISTS (SELECT 1 FROM task_tags pt WHERE pt.task_id = t.task_id "
                "AND (LOWER(pt.tag) = LOWER(?) OR LOWER(pt.tag) LIKE LOWER(?)))"
            )
            params.extend([f"projects/{project}", f"projects/{project}/%"])
        if query.namespace:
            namespace = query.namespace.strip().strip("#/")
            clauses.append(
                "EXISTS (SELECT 1 FROM task_tags nt WHERE nt.task_id = t.task_id "
                "AND nt.is_namespace = 1 "
                "AND (LOWER(nt.tag) = LOWER(?) OR LOWER(nt.tag) LIKE LOWER(?)))"
            )
            params.extend([namespace, f"{namespace}/%"])
        if query.text and query.text.strip():
            escaped = (
                query.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            needle = f"%{escaped}%"
            clauses.append(
                "(LOWER(COALESCE(t.description, '')) LIKE LOWER(?) ESCAPE '\\' "
                "OR LOWER(COALESCE(t.outcome_text, '')) LIKE LOWER(?) ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM task_tags qt WHERE qt.task_id = t.task_id "
                "AND LOWER(qt.tag) LIKE LOWER(?) ESCAPE '\\'))"
            )
            params.extend([needle, needle, needle])
        if query.due:
            today = date.today().isoformat()
            if query.due == "overdue":
                clauses.append("t.due_date IS NOT NULL AND t.due_date < ?")
                params.append(today)
            elif query.due == "today":
                clauses.append("t.due_date = ?")
                params.append(today)
            elif query.due == "upcoming":
                clauses.append("t.due_date IS NOT NULL AND t.due_date > ?")
                params.append(today)
            elif query.due == "none":
                clauses.append("t.due_date IS NULL")
            else:
                clauses.append("t.due_date = ?")
                params.append(query.due)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        sql = (
            "SELECT t.* FROM task_metadata t WHERE "
            + where
            + " ORDER BY CASE t.state WHEN 'focused' THEN 0 WHEN 'active' THEN 1 "
            "WHEN 'mit' THEN 2 WHEN 'waiting' THEN 3 WHEN 'inbox' THEN 4 "
            "WHEN 'snoozed' THEN 5 ELSE 6 END, "
            "CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END, t.due_date, "
            "t.created_at, t.task_id LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(int(query.limit), 5000)), max(0, int(query.offset))])
        return sql, params

    def list(self, query: TaskQuery | None = None) -> list[Task]:
        requested = query or TaskQuery()
        sql, params = self._query_sql(requested)
        conn = self.connect()
        try:
            rows = list(conn.execute(sql, params))
            ids = [str(row["task_id"]) for row in rows]
            tags = self._tags_for_ids(conn, ids)
            actions = self._action_items_for_ids(conn, ids)
            return [
                Task.from_row(
                    row,
                    tags=tags.get(str(row["task_id"]), ()),
                    action_items=actions.get(str(row["task_id"]), ()),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def search(
        self,
        text: str,
        *,
        limit: int = 50,
        include_done: bool = True,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> list[Task]:
        if not text.strip():
            return []
        return self.list(
            TaskQuery(
                text=text,
                limit=limit,
                include_done=include_done,
                include_archived=include_archived,
                include_deleted=include_deleted,
                include_snoozed=True,
            )
        )

    @staticmethod
    def collection_revision_in_connection(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT revision FROM task_collection_state WHERE id = 1"
        ).fetchone()
        return int(row[0])

    def collection_revision(self) -> int:
        conn = self.connect()
        try:
            return self.collection_revision_in_connection(conn)
        finally:
            conn.close()

    def history(self, task_id: str) -> list[TaskHistoryEntry]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM task_state_history WHERE task_id = ? "
                "ORDER BY changed_at DESC, id DESC",
                (task_id,),
            ).fetchall()
            result: list[TaskHistoryEntry] = []
            for row in rows:
                try:
                    details = json.loads(row["details_json"] or "{}")
                except (TypeError, ValueError):
                    details = {}
                result.append(
                    TaskHistoryEntry(
                        id=int(row["id"]),
                        task_id=str(row["task_id"]),
                        old_state=row["old_state"],
                        new_state=str(row["new_state"]),
                        changed_at=str(row["changed_at"]),
                        reason=row["reason"],
                        mutation=row["mutation"],
                        actor=row["actor"],
                        session_id=row["session_id"],
                        receipt_id=row["receipt_id"],
                        task_revision=row["task_revision"],
                        collection_revision=row["collection_revision"],
                        details=details if isinstance(details, dict) else {},
                    )
                )
            return result
        finally:
            conn.close()

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM task_event_outbox WHERE published_at IS NULL "
                "ORDER BY collection_revision LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result
        finally:
            conn.close()

    def mark_outbox_published(self, event_id: str, *, published_at: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE task_event_outbox SET published_at = ?, attempts = attempts + 1, "
                "last_error = NULL WHERE event_id = ? AND published_at IS NULL",
                (published_at, event_id),
            )
            return cursor.rowcount == 1

    def record_outbox_failure(self, event_id: str, *, error: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE task_event_outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE event_id = ? AND published_at IS NULL",
                (error[:2000], event_id),
            )
            return cursor.rowcount == 1

    def system_state(self) -> TaskSystemState:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM task_system_state WHERE id = 1").fetchone()
            assert row is not None
            return TaskSystemState(
                authority_epoch=str(row["authority_epoch"]),
                cowork_task_store_id=row["cowork_task_store_id"],
                cutover_receipt_id=row["cutover_receipt_id"],
                rollback_fence=bool(row["rollback_fence"]),
                process_generation=int(row["process_generation"]),
                updated_at=str(row["updated_at"]),
            )
        finally:
            conn.close()

    def get_task_document_link(self, task_id: str) -> TaskDocumentLink | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_document_links WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return TaskDocumentLink(**dict(row)) if row is not None else None
        finally:
            conn.close()

    def upsert_task_document_link(
        self,
        link: TaskDocumentLink,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> TaskDocumentLink:
        """Persist a canonical link for document-saga/migration callers.

        Ordinary product mutations should pass the service transaction's
        connection so the parent task revision, receipt, history, and outbox
        commit alongside this row.  The self-owned transaction form exists for
        the pre-activation migration operator, whose cohort receipt provides
        the outer audit boundary.
        """

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO task_document_links (
                    task_id, note_uuid, store_id, document_id, binding_id,
                    lifecycle, created_at, updated_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    note_uuid = excluded.note_uuid,
                    store_id = excluded.store_id,
                    document_id = excluded.document_id,
                    binding_id = excluded.binding_id,
                    lifecycle = excluded.lifecycle,
                    updated_at = excluded.updated_at,
                    retired_at = excluded.retired_at
                """,
                (
                    link.task_id,
                    link.note_uuid,
                    link.store_id,
                    link.document_id,
                    link.binding_id,
                    link.lifecycle,
                    link.created_at,
                    link.updated_at,
                    link.retired_at,
                ),
            )

        if connection is not None:
            write(connection)
        else:
            with self.transaction() as conn:
                write(conn)
        return link

    def local_file_links(self, task_id: str) -> list[dict[str, Any]]:
        """Return metadata-only link records; never roots, paths, or bytes."""
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT link_id, task_id, store_id, document_id, display_name,
                       suffix, media_type, byte_length, sha256, sensitivity,
                       allowed_action, policy_revision, created_at
                FROM task_local_file_links
                WHERE task_id = ? ORDER BY created_at, link_id
                """,
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def set_system_state(
        self,
        *,
        expected_authority_epoch: str,
        authority_epoch: str,
        updated_at: str,
        cowork_task_store_id: str | None = None,
        cutover_receipt_id: str | None = None,
        rollback_fence: bool = False,
        process_generation: int | None = None,
    ) -> TaskSystemState:
        """CAS the authority epoch for an explicitly authorized operator.

        This method performs no cutover on its own; callers must hold the
        maintenance/cohort lease and supply its durable receipt ID.
        """
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM task_system_state WHERE id = 1"
            ).fetchone()
            assert current is not None
            if current["authority_epoch"] != expected_authority_epoch:
                raise RuntimeError(
                    "task authority epoch changed concurrently: expected "
                    f"{expected_authority_epoch!r}, found {current['authority_epoch']!r}"
                )
            generation = (
                int(process_generation)
                if process_generation is not None
                else int(current["process_generation"])
            )
            conn.execute(
                """
                UPDATE task_system_state
                SET authority_epoch = ?, cowork_task_store_id = ?,
                    cutover_receipt_id = ?, rollback_fence = ?,
                    process_generation = ?, updated_at = ?
                WHERE id = 1 AND authority_epoch = ?
                """,
                (
                    authority_epoch,
                    cowork_task_store_id,
                    cutover_receipt_id,
                    int(rollback_fence),
                    generation,
                    updated_at,
                    expected_authority_epoch,
                ),
            )
        return self.system_state()
