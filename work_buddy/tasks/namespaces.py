"""Reviewed, atomic namespace organization with revision-aware durable undo."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from typing import Any, Mapping

from .errors import TaskDomainError, TaskIdempotencyConflict, TaskValidationError
from .models import Tag, Task
from .service import TaskApplicationService, _Change

# Match native task tag syntax, including historical suffixes such as /-draft.
# Paths are metadata, never interpreted as filesystem locations.
_PATH = re.compile(r"^[a-z0-9][a-z0-9_/-]*$")
_ACTIONS = {"rename", "move", "merge", "promote", "remove", "assign"}
_STATUSES = ("open", "completed", "archived", "trash")


class NamespaceConflict(TaskDomainError):
    code = "task_namespace_conflict"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path(value: Any, field: str, *, root: bool = False) -> str:
    if not isinstance(value, str):
        raise TaskValidationError({field: "Choose a namespace path."})
    result = value.strip().lstrip("#").casefold()
    if root and result == "":
        return result
    if not _PATH.fullmatch(result):
        raise TaskValidationError({field: "Use slash-separated names with letters, numbers, hyphens, or underscores."})
    return result


def _paths(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 500:
        raise TaskValidationError({field: "Choose at most 500 namespace paths."})
    return sorted({_path(item, field) for item in value})


def _under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _roots(paths: list[str]) -> list[str]:
    return [path for path in paths if not any(_under(path, other) for other in paths if path != other)]


def _join(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _status(row: Mapping[str, Any]) -> str:
    if row["deleted_at"]:
        return "trash"
    if row["archived_at"]:
        return "archived"
    return "completed" if row["state"] == "done" else "open"


def _counts() -> dict[str, int]:
    return dict.fromkeys(_STATUSES, 0)


def _canonical_request(body: Mapping[str, Any]) -> dict[str, Any]:
    action = body.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise TaskValidationError({"action": "Choose a namespace action."})
    request: dict[str, Any] = {"action": action}
    for key in ("include_descendants", "merge_collisions"):
        if key in body and not isinstance(body[key], bool):
            raise TaskValidationError({key: "Choose yes or no."})
    if action == "assign":
        ids = body.get("task_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 5000 or any(not isinstance(i, str) or not i for i in ids):
            raise TaskValidationError({"task_ids": "Select between 1 and 5,000 tasks."})
        mode = body.get("assignment_mode")
        if not isinstance(mode, str) or mode not in {"add", "remove", "replace"}:
            raise TaskValidationError({"assignment_mode": "Choose Add, Remove, or Replace."})
        request.update(task_ids=sorted(set(ids)), assignment_mode=mode, namespaces=_paths(body.get("namespaces", []), "namespaces"))
        if mode != "replace" and not request["namespaces"]:
            raise TaskValidationError({"namespaces": "Choose at least one namespace."})
        return request
    sources = _paths(body.get("sources", []), "sources")
    if not sources:
        raise TaskValidationError({"sources": "Choose at least one namespace."})
    if action == "rename" and len(sources) != 1:
        raise TaskValidationError({"sources": "Rename one namespace at a time."})
    include = True if action in {"rename", "move", "promote"} else body.get("include_descendants", True)
    request.update(sources=_roots(sources) if include else sources, include_descendants=include, merge_collisions=body.get("merge_collisions", False))
    if action == "rename":
        name = _path(body.get("name"), "name")
        if "/" in name:
            raise TaskValidationError({"name": "Use a single name; use Move to change its parent."})
        request["name"] = name
    if action in {"move", "merge"}:
        request["destination"] = _path(body.get("destination"), "destination", root=action == "move")
        if any(_under(request["destination"], src) for src in sources):
            raise TaskValidationError({"destination": "A namespace cannot move or merge into itself or its descendants."})
    if action == "promote":
        disposition = body.get("parent_assignment")
        if disposition is not None and (not isinstance(disposition, str) or disposition not in {"keep", "move"}):
            raise TaskValidationError({"parent_assignment": "Choose how to handle tasks assigned directly to the parent."})
        request["parent_assignment"] = disposition
        if disposition == "move":
            request["parent_destination"] = _path(body.get("parent_destination"), "parent_destination")
    return request


class TaskNamespaceService:
    """Companion to TaskApplicationService for collection-wide namespace writes."""

    def __init__(self, service: TaskApplicationService) -> None:
        self.service = service
        self.store = service.store

    @staticmethod
    def _namespace_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT g.task_id,g.tag,t.state,t.archived_at,t.deleted_at "
            "FROM task_tags g JOIN task_metadata t ON t.task_id=g.task_id "
            "WHERE g.is_namespace=1 ORDER BY g.tag,g.task_id"
        ).fetchall()

    @staticmethod
    def _inventory_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        branches: dict[str, set[str]] = defaultdict(set)
        direct: dict[str, set[str]] = defaultdict(set)
        statuses: dict[str, str] = {}
        for row in rows:
            path, task_id = str(row["tag"]), str(row["task_id"])
            statuses[task_id] = _status(row)
            direct[path].add(task_id)
            parts = path.split("/")
            for i in range(len(parts)):
                branches["/".join(parts[: i + 1])].add(task_id)
        result = []
        for path, task_ids in sorted(branches.items()):
            status_counts = _counts()
            for task_id in task_ids:
                status_counts[statuses[task_id]] += 1
            result.append({"path": path, "parent": path.rpartition("/")[0] or None, "label": path.rsplit("/", 1)[-1], "count": len(task_ids), "direct_count": len(direct[path]), "status_counts": status_counts})
        return result

    def inventory(self) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            conn.execute("BEGIN")
            namespaces = self._inventory_rows(self._namespace_rows(conn))
            operations = [self._operation_summary(conn, row) for row in conn.execute("SELECT * FROM task_namespace_operations ORDER BY created_at DESC,operation_id DESC LIMIT 30")]
            return {"collection_revision": self.store.collection_revision_in_connection(conn), "namespaces": namespaces, "operations": operations}
        finally:
            conn.close()

    def _plan(self, conn: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any]:
        rows = self._namespace_rows(conn)
        inventory = self._inventory_rows(rows)
        universe = {item["path"] for item in inventory}
        direct_ids: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            direct_ids[str(row["tag"])].add(str(row["task_id"]))
        action = request["action"]
        issues: list[str] = []
        mapping: dict[str, str | None] = {}
        sources = request.get("sources", [])
        for source in sources:
            if source not in universe:
                issues.append(f"Namespace '{source}' no longer exists.")
        if action == "merge" and request["destination"] not in universe:
            issues.append("Choose an existing destination namespace for Merge.")
        for source in sources:
            # Include implicit grouping paths so collisions at an existing
            # destination branch are reviewed even when its suffixes differ.
            for name in sorted(universe):
                if name != source and not (request["include_descendants"] and name.startswith(source + "/")):
                    continue
                # Native tags may contain repeated separators. Renaming a
                # branch must preserve its descendants' exact spelling.
                suffix = name[len(source):]
                if action == "rename":
                    target = _join(source.rpartition("/")[0], request["name"])
                    mapping[name] = target + suffix
                elif action == "move":
                    target = _join(request["destination"], source.rsplit("/", 1)[-1])
                    mapping[name] = target + suffix
                elif action == "merge":
                    mapping[name] = request["destination"] + suffix
                elif action == "remove":
                    mapping[name] = None
                elif action == "promote":
                    if suffix:
                        mapping[name] = _join(source.rpartition("/")[0], suffix[1:])
                    elif request["parent_assignment"] == "move":
                        mapping[name] = request["parent_destination"]
                    elif request["parent_assignment"] is None and direct_ids[source]:
                        issues.append(f"Choose what happens to the {len(direct_ids[source])} tasks assigned directly to '{source}'.")
        mapping = {a: b for a, b in mapping.items() if a != b}
        for target in mapping.values():
            if target is not None:
                _path(target, "destination")
        incoming: dict[str, list[str]] = defaultdict(list)
        for before, after in mapping.items():
            if after is not None:
                incoming[after].append(before)
        collisions = []
        for before, after in mapping.items():
            if after is not None and ((after in universe and after not in mapping) or len(incoming[after]) > 1):
                collisions.append({"from": before, "to": after})
        if collisions and action != "merge" and not request.get("merge_collisions", False):
            issues.append("Some destinations already exist. Choose another destination or allow merging namespace assignments.")
        affected_ids = set(request.get("task_ids", [])) if action == "assign" else {task_id for name in mapping for task_id in direct_ids[name]}
        task_rows: dict[str, sqlite3.Row] = {}
        ids = sorted(affected_ids)
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            placeholders = ",".join("?" for _ in chunk)
            task_rows.update({str(row["task_id"]): row for row in conn.execute(f"SELECT * FROM task_metadata WHERE task_id IN ({placeholders})", chunk)})
        if affected_ids - task_rows.keys():
            issues.append("One or more selected tasks no longer exist. Refresh the selection.")
        tags = self.store._tags_for_ids(conn, ids)
        changes = []
        status_counts = _counts()
        assignment_count = 0
        assignments_added = assignments_removed = duplicate_assignments_removed = 0
        unnamespaced_count = 0
        for task_id, row in sorted(task_rows.items()):
            original = {tag.name: tag.is_namespace for tag in tags.get(task_id, ())}
            after = dict(original)
            before_names = sorted(name for name, namespace in original.items() if namespace)
            if action == "assign":
                desired = set(request["namespaces"])
                mode = request["assignment_mode"]
                if mode in {"remove", "replace"}:
                    for name in before_names:
                        if mode == "replace" or name in desired:
                            after.pop(name, None)
                if mode in {"add", "replace"}:
                    after.update({name: True for name in desired})
            else:
                for name in before_names:
                    if name in mapping:
                        after.pop(name, None)
                for name in before_names:
                    if name in mapping and mapping[name] is not None:
                        after[str(mapping[name])] = True
            if after == original:
                continue
            for name, is_namespace in original.items():
                if not is_namespace and after.get(name) is True:
                    issues.append(f"Task '{row['description'] or task_id}' already has an ordinary tag '{name}'. Rename or remove that tag, or choose a different namespace destination.")
            after_names = sorted(name for name, namespace in after.items() if namespace)
            status_counts[_status(row)] += 1
            removed = set(before_names) - set(after_names)
            added = set(after_names) - set(before_names)
            assignments_removed += len(removed)
            assignments_added += len(added)
            if action == "assign":
                assignment_count += len(removed) + len(added)
            else:
                changed_names = [name for name in before_names if name in mapping]
                assignment_count += len(changed_names)
                incoming_names = [mapping[name] for name in changed_names if mapping[name] is not None]
                retained = set(before_names) - set(changed_names)
                duplicate_assignments_removed += len(incoming_names) - len(set(incoming_names) - retained)
            unnamespaced_count += int(bool(before_names) and not after_names)
            changes.append({"task_id": task_id, "title": row["description"] or "Untitled task", "revision": int(row["revision"]), "before": before_names, "after": after_names, "before_tags": sorted(original.items()), "after_tags": sorted(after.items())})
        if not changes and not issues:
            issues.append("This operation would not change any namespace assignments.")
        collision_keys = {(item["from"], item["to"]) for item in collisions}
        preview = {
            "request": request, "action": action,
            "collection_revision": self.store.collection_revision_in_connection(conn),
            "mapping": [{"from": before, "to": after, "task_count": len(direct_ids[before]), "collision": (before, after) in collision_keys} for before, after in sorted(mapping.items())],
            "task_count": len(changes), "assignment_count": assignment_count,
            "assignments_added": assignments_added, "assignments_removed": assignments_removed,
            "duplicate_assignments_removed": duplicate_assignments_removed,
            "status_counts": status_counts, "unnamespaced_count": unnamespaced_count,
            "collisions": collisions, "can_apply": bool(changes) and not issues,
            "issues": sorted(set(issues)), "scope": "selected_tasks" if action == "assign" else "all_statuses",
        }
        preview["fingerprint"] = _fingerprint({"request": request, "changes": changes, "mapping": preview["mapping"], "issues": preview["issues"]})
        preview["changes"] = changes
        return preview

    def preview(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = _canonical_request(body)
        offset, limit = body.get("tasks_offset", 0), body.get("tasks_limit", 50)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise TaskValidationError({"tasks_offset": "Choose a valid preview page."})
        conn = self.store.connect()
        try:
            conn.execute("BEGIN")
            result = self._plan(conn, request)
            changes = result.pop("changes")
            result.update(tasks=[{k: v for k, v in change.items() if k not in {"before_tags", "after_tags"}} for change in changes[offset:offset + limit]], tasks_offset=offset, tasks_limit=limit, tasks_has_more=offset + limit < len(changes))
            return result
        finally:
            conn.close()

    def _operation_summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        changes = json.loads(row["changes_json"])
        can_undo = row["undone_at"] is None
        if can_undo:
            for start in range(0, len(changes), 800):
                chunk = changes[start:start + 800]
                expected = {item["task_id"]: item["applied_revision"] for item in chunk}
                placeholders = ",".join("?" for _ in expected)
                current = {str(item[0]): int(item[1]) for item in conn.execute(f"SELECT task_id,revision FROM task_metadata WHERE task_id IN ({placeholders})", list(expected))}
                if current != expected:
                    can_undo = False
                    break
        return {"operation_id": row["operation_id"], "action": row["action"], "label": row["label"], "created_at": row["created_at"], "undone_at": row["undone_at"], "can_undo": can_undo, "undo_unavailable_reason": None if can_undo else "Already undone." if row["undone_at"] else "An affected task changed after this operation."}

    def _replay(self, conn: sqlite3.Connection, client_id: str, actor: str, request_hash: str) -> dict[str, Any] | None:
        existing = conn.execute("SELECT * FROM task_mutation_receipts WHERE client_mutation_id=?", (client_id,)).fetchone()
        if existing is None:
            return None
        if existing["request_hash"] != request_hash or existing["actor"] != actor:
            raise TaskIdempotencyConflict(client_id)
        if existing["status"] != "completed" or not existing["result_json"]:
            raise TaskDomainError("This operation has no completed receipt yet. Retry the same request.")
        result = json.loads(existing["result_json"])
        result["replayed"] = True
        return result

    def _receipt(self, conn: sqlite3.Connection, *, client_id: str, actor: str, session_id: str | None, mutation: str, request_hash: str, now: str) -> str:
        receipt_id = self.service._receipt_id_factory()
        conn.execute("INSERT INTO task_mutation_receipts(receipt_id,client_mutation_id,actor,session_id,mutation,request_hash,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)", (receipt_id, client_id, actor, session_id, mutation, request_hash, now))
        return receipt_id

    def _write_changes(self, conn: sqlite3.Connection, changes: list[dict[str, Any]], *, undo: bool, receipt_id: str, actor: str, session_id: str | None, now: str) -> int:
        collection_revision = self.store.collection_revision_in_connection(conn)
        for change in changes:
            task_id = change["task_id"]
            expected = change["applied_revision"] if undo else change["revision"]
            desired = change["before_tags"] if undo else change["after_tags"]
            self.service._update_task_cas(conn, task_id, expected, now, actor)
            conn.execute("DELETE FROM task_tags WHERE task_id=?", (task_id,))
            conn.executemany("INSERT INTO task_tags(task_id,tag,is_namespace) VALUES(?,?,?)", [(task_id, name, int(namespace)) for name, namespace in desired])
            task = Task.from_row(conn.execute("SELECT * FROM task_metadata WHERE task_id=?", (task_id,)).fetchone(), tags=tuple(Tag(name, bool(ns)) for name, ns in desired))
            collection_revision = self.service._next_collection_revision(conn, now)
            mutation = "namespaces.undo" if undo else "namespaces.organize"
            change_record = _Change(task_id, task.state, task.state, True, {"before": change["after"] if undo else change["before"], "after": change["before"] if undo else change["after"]}, "namespace organization undone" if undo else "namespaces organized")
            self.service._append_history(conn, task=task, change=change_record, mutation=mutation, actor=actor, session_id=session_id, receipt_id=receipt_id, collection_revision=collection_revision, now=now)
            self.service._append_outbox(conn, task=task, mutation=mutation, collection_revision=collection_revision, now=now)
            if not undo:
                change["applied_revision"] = task.revision
        return collection_revision

    def apply(self, body: Mapping[str, Any], *, actor: str, session_id: str | None = None) -> dict[str, Any]:
        raw_request = body.get("request")
        if not isinstance(raw_request, Mapping):
            raise TaskValidationError({"request": "Review a namespace change before applying it."})
        request = _canonical_request(raw_request)
        client_id, fingerprint = body.get("client_mutation_id"), body.get("expected_fingerprint")
        self.service._validate_authority(client_id, actor)
        if not isinstance(fingerprint, str) or not fingerprint:
            raise TaskValidationError({"expected_fingerprint": "Review a namespace change before applying it."})
        request_hash = self.service._request_hash("namespaces.apply", {"request": request, "expected_fingerprint": fingerprint})
        now = self.service._now()
        with self.store.transaction() as conn:
            self.service._assert_native_mutation_authority(conn)
            if replay := self._replay(conn, client_id, actor, request_hash):
                return replay
            plan = self._plan(conn, request)
            if plan["fingerprint"] != fingerprint:
                raise NamespaceConflict("Tasks or namespaces changed since this preview. Refresh the preview before applying.")
            if not plan["can_apply"]:
                raise TaskValidationError({"preview": " ".join(plan["issues"])})
            receipt_id = self._receipt(conn, client_id=client_id, actor=actor, session_id=session_id, mutation="namespaces.apply", request_hash=request_hash, now=now)
            revision = self._write_changes(conn, plan["changes"], undo=False, receipt_id=receipt_id, actor=actor, session_id=session_id, now=now)
            operation_id = "tno_" + uuid.uuid4().hex
            label = f"{request['action'].capitalize()} namespaces on {plan['task_count']} tasks"
            conn.execute("INSERT INTO task_namespace_operations(operation_id,receipt_id,action,label,request_json,changes_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?)", (operation_id, receipt_id, request["action"], label, _json(request), _json(plan["changes"]), actor, now))
            row = conn.execute("SELECT * FROM task_namespace_operations WHERE operation_id=?", (operation_id,)).fetchone()
            result = {"operation": self._operation_summary(conn, row), "collection_revision": revision, "task_count": plan["task_count"], "receipt_id": receipt_id, "replayed": False}
            conn.execute("UPDATE task_mutation_receipts SET status='completed',result_json=?,completed_at=? WHERE receipt_id=?", (_json(result), now, receipt_id))
            return result

    def undo(self, operation_id: str, body: Mapping[str, Any], *, actor: str, session_id: str | None = None) -> dict[str, Any]:
        client_id = body.get("client_mutation_id")
        self.service._validate_authority(client_id, actor)
        request_hash = self.service._request_hash("namespaces.undo", {"operation_id": operation_id})
        now = self.service._now()
        with self.store.transaction() as conn:
            self.service._assert_native_mutation_authority(conn)
            if replay := self._replay(conn, client_id, actor, request_hash):
                return replay
            row = conn.execute("SELECT * FROM task_namespace_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise TaskValidationError({"operation_id": "This namespace operation was not found."})
            summary = self._operation_summary(conn, row)
            if not summary["can_undo"]:
                raise NamespaceConflict(summary["undo_unavailable_reason"] + " Refresh the organizer to review the current state.")
            receipt_id = self._receipt(conn, client_id=client_id, actor=actor, session_id=session_id, mutation="namespaces.undo", request_hash=request_hash, now=now)
            changes = json.loads(row["changes_json"])
            revision = self._write_changes(conn, changes, undo=True, receipt_id=receipt_id, actor=actor, session_id=session_id, now=now)
            conn.execute("UPDATE task_namespace_operations SET undone_at=?,undo_receipt_id=? WHERE operation_id=?", (now, receipt_id, operation_id))
            updated = conn.execute("SELECT * FROM task_namespace_operations WHERE operation_id=?", (operation_id,)).fetchone()
            result = {"operation": self._operation_summary(conn, updated), "collection_revision": revision, "task_count": len(changes), "receipt_id": receipt_id, "replayed": False}
            conn.execute("UPDATE task_mutation_receipts SET status='completed',result_json=?,completed_at=? WHERE receipt_id=?", (_json(result), now, receipt_id))
            return result
