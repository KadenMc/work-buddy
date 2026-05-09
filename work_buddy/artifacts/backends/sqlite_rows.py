"""SQLite row-table backend.

Generic adapter for any SQLite table whose lifecycle is row-shaped:
each row is an artifact record, optionally with a per-row ``expires_at``
column and/or status-based retention.

Used by:
    * ``messaging`` (db/messages, table ``messages``)
    * ``llm_queue`` (db/llm_queue, table ``llm_call_queue``)

Capabilities declared:
    RECORDS, TYPED_COLUMNS, LISTABLE, DELETABLE, BULK_PRUNEABLE.

The ``transform_fn`` parameter on the constructor is intentionally
absent — that's the SqliteRollupStorage backend's concern. Keep the
shape narrow.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.protocol import Capability, Ref


class SqliteRowsStorage:
    """One row = one artifact record.

    Args:
        db_path: Path to the SQLite database file. Created if missing
            (caller is responsible for initial schema; this backend
            doesn't create tables).
        table: Name of the table holding the records.
        id_column: Column whose value uniquely identifies a row. Used
            by ``ref_for``, ``delete_record``.
        post_delete_sql: Optional SQL statements to run after a delete
            (e.g. cleaning orphaned foreign-key rows). Executed in the
            same transaction as the delete. List of statements; no
            parameters.
        vacuum_on_delete: Whether to run ``VACUUM`` after a successful
            delete to reclaim bytes. Default True; matches the
            messaging-DB pruner's behavior.
    """

    capabilities: frozenset[Capability] = frozenset({
        Capability.RECORDS,
        Capability.TYPED_COLUMNS,
        Capability.LISTABLE,
        Capability.DELETABLE,
        Capability.BULK_PRUNEABLE,
    })

    def __init__(
        self,
        *,
        db_path: Path,
        table: str,
        id_column: str = "id",
        post_delete_sql: list[str] | None = None,
        vacuum_on_delete: bool = True,
    ) -> None:
        self._db_path = db_path
        self._table = table
        self._id_column = id_column
        self._post_delete_sql = list(post_delete_sql or [])
        self._vacuum = vacuum_on_delete

    # --------------------------------------------------------- Storage API

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self._db_path.exists():
            return
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {self._table}").fetchall()
            except sqlite3.OperationalError:
                return
        for r in rows:
            yield dict(r)

    def ref_for(self, record: dict[str, Any]) -> Ref:
        return Ref(
            id=str(record.get(self._id_column, "")),
            artifact_name=self._table,
            metadata={k: v for k, v in record.items() if k != self._id_column},
        )

    def delete_record(self, ref: Ref) -> int:
        if not self._db_path.exists():
            return 0
        bytes_before = self._db_path.stat().st_size
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                f"DELETE FROM {self._table} WHERE {self._id_column} = ?",
                (ref.id,),
            )
            n = cur.rowcount
            for stmt in self._post_delete_sql:
                conn.execute(stmt)
            conn.commit()
            if n > 0 and self._vacuum:
                conn.execute("VACUUM")
        if n == 0:
            return 0
        bytes_after = self._db_path.stat().st_size if self._db_path.exists() else 0
        return max(0, bytes_before - bytes_after)

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        """Bulk-delete rows matching the Python-side predicate.

        Iterates rows once to identify victims, then issues one
        parameterised DELETE per id (small N expected). The whole batch
        runs in a single transaction with optional post-delete SQL +
        VACUUM.
        """
        if not self._db_path.exists():
            return (0, 0)
        bytes_before = self._db_path.stat().st_size

        # Find victim ids (Python-side filter)
        victims: list[Any] = []
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {self._table}").fetchall()
            except sqlite3.OperationalError:
                return (0, 0)
            for r in rows:
                row_dict = dict(r)
                if predicate(row_dict):
                    victims.append(row_dict.get(self._id_column))

        if not victims:
            return (0, 0)

        with sqlite3.connect(str(self._db_path)) as conn:
            placeholders = ",".join(["?"] * len(victims))
            cur = conn.execute(
                f"DELETE FROM {self._table} WHERE {self._id_column} IN ({placeholders})",
                victims,
            )
            n = cur.rowcount
            for stmt in self._post_delete_sql:
                conn.execute(stmt)
            conn.commit()
            if n > 0 and self._vacuum:
                conn.execute("VACUUM")

        bytes_after = self._db_path.stat().st_size if self._db_path.exists() else 0
        return (n, max(0, bytes_before - bytes_after))

    def size_bytes(self) -> int:
        return self._db_path.stat().st_size if self._db_path.exists() else 0
