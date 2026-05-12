"""SQLite project store with relational temporal model.

Schema lives in :mod:`work_buddy.projects.migrations` (versioned via the
``PRAGMA user_version`` migration framework). This module is the CRUD
surface: every mutation flows through here, every mutation writes a
complete revision row capturing the post-mutation state.

Identity is split across three current-state tables:

- ``projects`` — one row per canonical project. Stable surrogate
  ``id`` integer PK; ``slug`` is a mutable display label.
- ``project_folders`` — folder paths associated with the project, each
  flagged ``archived=0/1``.
- ``project_aliases`` — alternative slugs (e.g. prior names, capitalization
  variants) with a normalized form for lookup.

History is append-only and lives in three more tables:

- ``project_revisions`` — one row per mutation, snapshotting the
  projects-row state plus audit fields (``author``, ``created_at``,
  ``user_confirmed_at``, ``change_summary``, ``schema_version``).
- ``project_folders_history`` — folder set as of each revision.
- ``project_aliases_history`` — alias set as of each revision.

The ``project_id`` in revision tables intentionally **is not** a foreign
key — revision rows must outlive slug renames and the soft-delete
lifecycle.

Project narrative memory (decisions, trajectory, context) lives in the
Hindsight project bank; see :mod:`work_buddy.memory`. This module
captures structured identity only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.projects.migrations import PROJECT_MIGRATIONS

logger = get_logger(__name__)


# ─── Configuration ──────────────────────────────────────────────────


# Canonical lifecycle-status display order. Excludes ``deleted`` —
# soft-deleted projects are filtered from default list/render surfaces.
# Drives: the SQL ``CASE`` in :func:`list_projects`, the section order
# in :func:`work_buddy.projects.sync._render_markdown`, and the
# dashboard's ``/api/projects/_schema`` endpoint (which the frontend
# uses to build grouping buckets and the status ``<select>`` options).
STATUS_DISPLAY_ORDER: tuple[str, ...] = ("active", "paused", "future", "past")

VALID_STATUSES: set[str] = set(STATUS_DISPLAY_ORDER) | {"deleted"}
VALID_ORIGINS: set[str] = {"vault", "manual"}
VALID_AUTHORS: set[str] = {"user", "agent"}


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
    """Open (or create) the project database with WAL mode + migrations."""
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    PROJECT_MIGRATIONS.run(conn)
    return conn


# ─── Helpers ────────────────────────────────────────────────────────


def _now() -> str:
    """Millisecond-precision ISO 8601 UTC timestamp.

    Captured once per logical operation and passed to every INSERT in
    that operation's transaction — avoids the SQLite per-statement
    ``CURRENT_TIMESTAMP`` drift trap.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_slug(name: str) -> str:
    """Slug normalization: lowercase, ``_`` and space → ``-``."""
    return name.lower().replace("_", "-").replace(" ", "-")


