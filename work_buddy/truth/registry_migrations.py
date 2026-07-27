"""Versioned schema for the machine-level truth store registry."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from work_buddy.storage.migrations import Migration, MigrationRunner


def _filesystem_display_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if os.name != "nt" or not resolved.exists():
        return resolved
    parts = resolved.parts
    if not parts:
        return resolved
    current = Path(parts[0])
    for part in parts[1:]:
        try:
            match = next(
                (
                    entry.name
                    for entry in current.iterdir()
                    if entry.name.casefold() == part.casefold()
                ),
                part,
            )
        except OSError:
            match = part
        current /= match
    return current


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


def _m002_folder_projection(conn: sqlite3.Connection) -> None:
    """Cache the bounded fields needed by the Co-work Folder selector."""

    conn.executescript(
        """
        ALTER TABLE truth_stores ADD COLUMN layout TEXT NOT NULL
            DEFAULT 'wbuddy_cowork_v1';
        ALTER TABLE truth_stores ADD COLUMN document_surface_enabled INTEGER NOT NULL
            DEFAULT 0 CHECK (document_surface_enabled IN (0, 1));
        ALTER TABLE truth_stores ADD COLUMN allowed_document_classes_json TEXT NOT NULL
            DEFAULT '[]';
        ALTER TABLE truth_stores ADD COLUMN feedback_capture INTEGER NOT NULL
            DEFAULT 0 CHECK (feedback_capture IN (0, 1));
        ALTER TABLE truth_stores ADD COLUMN document_count INTEGER NOT NULL
            DEFAULT 0 CHECK (document_count >= 0);
        ALTER TABLE truth_stores ADD COLUMN last_error TEXT;
        """
    )


def _m003_path_identity(conn: sqlite3.Connection) -> None:
    """Separate case-insensitive machine identity from display/open casing."""

    conn.execute("ALTER TABLE truth_stores ADD COLUMN path_key TEXT")
    rows = conn.execute("SELECT path FROM truth_stores ORDER BY path").fetchall()
    seen: dict[str, str] = {}
    for row in rows:
        stored = str(row[0])
        display = str(_filesystem_display_path(Path(stored)))
        key = os.path.normcase(str(Path(display).expanduser().resolve()))
        prior = seen.get(key)
        if prior is not None and prior != stored:
            raise sqlite3.IntegrityError(
                "truth registry contains case-aliased duplicate paths"
            )
        seen[key] = stored
        conn.execute(
            "UPDATE truth_stores SET path = ?, path_key = ? WHERE path = ?",
            (display, key, stored),
        )
    conn.executescript(
        """
        CREATE UNIQUE INDEX uq_truth_stores_path_key
            ON truth_stores(path_key);
        CREATE TRIGGER truth_stores_path_key_required_insert
        BEFORE INSERT ON truth_stores
        WHEN NEW.path_key IS NULL OR NEW.path_key = ''
        BEGIN
            SELECT RAISE(ABORT, 'truth_stores.path_key is required');
        END;
        CREATE TRIGGER truth_stores_path_key_required_update
        BEFORE UPDATE OF path_key ON truth_stores
        WHEN NEW.path_key IS NULL OR NEW.path_key = ''
        BEGIN
            SELECT RAISE(ABORT, 'truth_stores.path_key is required');
        END;
        """
    )


def _m004_canonical_layout(conn: sqlite3.Connection) -> None:
    """Normalize every retained inventory row to the sole supported layout."""

    conn.execute(
        "UPDATE truth_stores SET layout = 'wbuddy_cowork_v1'"
    )


TRUTH_REGISTRY_MIGRATIONS = MigrationRunner(
    "truth_registry",
    migrations=[
        Migration(1, "truth store registry", _m001_truth_stores),
        Migration(2, "cached Co-work Folder projection", _m002_folder_projection),
        Migration(3, "case-faithful path identity", _m003_path_identity),
        Migration(4, "canonical Co-work layout", _m004_canonical_layout),
    ],
)


__all__ = ["TRUTH_REGISTRY_MIGRATIONS"]
