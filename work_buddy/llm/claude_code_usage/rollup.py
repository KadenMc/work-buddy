"""Daily rollup of older Claude Code `turns` rows.

The claude_code_usage DB grows unboundedly (~110 MB/year at observed
~1k turns/day rate) because every Claude Code call writes one row.
The dashboard only consumes per-(day, model, session) aggregates —
``by_day``, ``by_model``, ``by_project``, plus the per-session totals
that already live in the ``sessions`` table. Per-turn drilldown isn't
exposed in the UI at all.

This module collapses turns older than a configurable horizon
(default 90 days) into a `turns_daily` aggregate table, then deletes
the original rows. Lossless for everything the dashboard renders;
only loss is per-turn inspection of history older than the horizon.

The `turns_daily` table is created by ``scanner.init_db``; this
module assumes the schema exists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


def rollup_old_turns(
    conn: sqlite3.Connection,
    days_to_keep_full: int = 90,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Aggregate turns older than the cutoff into ``turns_daily``, then delete.

    Args:
        conn: open connection to the claude_code_usage DB.
        days_to_keep_full: turns whose date is on or after
            ``today - days_to_keep_full`` are left untouched.
        dry_run: if True, count what *would* be rolled up but make no
            changes. Returns ``rolled_turns: -1`` to signal that the
            count was not measured (dry-run only counts groups).

    Returns:
        dict with ``rollup_groups`` (number of distinct (session, day, model)
        triples that would be / were collapsed) and ``rolled_turns`` (number
        of original turn rows deleted; -1 for dry-run).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep_full)).strftime("%Y-%m-%d")

    candidate_sql = """
        SELECT session_id, substr(timestamp, 1, 10) AS day, model,
               COALESCE(SUM(input_tokens), 0),
               COALESCE(SUM(output_tokens), 0),
               COALESCE(SUM(cache_read_tokens), 0),
               COALESCE(SUM(cache_creation_tokens), 0),
               COUNT(*)
        FROM turns
        WHERE substr(timestamp, 1, 10) < ?
        GROUP BY session_id, substr(timestamp, 1, 10), model
    """
    rows = conn.execute(candidate_sql, (cutoff,)).fetchall()
    if not rows:
        return {"rollup_groups": 0, "rolled_turns": 0}

    if dry_run:
        return {"rollup_groups": len(rows), "rolled_turns": -1}

    # Atomic-ish: insert rollups + delete originals in one transaction so
    # a crash mid-operation can't lose data. SQLite's default journaled
    # mode plus the surrounding implicit transaction give us this.
    conn.executemany(
        """INSERT INTO turns_daily
           (session_id, day, model,
            input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, turn_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id, day, model) DO UPDATE SET
               input_tokens          = input_tokens          + excluded.input_tokens,
               output_tokens         = output_tokens         + excluded.output_tokens,
               cache_read_tokens     = cache_read_tokens     + excluded.cache_read_tokens,
               cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,
               turn_count            = turn_count            + excluded.turn_count
        """,
        rows,
    )
    cur = conn.execute(
        "DELETE FROM turns WHERE substr(timestamp, 1, 10) < ?", (cutoff,)
    )
    deleted = cur.rowcount
    conn.commit()
    # VACUUM cannot run inside a transaction. The commit above closes the
    # implicit transaction; VACUUM then runs in autocommit mode and
    # rewrites the file to actually reclaim freed pages.
    conn.execute("VACUUM")
    return {"rollup_groups": len(rows), "rolled_turns": deleted}


# ---------------------------------------------------------------------------
# Lifecycle registration — claude-code-usage artifact
# ---------------------------------------------------------------------------
#
# Registers a SqliteRollupStorage + TimeWindow + TransformAndDelete
# artifact under "claude-code-usage". The TransformAndDelete action
# wraps the rollup_old_turns function above so it plugs in unchanged.

import logging as _logging
_logger = _logging.getLogger(__name__)


def _register_claude_code_usage_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Lifecycle,
            register_artifact,
            SqliteRollupStorage,
            TimeWindow,
            TransformAndDelete,
        )
        from work_buddy.paths import resolve

        register_artifact(Artifact(
            name="claude-code-usage",
            storage=SqliteRollupStorage(
                db_path=resolve("cache/claude-code-usage"),
                source_table="turns",
                rollup_table="turns_daily",
                id_column="id",
            ),
            lifecycle=Lifecycle(
                # The trigger doesn't actually drive the action's
                # behavior here — TransformAndDelete uses the storage's
                # open_connection() to do its own SQL-driven row
                # selection (the cutoff lives inside rollup_old_turns).
                # The trigger's role is just to declare TIME_WINDOW so
                # the artifact's capabilities are honest about what it
                # does.
                trigger=TimeWindow(
                    timestamp_field="timestamp",
                    window_days=90,
                ),
                action=TransformAndDelete(transform_fn=rollup_old_turns),
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("Failed to register claude-code-usage artifact: %s", exc)


_register_claude_code_usage_artifact()