def _publish_project_event(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort publish to the dashboard event bus.

    Routes through ``publish_auto`` so callers from the dashboard
    process publish in-process and callers from any other process
    route through the messaging-service bridge automatically. Never
    raises — a missed event must not break a project mutation.
    """
    try:
        from work_buddy.dashboard.events import publish_auto
        publish_auto(event_type, payload)
    except Exception:
        logger.exception("projects: event publish for %r failed", event_type)


# ─── Lookup ─────────────────────────────────────────────────────────


def resolve_slug(slug_or_alias: str) -> int | None:
    """Return the canonical ``project_id`` for a slug or alias, else None.

    Single entry point for any caller that has a string and needs to
    identify which project it refers to. Resolution order:

    1. Non-deleted canonical slug match (exact)
    2. Non-deleted canonical slug match (case-insensitive)
    3. Alias match, joined to a non-deleted project
    4. Deleted canonical slug match (exact)
    5. Deleted canonical slug match (case-insensitive)
    6. Alias match, joined to a deleted project

    The "prefer non-deleted" ordering handles the case where a slug
    was once a canonical project, was soft-deleted, and is now an
    alias on a different live project. Without this ordering the
    soft-deleted canonical would shadow the live alias.
    """
    if not slug_or_alias:
        return None
    norm = _normalize_slug(slug_or_alias)
    conn = get_connection()
    try:
        # 1. Non-deleted exact slug.
        row = conn.execute(
            "SELECT id FROM projects WHERE slug = ? AND status != 'deleted'",
            (slug_or_alias,),
        ).fetchone()
        if row:
            return row["id"]
        # 2. Non-deleted case-insensitive slug.
        row = conn.execute(
            "SELECT id FROM projects WHERE LOWER(slug) = ? "
            "AND status != 'deleted'",
            (norm,),
        ).fetchone()
        if row:
            return row["id"]
        # 3. Alias joined to non-deleted project.
        row = conn.execute(
            "SELECT a.project_id FROM project_aliases a "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE a.alias_norm = ? AND p.status != 'deleted'",
            (norm,),
        ).fetchone()
        if row:
            return row["project_id"]
        # 4. Deleted exact slug.
        row = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug_or_alias,)
        ).fetchone()
        if row:
            return row["id"]
        # 5. Deleted case-insensitive slug.
        row = conn.execute(
            "SELECT id FROM projects WHERE LOWER(slug) = ?", (norm,)
        ).fetchone()
        if row:
            return row["id"]
        # 6. Alias joined to deleted project (rare; preserves audit).
        row = conn.execute(
            "SELECT project_id FROM project_aliases WHERE alias_norm = ?",
            (norm,),
        ).fetchone()
        if row:
            return row["project_id"]
        return None
    finally:
        conn.close()


def get_project(slug: str) -> dict[str, Any] | None:
    """Return a project record by slug or alias, with folders + aliases.

    Returns ``None`` if no matching project (or if the matching project
    has ``status='deleted'`` — callers that need deleted projects must
    look them up by id via :func:`get_project_by_id`).
    """
    pid = resolve_slug(slug)
    if pid is None:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND status != 'deleted'",
            (pid,),
        ).fetchone()
        if not row:
            return None
        return _row_with_children(conn, row)
    finally:
        conn.close()


def get_project_by_id(
    project_id: int, *, include_deleted: bool = False
) -> dict[str, Any] | None:
    """Return a project record by id, with folders + aliases.

    Pass ``include_deleted=True`` to include rows with ``status='deleted'``.
    """
    conn = get_connection()
    try:
        if include_deleted:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ? AND status != 'deleted'",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        return _row_with_children(conn, row)
    finally:
        conn.close()


def _row_with_children(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    """Decorate a projects row with its folders and aliases."""
    result = dict(row)
    pid = row["id"]
    result["folders"] = [
        dict(r) for r in conn.execute(
            "SELECT path, archived FROM project_folders WHERE project_id = ? "
            "ORDER BY archived ASC, path ASC",
            (pid,),
        )
    ]
    result["aliases"] = [
        dict(r) for r in conn.execute(
            "SELECT alias, alias_norm FROM project_aliases WHERE project_id = ? "
            "ORDER BY alias_norm",
            (pid,),
        )
    ]
    return result


def list_projects(
    *, status: str | None = None, include_deleted: bool = False
) -> list[dict[str, Any]]:
    """List projects, optionally filtered.

    By default, rows with ``status='deleted'`` are filtered out. Pass
    ``include_deleted=True`` to include them.

    If ``status`` is set, returns only rows with that status (overrides
    the default deleted filter — pass ``status='deleted'`` to see only
    deleted projects).
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status: {status!r}. Must be one of {VALID_STATUSES}"
        )

    conn = get_connection()
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? "
                "ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            # Derive the CASE order from STATUS_DISPLAY_ORDER so adding
            # a new lifecycle state only requires updating the constant.
            # ``deleted`` is appended last when included; otherwise
            # excluded entirely.
            order = list(STATUS_DISPLAY_ORDER)
            if include_deleted:
                order = order + ["deleted"]
            case_clauses = " ".join(
                f"WHEN '{s}' THEN {i}" for i, s in enumerate(order)
            )
            where = "" if include_deleted else "WHERE status != 'deleted'"
            sql = (
                f"SELECT * FROM projects {where} ORDER BY "
                f"CASE status {case_clauses} END, slug"
            )
            rows = conn.execute(sql).fetchall()
        return [_row_with_children(conn, r) for r in rows]
    finally:
        conn.close()


