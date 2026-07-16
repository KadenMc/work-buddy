"""Versioned schema migrations for the host-owned Settings store."""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import Migration, MigrationRunner


_CURRENT_STATE_COLUMNS = (
    "setting_id",
    "scope",
    "scope_id",
    "bootstrap_default_value_json",
    "bootstrap_source",
    "value_version",
    "active_timezone",
    "active_value_json",
    "pending_value_json",
    "pending_source",
    "pending_timezone",
    "effective_at",
    "applied_from_value_json",
    "applied_at",
    "revision",
    "updated_at",
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _create_current_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS setting_value_state (
            setting_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            bootstrap_default_value_json TEXT,
            bootstrap_source TEXT NOT NULL DEFAULT 'config',
            value_version INTEGER NOT NULL DEFAULT 1,
            active_timezone TEXT,
            active_value_json TEXT,
            pending_value_json TEXT,
            pending_source TEXT CHECK (
                pending_source IS NULL OR pending_source IN ('profile', 'default')
            ),
            pending_timezone TEXT,
            effective_at TEXT,
            applied_from_value_json TEXT,
            applied_at TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (setting_id, scope, scope_id)
        )
        """
    )


def _m001_legacy_value_state(conn: sqlite3.Connection) -> None:
    """Create the earliest Settings value table shape.

    The following migration rebuilds this shape into the complete value-identity
    contract. Keeping the historical shape explicit lets an unversioned early
    database be adopted without guessing that it already has the current schema.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS setting_value_state (
            setting_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            active_value_json TEXT,
            pending_value_json TEXT,
            pending_source TEXT,
            effective_at TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (setting_id, scope, scope_id)
        )
        """
    )


def _m002_value_identity_and_transition_state(conn: sqlite3.Connection) -> None:
    """Rebuild the value table with the current identity and transition fields."""
    existing = _table_columns(conn, "setting_value_state")
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'setting_value_state'"
    ).fetchone()
    table_sql = (table_sql_row[0] if table_sql_row else "").lower()
    has_current_constraint = (
        "pending_source in ('profile', 'default')" in " ".join(table_sql.split())
    )
    if set(_CURRENT_STATE_COLUMNS).issubset(existing) and has_current_constraint:
        return

    conn.execute(
        "ALTER TABLE setting_value_state RENAME TO setting_value_state_before_identity"
    )
    _create_current_state_table(conn)

    fallbacks = {
        "bootstrap_default_value_json": "NULL",
        "bootstrap_source": "'config'",
        "value_version": "1",
        "active_timezone": "NULL",
        "pending_timezone": "NULL",
        "applied_from_value_json": "NULL",
        "applied_at": "NULL",
    }
    select_expressions = [
        column if column in existing else fallbacks[column]
        for column in _CURRENT_STATE_COLUMNS
    ]
    conn.execute(
        "INSERT INTO setting_value_state ("
        + ", ".join(_CURRENT_STATE_COLUMNS)
        + ") SELECT "
        + ", ".join(select_expressions)
        + " FROM setting_value_state_before_identity"
    )
    conn.execute("DROP TABLE setting_value_state_before_identity")


def _m003_journal_day_policy_history(conn: sqlite3.Connection) -> None:
    """Create the durable Journal-day policy epoch history and uniqueness rules."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journal_day_policy_epoch (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            effective_local_date TEXT,
            window_start TEXT,
            boundary TEXT NOT NULL,
            timezone TEXT NOT NULL,
            setting_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (
                (effective_local_date IS NULL AND window_start IS NULL)
                OR (effective_local_date IS NOT NULL AND window_start IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_day_policy_base
        ON journal_day_policy_epoch ((1))
        WHERE effective_local_date IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_day_policy_date
        ON journal_day_policy_epoch (effective_local_date)
        WHERE effective_local_date IS NOT NULL
        """
    )


class _SettingsMigrationRunner(MigrationRunner):
    """Infer the precise adoption point for an unversioned Settings database."""

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != '_migration_history'"
            )
        }
        if "setting_value_state" not in tables:
            return 0

        columns = _table_columns(conn, "setting_value_state")
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'setting_value_state'"
        ).fetchone()
        table_sql = (table_sql_row[0] if table_sql_row else "").lower()
        has_current_constraint = (
            "pending_source in ('profile', 'default')" in " ".join(table_sql.split())
        )
        if not set(_CURRENT_STATE_COLUMNS).issubset(columns) or not has_current_constraint:
            return 1
        if "journal_day_policy_epoch" not in tables:
            return 2
        return self.target_version


SETTINGS_MIGRATIONS = _SettingsMigrationRunner(
    "settings",
    migrations=[
        Migration(1, "legacy Settings value state", _m001_legacy_value_state),
        Migration(
            2,
            "value identity and transition state",
            _m002_value_identity_and_transition_state,
        ),
        Migration(
            3,
            "Journal day policy history",
            _m003_journal_day_policy_history,
        ),
    ],
)
