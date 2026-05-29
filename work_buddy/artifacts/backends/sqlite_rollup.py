"""SQLite rollup backend — rows that get aggregated then deleted.

Used by ``claude_code_usage`` to roll up per-turn rows older than
``days_to_keep_full`` into a daily-aggregate table, then delete the
originals. Bytes drop dramatically (one row per (day, model, session)
instead of per turn) without losing any of the metrics the dashboard
actually displays.

This backend implements the *storage* side: it knows how to enumerate
rows, count them, and delete them. The *transform* side (writing
aggregates into the rollup table before deletion) is handled by the
:class:`TransformAndDelete` ExpiryAction. The action calls a
caller-provided ``transform_fn(conn, rows, dry_run) -> dict`` to do the
actual aggregation; this backend just exposes the SQLite connection
and row-iteration primitives.

Capabilities declared:
    RECORDS, TYPED_COLUMNS, BULK_PRUNEABLE.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.protocol import StorageTrait, Ref


class SqliteRollupStorage:
    """SQLite table whose lifecycle is rollup-then-delete.

    Args:
        db_path: Path to the SQLite database file.
        source_table: Table holding the per-record rows that get rolled
            up + deleted (e.g. ``"turns"`` for claude_code_usage).
        rollup_table: Aggregate table the action writes into (e.g.
            ``"turns_daily"``). The backend doesn't read or write this
            table itself; the action's transform_fn does. The reference
            is here for ``describe()`` so observability surfaces the
            full picture.
        id_column: Primary key column of source_table. Defaults to
            ``id``.
    """

    capabilities: frozenset[StorageTrait] = frozenset({
        StorageTrait.RECORDS,
        StorageTrait.TYPED_COLUMNS,
        StorageTrait.BULK_PRUNEABLE,
    })

    def __init__(
        self,
        *,
        db_path: Path,
        source_table: str,
        rollup_table: str,
        id_column: str = "id",
    ) -> None:
        self._db_path = db_path
        self._source_table = source_table
        self._rollup_table = rollup_table
        self._id_column = id_column

    # ----------------------------------------------------- introspection

    @property
    def source_table(self) -> str:
        return self._source_table

    @property
    def rollup_table(self) -> str:
        return self._rollup_table

    def open_connection(self) -> sqlite3.Connection:
        """Open a connection. Caller must close it.

        Used by :class:`TransformAndDelete` so its ``transform_fn`` can
        run arbitrary SQL inside one transaction (rollup + delete).
        """
        return sqlite3.connect(str(self._db_path))

    # --------------------------------------------------------- Storage API

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self._db_path.exists():
            return
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {self._source_table}").fetchall()
            except sqlite3.OperationalError:
                return
        for r in rows:
            yield dict(r)

    def ref_for(self, record: dict[str, Any]) -> Ref:
        return Ref(
            id=str(record.get(self._id_column, "")),
            artifact_name=f"{self._source_table}-rollup",
            metadata={k: v for k, v in record.items() if k != self._id_column},
        )

    def delete_record(self, ref: Ref) -> int:
        # Per-row delete isn't part of this backend's rollup workflow,
        # but supporting it as a primitive is useful for tests.
        if not self._db_path.exists():
            return 0
        bytes_before = self._db_path.stat().st_size
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute(
                f"DELETE FROM {self._source_table} WHERE {self._id_column} = ?",
                (ref.id,),
            )
            n = cur.rowcount
            conn.commit()
        if n == 0:
            return 0
        bytes_after = self._db_path.stat().st_size if self._db_path.exists() else 0
        return max(0, bytes_before - bytes_after)

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        """Bulk-delete rows from the source table matching predicate.

        Used as a fallback path; the rollup action prefers to use
        ``open_connection()`` to do rollup + delete in one transaction.
        """
        if not self._db_path.exists():
            return (0, 0)
        bytes_before = self._db_path.stat().st_size

        victims: list[Any] = []
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {self._source_table}").fetchall()
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
                f"DELETE FROM {self._source_table} "
                f"WHERE {self._id_column} IN ({placeholders})",
                victims,
            )
            n = cur.rowcount
            conn.commit()

        bytes_after = self._db_path.stat().st_size if self._db_path.exists() else 0
        return (n, max(0, bytes_before - bytes_after))

    def size_bytes(self) -> int:
        return self._db_path.stat().st_size if self._db_path.exists() else 0
