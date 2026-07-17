"""Versioned schema for the machine-level truth store registry."""

from __future__ import annotations

import sqlite3

from work_buddy.storage.migrations import Migration, MigrationRunner


def _m001_truth_stores(conn: sqlite3.Connection) -> None:
    """Create the registered truth store inventory and live identity guard."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS truth_stores (
            path       TEXT PRIMARY KEY,
            store_id   TEXT NOT NULL CHECK (
                length(store_id) = 32
                AND store_id NOT GLOB '*[^0-9a-f]*'
            ),
            profile    TEXT NOT NULL,
            title      TEXT,
            last_seen  TEXT NOT NULL,
            reachable  INTEGER NOT NULL CHECK (reachable IN (0, 1))
        );

        CREATE INDEX IF NOT EXISTS idx_truth_stores_store_id
            ON truth_stores(store_id);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_truth_stores_live_store_id
            ON truth_stores(store_id)
            WHERE reachable = 1;
        """
    )


TRUTH_REGISTRY_MIGRATIONS = MigrationRunner(
    "truth_registry",
    migrations=[
        Migration(1, "truth store registry", _m001_truth_stores),
    ],
)


__all__ = ["TRUTH_REGISTRY_MIGRATIONS"]
