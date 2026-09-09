"""Registry identity resolution for task links, independent of namespaces.

The registry is read-only here. Missing, renamed or deleted projects never
remove task links, and historical ambiguity is retained for an explicit edit.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .errors import TaskValidationError

if TYPE_CHECKING:
    from .store import TaskStore


def project_database_path() -> Path:
    from work_buddy.config import load_config
    from work_buddy.paths import repo_root, resolve
    configured = (load_config().get("projects") or {}).get("db_path")
    if configured:
        path = Path(str(configured)).expanduser()
        return path if path.is_absolute() else repo_root() / path
    return resolve("db/projects")


def _normalized(value: str) -> str:
    return value.strip().casefold().replace("_", "-").replace(" ", "-")


def registry_snapshot(path: str | Path | None = None) -> dict[int, dict[str, Any]] | None:
    """Return a consistent registry snapshot, including historical identities.

    Never initializes/migrates another authority while holding a task lock.
    None means unavailable; an empty mapping is an available empty registry.
    """
    conn = None
    try:
        target = (Path(path) if path is not None else project_database_path()).resolve()
        if not target.is_file():
            return None
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        result = {
            int(row["id"]): {**dict(row), "aliases": []}
            for row in conn.execute("SELECT id,slug,name,status FROM projects")
        }
        for row in conn.execute("SELECT project_id,alias_norm FROM project_aliases"):
            if int(row["project_id"]) in result:
                result[int(row["project_id"])]["aliases"].append(str(row["alias_norm"]))
        return result
    except (OSError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()


def candidates(value: str, registry: dict[int, dict[str, Any]]) -> tuple[int, ...]:
    normalized = _normalized(value)
    return tuple(sorted(
        project_id for project_id, project in registry.items()
        if normalized in {_normalized(str(project["slug"])), *(_normalized(alias) for alias in project["aliases"])}
    ))


def normalize_project_ids(values: Iterable[int | str]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise TaskValidationError({"project_ids": "Projects must be a list of registry IDs."})
    result: set[int] = set()
    try:
        for value in values:
            if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
                raise ValueError
            result.add(int(value))
    except (TypeError, ValueError) as exc:
        raise TaskValidationError({"project_ids": "Use positive integer registry project IDs."}) from exc
    return tuple(sorted(result))


def resolve_legacy_projects(
    values: Iterable[str], registry: dict[int, dict[str, Any]] | None,
) -> tuple[tuple[int, ...], tuple[dict[str, Any], ...]]:
    ids: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    for value in sorted(set(str(item).strip() for item in values if str(item).strip())):
        found = candidates(value, registry) if registry is not None else ()
        if len(found) == 1:
            ids.add(found[0])
        else:
            unresolved.append({
                "legacy_value": value, "source_tag": "",
                "reason": "registry_unavailable" if registry is None else "ambiguous" if found else "unmatched",
                "candidate_ids": list(found),
            })
    return tuple(sorted(ids)), tuple(unresolved)


def validate_project_ids(
    values: Iterable[int], registry: dict[int, dict[str, Any]] | None,
    *, existing: Iterable[int] = (),
) -> None:
    additions = set(values) - set(existing)
    if additions and registry is None:
        raise TaskValidationError({"project_ids": "Project registry is unavailable. Retry linking projects when it is available."})
    missing = additions - set(registry or {})
    if missing:
        raise TaskValidationError({"project_ids": "Unknown registry project IDs: " + ", ".join(map(str, sorted(missing)))})


def finish_legacy_backfill(
    conn: sqlite3.Connection,
    registry: dict[int, dict[str, Any]] | None,
    *,
    store: TaskStore,
) -> None:
    """Finish only migration-captured links; never interpret current tag names.

    The log retains every original source path, including resolved links.
    Unmatched/ambiguous decisions are frozen until the user resolves them.
    Resolution may happen after a client has read the pending associations,
    so it uses the same task/collection revision and audit fences as an edit.
    """
    if registry is None:
        return
    # Import only for the exceptional migration path. Every operation below
    # uses this connection; opening another TaskStore connection would recurse.
    from .models import MutationReceipt, MutationResult
    from .service import TaskApplicationService, _Change

    service = TaskApplicationService(store)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM task_project_unresolved WHERE reason='pending' "
            "ORDER BY task_id,legacy_value,source_tag"
        ).fetchall()
        prior_tasks = {}
        resolutions: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = (row["task_id"], row["legacy_value"], row["source_tag"])
            task_id = str(row["task_id"])
            if task_id not in prior_tasks:
                prior_tasks[task_id] = store.get_in_connection(conn, task_id, include_deleted=True)
            found = candidates(str(row["legacy_value"]), registry)
            resolution = "resolved" if len(found) == 1 else "ambiguous" if found else "unmatched"
            if len(found) == 1:
                conn.execute("INSERT OR IGNORE INTO task_projects(task_id,project_id) VALUES(?,?)", (row["task_id"], found[0]))
                conn.execute("DELETE FROM task_project_unresolved WHERE task_id=? AND legacy_value=? AND source_tag=?", key)
            else:
                conn.execute(
                    "UPDATE task_project_unresolved SET reason=?,candidate_ids_json=? WHERE task_id=? AND legacy_value=? AND source_tag=?",
                    (resolution, json.dumps(found), *key),
                )
            conn.execute(
                "UPDATE task_project_migration_log SET resolution=?,project_id=? WHERE task_id=? AND source_tag=?",
                (resolution, found[0] if len(found) == 1 else None, row["task_id"], row["source_tag"]),
            )
            resolutions.setdefault(task_id, []).append({
                "legacy_value": row["legacy_value"], "source_tag": row["source_tag"],
                "resolution": resolution, "candidate_ids": list(found),
            })
        for task_id, prior in prior_tasks.items():
            assert prior is not None
            now = service._now()
            actor = "system:project-migration"
            mutation = "project.backfill"
            client_mutation_id = f"legacy-project-backfill:{task_id}:{prior.revision}"
            service._update_task_cas(conn, task_id, prior.revision, now, actor)
            task = store.get_in_connection(conn, task_id, include_deleted=True)
            assert task is not None
            collection_revision = service._next_collection_revision(conn, now)
            details = {
                "project_ids": {"before": list(prior.project_ids), "after": list(task.project_ids)},
                "unresolved_projects": {"before": list(prior.unresolved_projects), "after": list(task.unresolved_projects)},
                "resolutions": resolutions[task_id],
            }
            receipt = MutationReceipt(
                receipt_id=service._receipt_id_factory(),
                client_mutation_id=client_mutation_id, actor=actor, session_id=None,
                mutation=mutation,
                request_hash=service._request_hash(mutation, {"expected_revision": prior.revision, **details}),
                status="completed", created_at=now, completed_at=now,
            )
            result = MutationResult(task=task, collection_revision=collection_revision, receipt=receipt)
            conn.execute(
                "INSERT INTO task_mutation_receipts "
                "(receipt_id,client_mutation_id,actor,mutation,task_id,request_hash,status,result_json,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (receipt.receipt_id, client_mutation_id, actor, mutation, task_id,
                 receipt.request_hash, receipt.status, service._canonical_json(result.to_dict()), now, now),
            )
            service._append_history(
                conn, task=task,
                change=_Change(task_id, prior.state, task.state, True, details, "Resolved historical project associations"),
                mutation=mutation, actor=actor, session_id=None, receipt_id=receipt.receipt_id,
                collection_revision=collection_revision, now=now,
            )
            service._append_outbox(conn, task=task, mutation=mutation, collection_revision=collection_revision, now=now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def write_links(conn: sqlite3.Connection, task_id: str, ids: Iterable[int], unresolved: Iterable[dict[str, Any]] = ()) -> None:
    conn.executemany("INSERT OR IGNORE INTO task_projects(task_id,project_id) VALUES(?,?)", [(task_id, project_id) for project_id in ids])
    conn.executemany(
        "INSERT OR IGNORE INTO task_project_unresolved(task_id,legacy_value,source_tag,reason,candidate_ids_json) VALUES(?,?,?,?,?)",
        [(task_id, item["legacy_value"], item.get("source_tag", ""), item["reason"], json.dumps(item.get("candidate_ids", []))) for item in unresolved],
    )


def stage_imported_project_tags(conn: sqlite3.Connection, task_id: str, tags: Iterable[str]) -> None:
    """Explicit legacy-import boundary, never called by ordinary tag editing."""
    for tag in tags:
        if not str(tag).casefold().startswith('projects/'):
            continue
        value = str(tag).split('/', 2)[1]
        if not value or conn.execute('SELECT 1 FROM task_project_migration_log WHERE task_id=? AND source_tag=?', (task_id, tag)).fetchone():
            continue
        conn.execute("INSERT INTO task_project_migration_log(task_id,source_tag,legacy_value,resolution) VALUES(?,?,?,'pending')", (task_id, tag, value))
        conn.execute("INSERT OR IGNORE INTO task_project_unresolved(task_id,legacy_value,source_tag,reason) VALUES(?,?,?,'pending')", (task_id, value, tag))