# ─── Revision-writing core ──────────────────────────────────────────


def _write_revision(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    author: str,
    now: str,
    change_summary: str | None = None,
    user_confirmed_at: str | None = None,
) -> int:
    """Append a revision row capturing the project's full current state.

    Assumes the projects-row + folders + aliases tables already reflect
    the post-mutation state. Returns the new revision_id.
    """
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )

    row = conn.execute(
        "SELECT slug, name, status, description, origin FROM projects "
        "WHERE id = ?",
        (project_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Cannot write revision: project id={project_id} not found"
        )

    cursor = conn.execute(
        "INSERT INTO project_revisions ("
        "  project_id, project_slug, name, status, description, origin,"
        "  author, created_at, user_confirmed_at, change_summary, "
        "  schema_version"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            project_id, row["slug"], row["name"], row["status"],
            row["description"], row["origin"],
            author, now, user_confirmed_at, change_summary,
        ),
    )
    revision_id = cursor.lastrowid
    assert revision_id is not None

    # Snapshot folders + aliases as of right now.
    for r in conn.execute(
        "SELECT path, archived FROM project_folders WHERE project_id = ? "
        "ORDER BY path",
        (project_id,),
    ):
        conn.execute(
            "INSERT INTO project_folders_history "
            "(revision_id, path, archived) VALUES (?, ?, ?)",
            (revision_id, r["path"], r["archived"]),
        )
    for r in conn.execute(
        "SELECT alias, alias_norm FROM project_aliases WHERE project_id = ? "
        "ORDER BY alias_norm",
        (project_id,),
    ):
        conn.execute(
            "INSERT INTO project_aliases_history "
            "(revision_id, alias, alias_norm) VALUES (?, ?, ?)",
            (revision_id, r["alias"], r["alias_norm"]),
        )

    return revision_id


# ─── Project CRUD ───────────────────────────────────────────────────


class _Sentinel:
    """Distinguishes 'not provided' from None in update kwargs."""

_NOT_SET = _Sentinel()


def upsert_project(
    slug: str,
    name: str | None = None,
    *,
    status: str = "active",
    description: str | None = None,
    origin: str = "manual",
    author: str = "user",
    change_summary: str | None = None,
    folders: Iterable[tuple[str, int]] | None = None,
    aliases: Iterable[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Create or update a project identity record.

    On conflict (slug exists), updates fields and writes a revision.
    Does NOT overwrite an existing description with None — pass an
    explicit value to change it. Name follows the same rule.

    ``folders``: iterable of ``(path, archived_int)`` pairs. If
    provided on a create, populates initial folders. If provided on an
    update, REPLACES the folder set (removes any not in the new list).
    Pass ``None`` (default) to leave folders untouched.

    ``aliases``: iterable of ``(display, normalized)`` pairs. Same
    semantics as ``folders`` — provided = replace, None = leave alone.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status: {status!r}. Must be one of {VALID_STATUSES}"
        )
    if origin not in VALID_ORIGINS:
        raise ValueError(
            f"Invalid origin: {origin!r}. Must be one of {VALID_ORIGINS}"
        )
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )

    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        existing = conn.execute(
            "SELECT id, name, description, status, origin FROM projects "
            "WHERE slug = ?",
            (slug,),
        ).fetchone()

        if existing:
            project_id = existing["id"]
            resolved_name = name if name is not None else existing["name"]
            desc = (
                description if description is not None
                else existing["description"]
            )
            conn.execute(
                "UPDATE projects SET name=?, status=?, description=?, "
                "origin=?, updated_at=? WHERE id=?",
                (resolved_name, status, desc, origin, now, project_id),
            )
        else:
            resolved_name = name if name is not None else slug
            cursor = conn.execute(
                "INSERT INTO projects "
                "(slug, name, status, description, origin, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, resolved_name, status, description, origin, now, now),
            )
            project_id = cursor.lastrowid
            assert project_id is not None

        if folders is not None:
            _replace_folders(conn, project_id, folders)
        if aliases is not None:
            _replace_aliases(conn, project_id, aliases)

        summary = change_summary or (
            "created" if existing is None else "upsert"
        )
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=summary,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    event = "project.created" if existing is None else "project.updated"
    _publish_project_event(event, {
        "project_id": project_id, "slug": slug, "status": status,
        "author": author,
    })

    return get_project_by_id(project_id) or {
        "id": project_id, "slug": slug, "name": resolved_name,
        "status": status, "updated_at": now,
    }


