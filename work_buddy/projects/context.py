"""Read-only context rendering for the native Projects authority."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Iterable

from work_buddy.projects.store import STATUS_DISPLAY_ORDER


_DEFAULT_STATUSES = ("active",)


def render_native_projects_context(
    *,
    statuses: Iterable[str] | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Render authoritative project rows without scanning legacy sources.

    The database is opened in SQLite read-only/query-only mode.  Callers must
    check that native authority is active before invoking this function.
    """

    if db_path is None:
        from work_buddy.projects.store import _db_path

        path = _db_path()
    else:
        path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("the authoritative Projects database is unavailable")

    selected = tuple(statuses) if statuses is not None else _DEFAULT_STATUSES
    allowed = set(selected)
    allowed.discard("deleted")
    unknown = allowed - set(STATUS_DISPLAY_ORDER)
    if unknown:
        raise ValueError("unsupported Project context status")

    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT slug,name,status,description,updated_at FROM projects "
            "WHERE status!='deleted' ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 "
            "WHEN 'future' THEN 2 WHEN 'past' THEN 3 ELSE 9 END,slug"
        ).fetchall()
    finally:
        connection.close()

    by_status: dict[str, list[sqlite3.Row]] = {
        status: [] for status in STATUS_DISPLAY_ORDER
    }
    for row in rows:
        status = str(row["status"])
        if status in allowed:
            by_status[status].append(row)

    sections = ["# Projects\n"]
    rendered = 0
    for status in STATUS_DISPLAY_ORDER:
        entries = by_status[status]
        if not entries:
            continue
        if rendered:
            sections.append("")
        sections.append(f"## {status.title()}\n")
        for row in entries:
            lines = [
                f"### {row['slug']}",
                f"**Name:** {row['name']}  ",
                f"**Status:** {status}  ",
                "**Evidence:** sqlite",
            ]
            if row["description"]:
                lines.append(f"**Description:** {row['description']}")
            lines.append(f"**Updated:** {row['updated_at']}")
            sections.append("\n".join(lines))
            rendered += 1
    if not rendered:
        return "# Projects\n\nNo projects in the selected native lifecycle lanes.\n"
    return "\n\n".join(sections) + "\n"


__all__ = ["render_native_projects_context"]
