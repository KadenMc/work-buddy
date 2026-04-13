"""SQLite project store — identity registry.

Projects are any bounded area of work or life the user tracks: research
papers, coding repos, a book, a business, admin workflows.  The store
captures project *identity* (what projects exist, their lifecycle status).
Project *memory* (observations, decisions, trajectory) lives in the
Hindsight project bank — see work_buddy.memory for retain/recall.

Schema follows work_buddy.obsidian.tasks.store patterns: SQLite with WAL
mode, row_factory=sqlite3.Row, auto-create on first access.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS projects (
    slug         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    description  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_status
    ON projects(status);
"""

VALID_STATUSES = {"active", "paused", "past", "future", "inferred"}


def _db_path() -> Path:
    """Resolve the project database path from config."""
    cfg = load_config()
    custom = cfg.get("projects", {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/projects")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection() -> sqlite3.Connection:
    """Open (or create) the project database with WAL mode."""
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Drop legacy project_observations table if it exists."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "project_observations" in tables:
        conn.execute("DROP TABLE project_observations")
        conn.commit()
        logger.info("Dropped legacy project_observations table")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Project CRUD ────────────────────────────────────────────────

def upsert_project(
    slug: str,
    name: str | None = None,
    *,
    status: str = "active",
    description: str | None = None,
) -> dict[str, Any]:
    """Create or update a project identity record.

    On conflict (slug exists), updates status, description, and updated_at.
    Does NOT overwrite an existing name or description with None — pass an
    explicit value to change them.  This means the collector can pass a
    humanized-slug default and it will only take effect when the project is
    first created; subsequent runs leave user-set names intact.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}. Must be one of {VALID_STATUSES}")

    now = _now_iso()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT slug, name, description FROM projects WHERE slug = ?", (slug,)
        ).fetchone()

        if existing:
            resolved_name = name if name is not None else existing["name"]
            desc = description if description is not None else existing["description"]
            conn.execute(
                "UPDATE projects SET name=?, status=?, description=?, updated_at=? WHERE slug=?",
                (resolved_name, status, desc, now, slug),
            )
        else:
            resolved_name = name if name is not None else slug
            conn.execute(
                "INSERT INTO projects (slug, name, status, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, resolved_name, status, description, now, now),
            )
        conn.commit()
    finally:
        conn.close()

    return {"slug": slug, "name": resolved_name, "status": status, "updated_at": now}


def get_project(slug: str) -> dict[str, Any] | None:
    """Get a project identity record."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def list_projects(status: str | None = None) -> list[dict[str, Any]]:
    """List projects, optionally filtered by status."""
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY "
                "CASE status "
                "  WHEN 'active' THEN 0 "
                "  WHEN 'inferred' THEN 1 "
                "  WHEN 'paused' THEN 2 "
                "  WHEN 'future' THEN 3 "
                "  WHEN 'past' THEN 4 "
                "END, slug",
            ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


def touch_project(slug: str) -> None:
    """Update a project's updated_at timestamp without changing other fields."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE projects SET updated_at = ? WHERE slug = ?",
            (_now_iso(), slug),
        )
        conn.commit()
    finally:
        conn.close()


class _Sentinel:
    """Distinguishes 'not provided' from None in update kwargs."""

_NOT_SET = _Sentinel()


def update_project(
    slug: str,
    *,
    name: str | _Sentinel = _NOT_SET,
    status: str | _Sentinel = _NOT_SET,
    description: str | None | _Sentinel = _NOT_SET,
) -> dict[str, Any] | None:
    """Update specific fields of a project. Only provided fields are changed."""
    if isinstance(status, str) and status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}. Must be one of {VALID_STATUSES}")

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return None

        updates = []
        params = []
        if not isinstance(name, _Sentinel):
            updates.append("name=?")
            params.append(name)
        if not isinstance(status, _Sentinel):
            updates.append("status=?")
            params.append(status)
        if not isinstance(description, _Sentinel):
            updates.append("description=?")
            params.append(description)

        if not updates:
            return dict(row)

        updates.append("updated_at=?")
        params.append(_now_iso())
        params.append(slug)

        conn.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE slug=?", params
        )
        conn.commit()

        return dict(conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone())
    finally:
        conn.close()


def delete_project(slug: str) -> bool:
    """Delete a project. Returns True if found."""
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