def update_project(
    slug: str,
    *,
    name: str | None | _Sentinel = _NOT_SET,
    status: str | _Sentinel = _NOT_SET,
    description: str | None | _Sentinel = _NOT_SET,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any] | None:
    """Update specific fields of a project. Only provided fields change.

    Resolves ``slug`` via :func:`resolve_slug` (so aliases work too).
    Writes a revision capturing the post-update state. Returns the
    updated record, or None if no such project.
    """
    if isinstance(status, str) and status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status: {status!r}. Must be one of {VALID_STATUSES}"
        )
    if author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author: {author!r}. Must be one of {VALID_AUTHORS}"
        )

    pid = resolve_slug(slug)
    if pid is None:
        return None

    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        updates: list[str] = []
        params: list[Any] = []
        if not isinstance(name, _Sentinel):
            updates.append("name=?")
            params.append(name)
        if not isinstance(status, _Sentinel):
            updates.append("status=?")
            params.append(status)
        if not isinstance(description, _Sentinel):
            updates.append("description=?")
            params.append(description)

        if updates:
            updates.append("updated_at=?")
            params.append(now)
            params.append(pid)
            conn.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id=?",
                params,
            )

        _write_revision(
            conn, pid, author=author, now=now,
            change_summary=change_summary or "update",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _publish_project_event("project.updated", {
        "project_id": pid, "slug": slug, "author": author,
    })
    return get_project_by_id(pid)


def delete_project(slug: str, *, author: str = "user") -> bool:
    """Soft-delete a project by setting ``status='deleted'``.

    Returns True if the project existed and was deleted; False if no
    such project was found. Writes a revision capturing the state
    transition. Hard-deleting rows is not exposed here — the revision
    history must remain attached.
    """
    pid = resolve_slug(slug)
    if pid is None:
        return False
    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE projects SET status='deleted', updated_at=? WHERE id=?",
            (now, pid),
        )
        _write_revision(
            conn, pid, author=author, now=now,
            change_summary="soft-delete",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.deleted", {
        "project_id": pid, "slug": slug, "author": author,
    })
    return True


def touch_project(slug: str) -> None:
    """Bump a project's ``updated_at`` without changing any field.

    Does NOT write a revision (no state change). Used by signal-scan
    code to record "we saw this project today" without polluting
    revision history with no-op rows.
    """
    pid = resolve_slug(slug)
    if pid is None:
        return
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (_now(), pid),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Folders ────────────────────────────────────────────────────────


def list_folders(project_id: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT path, archived FROM project_folders "
                "WHERE project_id = ? ORDER BY archived ASC, path ASC",
                (project_id,),
            )
        ]
    finally:
        conn.close()


