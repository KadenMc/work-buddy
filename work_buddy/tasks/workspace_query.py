"""Paged task browsing without loading task aggregates or document bodies.

Filters and ordering apply to the complete collection in SQLite. Facets omit
their own restriction so a selected value never hides the alternatives. All
parts of a response share one read transaction and collection revision.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .store import TaskStore


STATUSES = ("open", "completed", "archived", "trash")
ATTENTION = ("inbox", "mit", "focused", "active", "waiting", "snoozed")
URGENCIES = ("low", "medium", "high")
NONE = "__none__"
UNRESOLVED = "__unresolved__"
_STATUS = (
    "CASE WHEN t.deleted_at IS NOT NULL THEN 'trash' "
    "WHEN t.archived_at IS NOT NULL THEN 'archived' "
    "WHEN t.state = 'done' THEN 'completed' ELSE 'open' END"
)
_DOCUMENT = (
    "EXISTS (SELECT 1 FROM task_document_links d WHERE d.task_id = t.task_id "
    "AND d.lifecycle NOT IN ('retired', 'deleted'))"
)


@dataclass(frozen=True, slots=True)
class WorkspaceQuery:
    statuses: tuple[str, ...] = ("open",)
    namespaces: tuple[str, ...] = ()
    exact_namespaces: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    attention: tuple[str, ...] = ()
    urgencies: tuple[str, ...] = ()
    q: str = ""
    due: str = ""
    note: str = ""
    sort: str = "created_at"
    direction: str = "desc"
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        # Reject unsupported fields before interpolation into ORDER BY.
        if self.sort not in {"created_at", "updated_at", "title", "due_date", "urgency"}:
            raise ValueError("Unsupported task sort field")
        if self.direction not in {"asc", "desc"}:
            raise ValueError("Task sort direction must be asc or desc")
        for field, values, accepted in (
            ("status", self.statuses, STATUSES),
            ("attention", self.attention, (*ATTENTION, "done")),
            ("urgency", self.urgencies, URGENCIES),
        ):
            if any(value not in accepted for value in values):
                raise ValueError(f"Unsupported task {field}")
        if self.note not in {"", "yes", "no"}:
            raise ValueError("Document filter must be yes or no")
        projects: list[str] = []
        for project in self.projects:
            value = str(project).strip()
            if value in {NONE, UNRESOLVED}:
                projects.append(value)
            elif value.startswith("unresolved:") and value.removeprefix("unresolved:").strip():
                projects.append("unresolved:" + value.removeprefix("unresolved:").strip())
            else:
                try:
                    if not value.isdigit() or int(value) <= 0:
                        raise ValueError
                    projects.append(str(int(value)))
                except ValueError as exc:
                    raise ValueError("Project filters must identify registered projects") from exc
        object.__setattr__(self, "projects", tuple(dict.fromkeys(projects)))
        if self.due and self.due not in {"today", "overdue", "none", "week", "upcoming"}:
            try:
                date.fromisoformat(self.due)
            except ValueError as exc:
                raise ValueError("Unsupported task due date filter") from exc
        object.__setattr__(self, "limit", max(1, min(int(self.limit), 200)))
        object.__setattr__(self, "offset", max(0, int(self.offset)))


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(
    query: WorkspaceQuery, *, exclude: str = "", today: date
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for name, expression, values in (
        ("statuses", _STATUS, query.statuses),
        ("attention", "t.state", query.attention),
        ("urgencies", "t.urgency", query.urgencies),
    ):
        if values and exclude != name:
            clauses.append(f"{expression} IN ({_placeholders(values)})")
            params.extend(values)
    if query.q.strip():
        needle = query.q.strip().casefold()
        clauses.append(
            "(instr(unicode_casefold(t.description), ?) > 0 OR EXISTS "
            "(SELECT 1 FROM task_tags qt WHERE qt.task_id = t.task_id "
            "AND instr(unicode_casefold(qt.tag), ?) > 0))"
        )
        params.extend((needle, needle))
    if (query.namespaces or query.exact_namespaces) and exclude != "namespaces":
        alternatives: list[str] = []
        for namespace, exact in (
            *((value, False) for value in query.namespaces),
            *((value, True) for value in query.exact_namespaces),
        ):
            if namespace == NONE:
                alternatives.append(
                    "NOT EXISTS (SELECT 1 FROM task_tags nt WHERE nt.task_id = t.task_id "
                    "AND nt.is_namespace = 1)"
                )
                continue
            # A trailing separator can be a real historical assignment. Keep
            # it distinct from its parent when filtering the inventory row.
            namespace = namespace.strip().lstrip("#/")
            if not namespace:
                continue
            condition = "nt.tag = ? COLLATE NOCASE"
            params.append(namespace)
            if not exact:
                condition += " OR nt.tag LIKE ? ESCAPE '\\' COLLATE NOCASE"
                params.append(f"{_escape_like(namespace)}/%")
            alternatives.append(
                "EXISTS (SELECT 1 FROM task_tags nt WHERE nt.task_id = t.task_id "
                f"AND nt.is_namespace = 1 AND ({condition}))"
            )
        if alternatives:
            clauses.append("(" + " OR ".join(alternatives) + ")")
    if query.projects and exclude != "projects":
        alternatives = []
        ids = []
        for project in query.projects:
            value = str(project)
            if value == NONE:
                alternatives.append(
                    "(NOT EXISTS (SELECT 1 FROM task_projects p WHERE p.task_id = t.task_id) "
                    "AND NOT EXISTS (SELECT 1 FROM task_project_unresolved u "
                    "WHERE u.task_id = t.task_id))"
                )
            elif value == UNRESOLVED:
                alternatives.append(
                    "EXISTS (SELECT 1 FROM task_project_unresolved u WHERE u.task_id = t.task_id)"
                )
            elif value.startswith("unresolved:"):
                alternatives.append(
                    "EXISTS (SELECT 1 FROM task_project_unresolved u WHERE u.task_id = t.task_id "
                    "AND u.legacy_value = ? COLLATE NOCASE)"
                )
                params.append(value.removeprefix("unresolved:"))
            else:
                try:
                    ids.append(int(value))
                except ValueError as exc:
                    raise ValueError("Project filters must identify registered projects") from exc
        if ids:
            alternatives.append(
                "EXISTS (SELECT 1 FROM task_projects p WHERE p.task_id = t.task_id "
                f"AND p.project_id IN ({_placeholders(ids)}))"
            )
            params.extend(ids)
        clauses.append("(" + " OR ".join(alternatives) + ")")
    if query.note:
        clauses.append(_DOCUMENT if query.note == "yes" else "NOT " + _DOCUMENT)
    if query.due:
        if query.due == "none":
            clauses.append("NULLIF(t.due_date, '') IS NULL")
        elif query.due == "today":
            clauses.append("t.due_date = ?")
            params.append(today.isoformat())
        elif query.due == "overdue":
            clauses.append("NULLIF(t.due_date, '') IS NOT NULL AND t.due_date < ?")
            params.append(today.isoformat())
        elif query.due == "upcoming":
            clauses.append("t.due_date > ?")
            params.append(today.isoformat())
        elif query.due == "week":
            clauses.append("t.due_date BETWEEN ? AND ?")
            params.extend((today.isoformat(), (today + timedelta(days=7)).isoformat()))
        else:
            clauses.append("t.due_date = ?")
            params.append(query.due)
    return " AND ".join(clauses) or "1 = 1", params


def _order(query: WorkspaceQuery) -> str:
    if query.sort == "title":
        value = "COALESCE(t.description, '') COLLATE NOCASE"
        missing = ""
    elif query.sort == "urgency":
        value = "CASE t.urgency WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END"
        missing = ""
    else:
        # julianday compares real instants, including imported UTC offsets.
        # Unknown/invalid imported dates remain last in both directions.
        value = (
            f"CASE WHEN t.{query.sort} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
            f"THEN julianday(t.{query.sort}) END"
        )
        missing = f"({value} IS NULL) ASC, "
    return f"{missing}{value} {query.direction.upper()}, t.task_id ASC"


def _groups(
    conn: sqlite3.Connection, query: WorkspaceQuery, field: str, expression: str, today: date
) -> dict[str, int]:
    where, params = _where(query, exclude=field, today=today)
    return {
        str(row["value"]): int(row["count"])
        for row in conn.execute(
            f"SELECT {expression} AS value, COUNT(*) AS count FROM task_metadata t "
            f"WHERE {where} GROUP BY value", params
        )
    }


def _namespace_inventory(
    conn: sqlite3.Connection, query: WorkspaceQuery, today: date
) -> tuple[list[dict[str, Any]], int]:
    where, params = _where(query, exclude="namespaces", today=today)
    # The picker follows every other filter; selecting a namespace must still
    # leave matching alternatives available. Manage namespaces has its own
    # complete inventory. Fetch only matching assignment identities and names.
    # Building the ancestor sets
    # here avoids SQLite repeatedly materializing/sorting the same recursive
    # paths (one row per task per ancestor) while retaining unique task counts.
    rows = conn.execute(
        f"WITH scoped AS MATERIALIZED (SELECT t.task_id FROM task_metadata t WHERE {where}) "
        "SELECT nt.task_id, nt.tag "
        "FROM task_tags nt JOIN scoped s ON s.task_id = nt.task_id "
        "WHERE nt.is_namespace = 1 AND nt.tag != ''", params
    )
    members: dict[str, set[str]] = {}
    direct: dict[str, set[str]] = {}
    for row in rows:
        segments = str(row["tag"]).split("/")
        for length in range(1, len(segments) + 1):
            path = "/".join(segments[:length])
            members.setdefault(path, set())
            direct.setdefault(path, set())
            members[path].add(row["task_id"])
            if length == len(segments):
                direct[path].add(row["task_id"])
    inventory = {
        path: {"path": path, "count": len(task_ids), "direct_count": len(direct[path])}
        for path, task_ids in members.items()
    }
    # Keep zero-result selections reachable so they can be removed, including
    # every ancestor needed to render their hierarchy and exact path spelling.
    for selected in (*query.namespaces, *query.exact_namespaces):
        if selected == NONE:
            continue
        segments = selected.strip().lstrip("#/").split("/")
        for length in range(1, len(segments) + 1):
            path = "/".join(segments[:length])
            if path:
                inventory.setdefault(path, {"path": path, "count": 0, "direct_count": 0})
    for path, item in inventory.items():
        parent, _, label = path.rpartition("/")
        item.update(parent=parent or None, label=label or "(empty segment)")
    no_namespace = int(conn.execute(
        f"SELECT COUNT(*) FROM task_metadata t WHERE {where} "
        "AND NOT EXISTS (SELECT 1 FROM task_tags nt WHERE nt.task_id = t.task_id "
        "AND nt.is_namespace = 1)", params
    ).fetchone()[0])
    return sorted(inventory.values(), key=lambda node: (node["path"].casefold(), node["path"])), no_namespace


def _namespace_options(conn: sqlite3.Connection) -> list[str]:
    """Authoring can reuse any namespace regardless of browsing filters."""
    paths: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT tag FROM task_tags WHERE is_namespace = 1 AND tag != ''"
    ):
        segments = str(row["tag"]).split("/")
        paths.update("/".join(segments[:length]) for length in range(1, len(segments) + 1))
    return sorted(paths, key=lambda path: (path.casefold(), path))


def _project_inventory(
    conn: sqlite3.Connection, query: WorkspaceQuery, today: date
) -> dict[str, int]:
    where, params = _where(query, exclude="projects", today=today)
    rows = conn.execute(
        f"WITH scoped AS MATERIALIZED (SELECT t.task_id FROM task_metadata t WHERE {where}) "
        "SELECT CAST(p.project_id AS TEXT) AS value, COUNT(DISTINCT s.task_id) AS count "
        "FROM task_projects p LEFT JOIN scoped s ON s.task_id = p.task_id GROUP BY p.project_id "
        "UNION ALL SELECT 'unresolved:' || u.legacy_value AS value, "
        "COUNT(DISTINCT s.task_id) AS count FROM task_project_unresolved u "
        "LEFT JOIN scoped s ON s.task_id = u.task_id GROUP BY u.legacy_value", params
    )
    counts = {str(row["value"]): int(row["count"]) for row in rows}
    special = conn.execute(
        "SELECT SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM task_projects p "
        "WHERE p.task_id = t.task_id) AND NOT EXISTS (SELECT 1 FROM task_project_unresolved u "
        "WHERE u.task_id = t.task_id) THEN 1 ELSE 0 END) AS unassigned, "
        "SUM(CASE WHEN EXISTS (SELECT 1 FROM task_project_unresolved u "
        "WHERE u.task_id = t.task_id) THEN 1 ELSE 0 END) AS unresolved "
        f"FROM task_metadata t WHERE {where}", params
    ).fetchone()
    counts[NONE] = int(special["unassigned"] or 0)
    counts[UNRESOLVED] = int(special["unresolved"] or 0)
    for selected in query.projects:
        counts.setdefault(str(selected), 0)
    return counts


def _summaries(conn: sqlite3.Connection, query: WorkspaceQuery, today: date) -> list[dict[str, Any]]:
    where, params = _where(query, today=today)
    rows = conn.execute(
        "SELECT t.task_id, COALESCE(t.description, '') AS title, t.revision, "
        "t.state AS attention_state, t.urgency, t.due_date, t.deadline_date, t.snooze_until, "
        "t.completed_at, t.archived_at, t.deleted_at, "
        "NULLIF(t.created_at, '') AS created_at, NULLIF(t.updated_at, '') AS updated_at, "
        f"{_STATUS} AS status, {_DOCUMENT} AS has_document, "
        "(SELECT a.description FROM task_action_items a WHERE a.id = t.current_action_item_id "
        "AND a.task_id = t.task_id AND a.deleted_at IS NULL) AS current_action "
        f"FROM task_metadata t WHERE {where} ORDER BY {_order(query)} LIMIT ? OFFSET ?",
        [*params, query.limit, query.offset],
    )
    result = [dict(row) for row in rows]
    if not result:
        return result
    by_id = {item["task_id"]: item for item in result}
    for item in result:
        item.update(tags=[], namespaces=[], project_ids=[], unresolved_projects=[])
        item["has_document"] = bool(item["has_document"])
    ids = list(by_id)
    placeholders = _placeholders(ids)
    for row in conn.execute(
        f"SELECT task_id, tag, is_namespace FROM task_tags WHERE task_id IN ({placeholders}) "
        "ORDER BY tag COLLATE NOCASE, tag", ids
    ):
        item = by_id[row["task_id"]]
        item["tags"].append(row["tag"])
        if row["is_namespace"]:
            item["namespaces"].append(row["tag"])
    for row in conn.execute(
        f"SELECT task_id, project_id FROM task_projects WHERE task_id IN ({placeholders}) "
        "ORDER BY project_id", ids
    ):
        by_id[row["task_id"]]["project_ids"].append(int(row["project_id"]))
    for row in conn.execute(
        "SELECT task_id, legacy_value, source_tag, reason, candidate_ids_json "
        f"FROM task_project_unresolved WHERE task_id IN ({placeholders}) ORDER BY legacy_value, source_tag", ids
    ):
        unresolved = dict(row)
        item = by_id[unresolved.pop("task_id")]
        try:
            candidate_ids = json.loads(unresolved.pop("candidate_ids_json") or "[]")
        except (TypeError, ValueError):
            candidate_ids = []
        unresolved["candidate_ids"] = candidate_ids if isinstance(candidate_ids, list) else []
        item["unresolved_projects"].append(unresolved)
    return result


def read_workspace(
    store: TaskStore, query: WorkspaceQuery | None = None, *, today: date | None = None
) -> dict[str, Any]:
    """Read an initialized store. The API owns schema initialization and labels.

    This deliberately uses a read-only connection: browsing cannot migrate a
    store, hydrate full tasks, or read document bodies. Page associations are
    loaded in batches; current-action lookup uses its primary key.
    """
    query = query or WorkspaceQuery()
    today = today or date.today()
    conn = store.connect_readonly()
    # SQLite's built-in NOCASE only folds ASCII. Retain the former Python
    # search's Unicode casefold semantics without loading task aggregates.
    conn.create_function("unicode_casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
    try:
        conn.execute("BEGIN")
        revision = store.collection_revision_in_connection(conn)
        where, params = _where(query, today=today)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM task_metadata t WHERE {where}", params
        ).fetchone()[0])
        tasks = _summaries(conn, query, today)
        statuses = {value: 0 for value in STATUSES}
        statuses.update(_groups(conn, query, "statuses", _STATUS, today))
        attention = {value: 0 for value in ATTENTION}
        attention.update(_groups(conn, query, "attention", "t.state", today))
        attention.pop("done", None)
        urgencies = {value: 0 for value in URGENCIES}
        urgencies.update(_groups(conn, query, "urgencies", "t.urgency", today))
        namespace_tree, no_namespace = _namespace_inventory(conn, query, today)
        namespaces = {item["path"]: item["count"] for item in namespace_tree}
        namespaces[NONE] = no_namespace
        projects = _project_inventory(conn, query, today)
        contracts = [str(row[0]) for row in conn.execute(
            "SELECT DISTINCT contract FROM task_metadata WHERE NULLIF(contract, '') IS NOT NULL "
            "ORDER BY contract COLLATE NOCASE, contract"
        )]
        contexts = [str(row[0]) for row in conn.execute(
            "SELECT DISTINCT j.value FROM task_metadata t, "
            "json_each(CASE WHEN json_valid(t.agent_required_contexts) "
            "THEN t.agent_required_contexts ELSE '[]' END) j WHERE j.type = 'text' "
            "UNION SELECT DISTINCT j.value FROM task_metadata t, "
            "json_each(CASE WHEN json_valid(t.user_required_contexts) "
            "THEN t.user_required_contexts ELSE '[]' END) j WHERE j.type = 'text' ORDER BY 1"
        )]
        option = lambda value: {"value": value, "label": value}
        return {
            "collection_revision": revision,
            "tasks": tasks,
            "total": total,
            "page": {"limit": query.limit, "offset": query.offset,
                     "has_more": query.offset + len(tasks) < total},
            "facets": {"statuses": statuses, "attention": attention, "urgencies": urgencies,
                       "namespaces": namespaces, "projects": projects},
            "namespace_tree": namespace_tree,
            "options": {"namespaces": [option(path) for path in _namespace_options(conn)],
                        "projects": [option(value) for value in projects],
                        "contracts": [option(value) for value in contracts],
                        "contexts": [option(value) for value in contexts]},
        }
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