def add_folder(
    project_id: int, path: str, *,
    archived: bool = False,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Add a folder to a project. No-op if (project_id, path) already exists.

    Logs a warning if the path doesn't exist on disk (per the plan:
    warn but still store — allows for future folders, network paths,
    etc.).
    """
    try:
        if not Path(path).exists():
            logger.warning(
                "Project id=%d: folder does not exist on disk: %s",
                project_id, path,
            )
    except OSError:
        pass

    now = _now()
    archived_int = 1 if archived else 0
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO project_folders "
            "(project_id, path, archived) VALUES (?, ?, ?)",
            (project_id, path, archived_int),
        )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id),
        )
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=change_summary or f"add folder {path}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.folders_changed", {
        "project_id": project_id, "action": "add", "path": path,
        "author": author,
    })
    return get_project_by_id(project_id) or {}


def remove_folder(
    project_id: int, path: str, *,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Remove a folder from a project. No-op if the folder isn't attached."""
    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM project_folders WHERE project_id=? AND path=?",
            (project_id, path),
        )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id),
        )
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=change_summary or f"remove folder {path}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.folders_changed", {
        "project_id": project_id, "action": "remove", "path": path,
        "author": author,
    })
    return get_project_by_id(project_id) or {}


def set_folder_archived(
    project_id: int, path: str, archived: bool, *,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Flip the ``archived`` flag on a folder. Raises if not attached."""
    now = _now()
    archived_int = 1 if archived else 0
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "UPDATE project_folders SET archived=? WHERE project_id=? AND path=?",
            (archived_int, project_id, path),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"Folder {path!r} not attached to project id={project_id}"
            )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id),
        )
        verb = "archive" if archived else "unarchive"
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=change_summary or f"{verb} folder {path}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.folders_changed", {
        "project_id": project_id, "action": verb, "path": path,
        "author": author,
    })
    return get_project_by_id(project_id) or {}


def _replace_folders(
    conn: sqlite3.Connection,
    project_id: int,
    folders: Iterable[tuple[str, int]],
) -> None:
    """Replace a project's folder set. Caller must hold the transaction."""
    conn.execute(
        "DELETE FROM project_folders WHERE project_id=?", (project_id,)
    )
    for path, archived in folders:
        conn.execute(
            "INSERT INTO project_folders (project_id, path, archived) "
            "VALUES (?, ?, ?)",
            (project_id, path, 1 if archived else 0),
        )


# ─── Aliases ────────────────────────────────────────────────────────


def list_aliases(project_id: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT alias, alias_norm FROM project_aliases "
                "WHERE project_id = ? ORDER BY alias_norm",
                (project_id,),
            )
        ]
    finally:
        conn.close()


def add_alias(
    project_id: int, alias: str, *,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Add an alias to a project. Raises ValueError if alias_norm collides
    with another project's alias or a canonical slug.
    """
    alias_norm = _normalize_slug(alias)
    if not alias_norm:
        raise ValueError(f"Empty alias not allowed: {alias!r}")

    now = _now()
    conn = get_connection()
    try:
        # Collision checks against canonical slugs of OTHER projects.
        clash = conn.execute(
            "SELECT id FROM projects WHERE LOWER(slug)=? AND id != ?",
            (alias_norm, project_id),
        ).fetchone()
        if clash:
            raise ValueError(
                f"Alias {alias!r} collides with canonical slug of "
                f"project id={clash['id']}"
            )
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR IGNORE INTO project_aliases "
            "(project_id, alias, alias_norm) VALUES (?, ?, ?)",
            (project_id, alias, alias_norm),
        )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id),
        )
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=change_summary or f"add alias {alias}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.aliases_changed", {
        "project_id": project_id, "action": "add", "alias": alias,
        "author": author,
    })
    return get_project_by_id(project_id) or {}


def remove_alias(
    project_id: int, alias: str, *,
    author: str = "user",
    change_summary: str | None = None,
) -> dict[str, Any]:
    """Remove an alias from a project. Matches by ``alias_norm``."""
    alias_norm = _normalize_slug(alias)
    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM project_aliases WHERE project_id=? AND alias_norm=?",
            (project_id, alias_norm),
        )
        conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now, project_id),
        )
        _write_revision(
            conn, project_id, author=author, now=now,
            change_summary=change_summary or f"remove alias {alias}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _publish_project_event("project.aliases_changed", {
        "project_id": project_id, "action": "remove", "alias": alias,
        "author": author,
    })
    return get_project_by_id(project_id) or {}


def _replace_aliases(
    conn: sqlite3.Connection,
    project_id: int,
    aliases: Iterable[tuple[str, str]],
) -> None:
    """Replace a project's alias set. Caller must hold the transaction.

    Each item is ``(display, normalized)``. If ``normalized`` is empty,
    it's recomputed from ``display``.
    """
    conn.execute(
        "DELETE FROM project_aliases WHERE project_id=?", (project_id,)
    )
    for alias, alias_norm in aliases:
        norm = alias_norm or _normalize_slug(alias)
        if not norm:
            continue
        conn.execute(
            "INSERT INTO project_aliases (project_id, alias, alias_norm) "
            "VALUES (?, ?, ?)",
            (project_id, alias, norm),
        )


# ─── Revisions ──────────────────────────────────────────────────────


def list_revisions(
    project_id: int, *, limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return revisions for a project, newest first.

    Each row includes the snapshot fields plus a ``folders`` and
    ``aliases`` list (joined from the history tables).
    """
    conn = get_connection()
    try:
        sql = (
            "SELECT * FROM project_revisions WHERE project_id = ? "
            "ORDER BY created_at DESC"
        )
        params: tuple[Any, ...] = (project_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (project_id, limit)
        revisions = conn.execute(sql, params).fetchall()

        out: list[dict[str, Any]] = []
        for rev in revisions:
            rev_dict = dict(rev)
            rid = rev["id"]
            rev_dict["folders"] = [
                dict(r) for r in conn.execute(
                    "SELECT path, archived FROM project_folders_history "
                    "WHERE revision_id = ? ORDER BY path",
                    (rid,),
                )
            ]
            rev_dict["aliases"] = [
                dict(r) for r in conn.execute(
                    "SELECT alias, alias_norm FROM project_aliases_history "
                    "WHERE revision_id = ? ORDER BY alias_norm",
                    (rid,),
                )
            ]
            out.append(rev_dict)
        return out
    finally:
        conn.close()


def get_state_at(
    project_id: int, timestamp: str,
) -> dict[str, Any] | None:
    """Return the latest revision with ``created_at <= timestamp``.

    Result includes ``folders`` and ``aliases`` joined from the history
    tables. Returns ``None`` if no revision predates the given moment.
    """
    conn = get_connection()
    try:
        rev = conn.execute(
            "SELECT * FROM project_revisions "
            "WHERE project_id = ? AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id, timestamp),
        ).fetchone()
        if not rev:
            return None
        rev_dict = dict(rev)
        rid = rev["id"]
        rev_dict["folders"] = [
            dict(r) for r in conn.execute(
                "SELECT path, archived FROM project_folders_history "
                "WHERE revision_id = ? ORDER BY path",
                (rid,),
            )
        ]
        rev_dict["aliases"] = [
            dict(r) for r in conn.execute(
                "SELECT alias, alias_norm FROM project_aliases_history "
                "WHERE revision_id = ? ORDER BY alias_norm",
                (rid,),
            )
        ]
        return rev_dict
    finally:
        conn.close()


def confirm_description(
    project_id: int, *, confirmed_at: str | None = None,
) -> int | None:
    """Mark the latest revision's ``user_confirmed_at`` as set.

    By default uses the current time; pass an explicit ISO timestamp
    to override. Returns the revision id touched, or ``None`` if the
    project has no revisions yet.
    """
    ts = confirmed_at or _now()
    conn = get_connection()
    try:
        rev = conn.execute(
            "SELECT id FROM project_revisions WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if not rev:
            return None
        conn.execute(
            "UPDATE project_revisions SET user_confirmed_at = ? WHERE id = ?",
            (ts, rev["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    _publish_project_event("project.description_confirmed", {
        "project_id": project_id, "revision_id": rev["id"],
    })
    return rev["id"]
