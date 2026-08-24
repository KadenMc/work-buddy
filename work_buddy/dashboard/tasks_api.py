"""Typed HTTP boundary for the SQLite-authoritative React Tasks app."""

from __future__ import annotations

import hmac
import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from flask import Blueprint, jsonify, request

from work_buddy.dashboard import local_identity_api
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.tasks.documents import TaskDocumentService
from work_buddy.tasks.errors import (
    TaskAuthorityUnavailable,
    TaskDomainError,
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskMutationFenced,
    TaskRevisionConflict,
    TaskValidationError,
)
from work_buddy.tasks.models import Task, TaskActionItem, TaskDocumentLink, TaskQuery
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore
from work_buddy.truth.identity import canonical_json, sha256_text


TaskStoreFactory = Callable[[], TaskStore]
TaskDocumentFactory = Callable[[], TaskDocumentService]
TaskAuthorizer = Callable[[str, str, str, str, Mapping[str, Any]], str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _encoded(value: str) -> str:
    return quote(value, safe="-_.!~*'()")


def _default_authorizer(
    operation: str,
    subject: str,
    method: str,
    path: str,
    body: Mapping[str, Any],
) -> str:
    authority = local_identity_api.require_human_authority_request(
        action=f"dashboard.tasks.{operation}",
        subject=subject,
        context_sha256=sha256_text(
            canonical_json({"method": method, "path": path, "body": dict(body)})
        ),
    )
    return authority.principal.actor.canonical_id


def _json_body() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        raise TaskValidationError({"body": "The request body must be a JSON object."})
    return dict(value)


def _required_text(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError({key: "This field is required."})
    return value.strip()


def _client_mutation_id(body: Mapping[str, Any]) -> str:
    return _required_text(body, "client_mutation_id")


def _expected_revision(body: Mapping[str, Any]) -> int:
    value = body.get("expected_revision")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TaskValidationError(
            {"expected_revision": "A positive task revision is required."}
        )
    return value


def _authority_active(store: TaskStore) -> bool:
    from work_buddy.tasks.runtime import mutation_fence_active, native_authority_active

    # Never let SQLite attest to its own authority.  The runtime cross-checks
    # the external activation latch and raises when that proof is absent or
    # malformed, including when this store path was supplied explicitly.
    return native_authority_active(store.path) and not mutation_fence_active(store.path)


def _attention_state(task: Task) -> str:
    return task.state


def _current_action(task: Task) -> str | None:
    for item in task.action_items:
        if item.id == task.current_action_item_id and item.deleted_at is None:
            return item.description
    return None


def _task_summary(task: Task, document: TaskDocumentLink | None) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.description,
        "revision": task.revision,
        "attention_state": _attention_state(task),
        "urgency": task.urgency,
        "due_date": task.due_date,
        "deadline_date": task.deadline_date,
        "snooze_until": task.snooze_until,
        "project": task.project,
        "namespaces": list(task.namespace_tags),
        "tags": [tag.name for tag in task.tags],
        "current_action": _current_action(task),
        "has_document": document is not None and document.lifecycle not in {"retired", "deleted"},
        "completed_at": task.completed_at,
        "archived_at": task.archived_at,
        "deleted_at": task.deleted_at,
        "updated_at": task.updated_at,
    }


def _dependencies(task: Task) -> list[str]:
    native = getattr(task, "dependencies", None)
    if isinstance(native, tuple):
        return [str(item) for item in native]
    hint = task.dependency_hint
    if not hint:
        return []
    try:
        parsed = json.loads(hint)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [hint]


def _action_items(
    task: Task,
    *,
    deleted: tuple[TaskActionItem, ...] = (),
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in (*task.action_items, *deleted):
        if item.authorship == "agent_unapproved":
            approval = "pending"
        elif item.authorship == "agent_approved":
            approval = "approved"
        else:
            approval = "not_required"
        result.append(
            {
                "action_item_id": str(item.id),
                "text": item.description,
                "position": item.sequence,
                "completed": item.state == "done",
                "current": item.id == task.current_action_item_id,
                "approval_state": approval,
                "deleted_at": item.deleted_at,
            }
        )
    return result


def _document_summary(link: TaskDocumentLink | None) -> dict[str, Any]:
    if link is None:
        return {
            "state": "missing",
            "store_id": None,
            "document_id": None,
            "excerpt": None,
            "updated_at": None,
            "updated_by": None,
            "href": None,
        }
    state = "available" if link.lifecycle not in {"retired", "deleted"} else "unavailable"
    excerpt: str | None = None
    updated_at = link.updated_at
    updated_by: str | None = None
    try:
        from work_buddy.tasks.documents import (
            TaskDocumentStoreManager,
            project_live_markdown,
        )

        cowork_store = TaskDocumentStoreManager().open_existing()
        if cowork_store.store_id == link.store_id:
            content = project_live_markdown(cowork_store, link.document_id)
            compact = " ".join(
                line.lstrip("#>-* ").strip()
                for line in content.splitlines()
                if line.strip()
            )
            excerpt = compact[:280] or None
            conn = cowork_store.connect()
            try:
                events = cowork_store._document_events_locked(conn, link.document_id)
            finally:
                conn.close()
            if events:
                latest = events[-1]
                updated_at = latest.at
                updated_by = latest.actor_ref or latest.actor_kind
    except Exception:
        # Link identity remains useful if the local task-document store is
        # temporarily unavailable; never turn a list read into a hard error.
        pass
    return {
        "state": state,
        "store_id": link.store_id,
        "document_id": link.document_id,
        "excerpt": excerpt,
        "updated_at": updated_at,
        "updated_by": updated_by,
        "href": (
            f"/app/cowork?store_id={_encoded(link.store_id)}"
            f"&document_id={_encoded(link.document_id)}"
        ),
    }


def _local_files(
    link: TaskDocumentLink | None,
) -> tuple[list[dict[str, Any]], str | None]:
    if link is None:
        return [], None
    try:
        from work_buddy.cowork.folder_api import _is_direct_loopback_request
        from work_buddy.cowork.local_files import LocalFileLinkRegistry

        registry = LocalFileLinkRegistry.default()
        local = _is_direct_loopback_request()
        result: list[dict[str, Any]] = []
        for item in registry.list_document_links(
            store_id=link.store_id,
            document_id=link.document_id,
        ):
            status = registry.inspect(item)
            availability = {
                "verified": "available",
                "changed": "changed",
            }.get(status.availability, "unavailable")
            result.append(
                {
                    "link_id": item.link_id,
                    "display_name": item.display_name,
                    "media_type": item.media_type,
                    "byte_length": item.byte_length,
                    "sensitivity": item.sensitivity,
                    "allowed_action": item.allowed_action,
                    "availability": availability,
                    "host_action_available": local and availability == "available",
                    "unavailable_reason": (
                        None
                        if availability == "available"
                        else "The linked file is unavailable or changed on this computer."
                    ),
                }
            )
        return result, None
    except Exception:
        return (
            [],
            "Linked-file metadata is unavailable. Recheck before opening a local file.",
        )


def _task_detail(store: TaskStore, task: Task) -> dict[str, Any]:
    document = store.get_task_document_link(task.task_id)
    local_files, local_files_error = _local_files(document)
    summary = _task_summary(task, document)
    conn = store.connect()
    try:
        deleted_actions = tuple(
            TaskActionItem.from_row(row)
            for row in conn.execute(
                "SELECT * FROM task_action_items WHERE task_id = ? "
                "AND deleted_at IS NOT NULL ORDER BY deleted_at DESC, id",
                (task.task_id,),
            ).fetchall()
        )
    finally:
        conn.close()
    history = [
        {
            "history_id": str(item.id),
            "occurred_at": item.changed_at,
            "actor": item.actor or "Work Buddy",
            "action": item.mutation or "state_change",
            "summary": item.reason or item.mutation or "Task updated",
        }
        for item in store.history(task.task_id)
    ]
    contexts = tuple(
        dict.fromkeys((*task.agent_required_contexts, *task.user_required_contexts))
    )
    automation = task.automation_tier_achievable
    return {
        **summary,
        "summary": str(getattr(task, "summary_text", None) or ""),
        "desired_outcome": task.outcome_text or "",
        "next_action": task.next_action_text or "",
        "definition_of_done": task.definition_of_done or "",
        "dependencies": _dependencies(task),
        "contract": task.contract,
        "required_contexts": list(contexts),
        "automation_tier": None if automation is None else str(automation),
        "provenance": {
            "created_by": task.created_by_session or "Unknown",
            "created_at": task.created_at,
            "source": task.creation_provenance,
        },
        "action_items": _action_items(task, deleted=deleted_actions),
        "history": history,
        "document": _document_summary(document),
        "local_files": local_files,
        "local_files_error": local_files_error,
    }


def _document_links(store: TaskStore, tasks: list[Task]) -> dict[str, TaskDocumentLink]:
    if not tasks:
        return {}
    conn = store.connect()
    try:
        ids = [task.task_id for task in tasks]
        result: dict[str, TaskDocumentLink] = {}
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT * FROM task_document_links WHERE task_id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                link = TaskDocumentLink(**dict(row))
                result[link.task_id] = link
        return result
    finally:
        conn.close()


def _lens_matches(task: Task, lens: str) -> bool:
    if lens == "trash":
        return task.deleted_at is not None
    if task.deleted_at is not None:
        return False
    if lens == "completed":
        return task.state == "done" or task.archived_at is not None
    if task.archived_at is not None or task.state == "done":
        return False
    if lens == "focused":
        return task.state == "focused"
    if lens in {"inbox", "triage"}:
        return task.state == "inbox"
    if lens == "snoozed":
        return task.state == "snoozed"
    return task.state != "snoozed"


def _filters_match(
    task: Task,
    document: TaskDocumentLink | None,
    query: Mapping[str, str | None],
) -> bool:
    q = (query.get("q") or "").casefold()
    if q and q not in task.description.casefold() and all(
        q not in tag.name.casefold() for tag in task.tags
    ):
        return False
    project = query.get("project") or ""
    if project and (task.project or "").casefold() != project.casefold():
        return False
    namespace = (query.get("namespace") or "").strip("#/").casefold()
    if namespace and not any(
        tag.casefold() == namespace or tag.casefold().startswith(namespace + "/")
        for tag in task.namespace_tags
    ):
        return False
    urgency = query.get("urgency") or ""
    if urgency and task.urgency != ("high" if urgency == "critical" else urgency):
        return False
    state = query.get("state") or ""
    if state and _attention_state(task) != state:
        return False
    note = query.get("note") or ""
    if note == "yes" and document is None:
        return False
    if note == "no" and document is not None:
        return False
    due = query.get("due") or ""
    today = date.today().isoformat()
    if due == "today" and task.due_date != today:
        return False
    if due == "overdue" and (task.due_date is None or task.due_date >= today):
        return False
    if due == "none" and task.due_date is not None:
        return False
    if due == "week":
        week_end = (date.today() + timedelta(days=7)).isoformat()
        if task.due_date is None or not (today <= task.due_date <= week_end):
            return False
    return True


def _facets(tasks: list[Task]) -> dict[str, Any]:
    counts = {
        lens: sum(1 for task in tasks if _lens_matches(task, lens))
        for lens in (
            "focused",
            "inbox",
            "active",
            "snoozed",
            "completed",
            "trash",
            "triage",
        )
    }
    projects = Counter(task.project for task in tasks if task.project)
    namespaces = Counter(tag for task in tasks for tag in task.namespace_tags)
    urgencies = Counter(task.urgency for task in tasks)
    return {
        "counts": counts,
        "projects": dict(sorted(projects.items())),
        "namespaces": dict(sorted(namespaces.items())),
        "urgencies": dict(sorted(urgencies.items())),
    }


def _options(tasks: list[Task]) -> dict[str, Any]:
    projects = sorted({task.project for task in tasks if task.project})
    namespaces = sorted({tag for task in tasks for tag in task.namespace_tags})
    contracts = sorted({task.contract for task in tasks if task.contract})
    contexts = sorted(
        {
            value
            for task in tasks
            for value in (*task.agent_required_contexts, *task.user_required_contexts)
        }
    )
    option = lambda value: {"value": value, "label": value}
    return {
        "projects": [option(value) for value in projects],
        "namespaces": [option(value) for value in namespaces],
        "contracts": [option(value) for value in contracts],
        "contexts": [option(value) for value in contexts],
    }


def _task_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    mapping = {
        "title": "description",
        "summary": "summary_text",
        "desired_outcome": "outcome_text",
        "next_action": "next_action_text",
        "definition_of_done": "definition_of_done",
        "contract": "contract",
        "due_date": "due_date",
        "deadline_date": "deadline_date",
    }
    result = {target: body[source] for source, target in mapping.items() if source in body}
    if "urgency" in body:
        result["urgency"] = "high" if body["urgency"] == "critical" else body["urgency"]
    if "dependencies" in body:
        dependencies = body["dependencies"]
        result["dependencies"] = dependencies
        result["has_dependency"] = bool(dependencies)
    if "required_contexts" in body:
        result["user_required_contexts"] = body["required_contexts"]
        result["required_contexts_source"] = "user_authored"
    if "automation_tier" in body:
        raw = body["automation_tier"]
        if raw is None or raw == "":
            result["automation_tier_achievable"] = None
        else:
            try:
                result["automation_tier_achievable"] = int(raw)
            except (TypeError, ValueError) as exc:
                raise TaskValidationError(
                    {"automation_tier": "Automation tier must be an integer or empty."}
                ) from exc
    return result


def _tag_set(body: Mapping[str, Any], current: Task | None = None) -> list[tuple[str, bool]]:
    ordinary: list[str] = []
    if current is not None:
        ordinary = [tag.name for tag in current.tags if not tag.is_namespace]
    if isinstance(body.get("tags"), list):
        ordinary = [str(value).strip(" #") for value in body["tags"] if str(value).strip(" #")]
    namespaces = (
        [str(value).strip(" #/") for value in body.get("namespaces", [])]
        if isinstance(body.get("namespaces"), list)
        else list(current.namespace_tags if current is not None else ())
    )
    project = body.get("project")
    if "project" not in body and current is not None:
        project = current.project
    namespaces = [value for value in namespaces if value and not value.casefold().startswith("projects/")]
    if isinstance(project, str) and project.strip():
        namespaces.append(f"projects/{project.strip().strip('#/')}" )
    return [*( (value, False) for value in ordinary), *((value, True) for value in namespaces)]


def _batch_items(body: Mapping[str, Any]) -> list[Any]:
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise TaskValidationError({"items": "A batch must contain 1 to 100 tasks."})
    return items


def _batch_service_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(raw),
        "description": raw.get("title"),
        "state": str(raw.get("attention_state") or raw.get("state") or "inbox"),
        "urgency": (
            "high"
            if raw.get("urgency") == "critical"
            else str(raw.get("urgency") or "medium")
        ),
        "creation_provenance": "dashboard_batch",
        **(
            {"user_required_contexts": raw.get("required_contexts") or ()}
            if "required_contexts" in raw
            else {}
        ),
    }


def _batch_title_key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _batch_preview(
    store: TaskStore,
    service: TaskApplicationService,
    body: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = _batch_items(body)
    batch_id = _client_mutation_id(body)
    existing_titles: set[str] = set()
    offset = 0
    while True:
        page = service.list(
            TaskQuery(
                include_done=True,
                include_archived=True,
                include_deleted=False,
                include_snoozed=True,
                limit=5000,
                offset=offset,
            )
        )
        existing_titles.update(
            key
            for task in page
            if (key := _batch_title_key(task.description))
        )
        if len(page) < 5000:
            break
        offset += len(page)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    accepted_indices: list[int] = []
    for index, candidate in enumerate(items):
        raw = candidate if isinstance(candidate, Mapping) else None
        title = (
            str(raw.get("title") or "").strip()
            if raw is not None
            else ""
        )
        title_key = _batch_title_key(title)
        duplicate_reason: str | None = None
        if title_key:
            if title_key in existing_titles:
                duplicate_reason = "existing_title"
            elif title_key in seen:
                duplicate_reason = "batch"
            seen.add(title_key)

        row_errors: dict[str, str] = {}
        prepared: dict[str, Any] | None = None
        if raw is None:
            row_errors["item"] = "Each item must be an object."
        else:
            prepared = _batch_service_item(raw)
            try:
                # Use the application service's complete create validator so
                # preview and commit cannot drift on dates, states, tags, or
                # structured authoring fields.
                service._prepare_batch_item(prepared, index, batch_id)
            except TaskValidationError as exc:
                prefix = f"items.{index}."
                exact = f"items.{index}"
                for key, message in exc.field_errors.items():
                    local_key = (
                        "item"
                        if key == exact
                        else key[len(prefix) :]
                        if key.startswith(prefix)
                        else key
                    )
                    row_errors[local_key] = message

        valid = not row_errors
        will_create = valid and duplicate_reason is None and prepared is not None
        if will_create:
            accepted.append(prepared)
            accepted_indices.append(index)
        rows.append(
            {
                "index": index,
                "title": title,
                "valid": valid,
                "field_errors": row_errors,
                "duplicate": duplicate_reason is not None,
                "duplicate_reason": duplicate_reason,
                "will_create": will_create,
            }
        )

    collection_revision = store.collection_revision()
    token_value = {
        "schema": "wb.tasks.batch-preview/v1",
        "client_mutation_id": batch_id,
        "collection_revision": collection_revision,
        "items": items,
        "accepted_indices": accepted_indices,
        "rows": rows,
    }
    preview = {
        "rows": rows,
        "accepted_indices": accepted_indices,
        "accepted_count": len(accepted),
        "can_commit": bool(accepted),
        "collection_revision": collection_revision,
        "preview_token": sha256_text(canonical_json(token_value)),
    }
    return preview, accepted


def _completed_batch_receipt(store: TaskStore, client_mutation_id: str) -> bool:
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT mutation, status FROM task_mutation_receipts "
            "WHERE client_mutation_id = ?",
            (client_mutation_id,),
        ).fetchone()
    finally:
        conn.close()
    return bool(
        row is not None
        and row["mutation"] == "batch.create"
        and row["status"] == "completed"
    )


def _submitted_batch_indices(
    body: Mapping[str, Any],
    *,
    item_count: int,
) -> list[int]:
    indices = body.get("accepted_indices")
    if (
        not isinstance(indices, list)
        or not indices
        or any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= item_count
            for index in indices
        )
        or len(set(indices)) != len(indices)
    ):
        raise TaskValidationError(
            {"accepted_indices": "Commit the exact rows returned by batch preview."}
        )
    return indices


def _replay_batch_items(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = _batch_items(body)
    indices = _submitted_batch_indices(body, item_count=len(items))
    result: list[dict[str, Any]] = []
    for index in indices:
        raw = items[index]
        if not isinstance(raw, Mapping):
            raise TaskValidationError(
                {f"items.{index}": "Each committed item must be an object."}
            )
        result.append(_batch_service_item(raw))
    return result


def _mutation_envelope(store: TaskStore, result) -> dict[str, Any]:
    from work_buddy.tasks.events import publish_pending
    from work_buddy.dashboard.events import publish

    def deliver(event_type: str, payload: dict[str, Any]) -> bool:
        publish(event_type, payload)
        return True

    publish_pending(store, delivery=deliver)
    return {
        "ok": True,
        "result": {
            "task": _task_detail(store, result.task),
            "collection_revision": result.collection_revision,
            "receipt": result.receipt.to_dict(),
            "changed": result.changed,
            "replayed": result.replayed,
        },
    }


def _error_response(exc: Exception):
    if isinstance(exc, LocalIdentityError):
        return local_identity_api._error(exc)
    if isinstance(exc, TaskDomainError):
        status = 422
        if isinstance(exc, TaskNotFound):
            status = 404
        elif isinstance(exc, (TaskRevisionConflict, TaskIdempotencyConflict)):
            status = 409
        elif isinstance(exc, (TaskMutationFenced, TaskAuthorityUnavailable)):
            status = 503
        elif exc.code in {"task_invalid_transition", "task_deleted"}:
            status = 409
        return jsonify({"ok": False, "error": exc.to_dict()}), status
    return jsonify(
        {
            "ok": False,
            "error": {
                "code": "task_internal_error",
                "message": "The task operation could not be completed.",
                "retryable": False,
            },
        }
    ), 500


def create_tasks_blueprint(
    *,
    store_factory: TaskStoreFactory = TaskStore,
    document_factory: TaskDocumentFactory = TaskDocumentService,
    authorizer: TaskAuthorizer = _default_authorizer,
    dashboard_read_only: Callable[[], bool] | None = None,
    local_registry_factory: Callable[[], Any] | None = None,
    local_os_actions: Any | None = None,
) -> Blueprint:
    """Build the Tasks API with injectable security and host-action seams."""

    blueprint = Blueprint(f"tasks_native_{uuid.uuid4().hex}", __name__)

    def stack() -> tuple[TaskStore, TaskApplicationService]:
        store = store_factory()
        return store, TaskApplicationService(store)

    def access(store: TaskStore) -> dict[str, str]:
        authority_active = _authority_active(store)
        read_only = bool(dashboard_read_only and dashboard_read_only())
        if read_only:
            return {"mode": "read_only", "reason": "The dashboard is read-only."}
        if not authority_active:
            return {
                "mode": "read_only",
                "reason": "Task editing is temporarily unavailable while setup finishes.",
            }
        return {"mode": "read_write"}

    def authorize(
        store: TaskStore,
        *,
        operation: str,
        subject: str,
        path: str,
        body: Mapping[str, Any],
    ) -> str:
        from work_buddy.tasks.runtime import native_task_mutation_authority

        if not native_task_mutation_authority(store.path):
            raise TaskDomainError(
                "Task editing is temporarily unavailable while setup finishes."
            )
        if dashboard_read_only and dashboard_read_only():
            raise TaskDomainError("The dashboard is read-only.")
        return authorizer(operation, subject, request.method, path, body)

    @blueprint.get("/api/tasks/view")
    def view():
        try:
            store, service = stack()
            all_tasks = service.list(
                TaskQuery(
                    include_done=True,
                    include_archived=True,
                    include_deleted=True,
                    include_snoozed=True,
                    limit=5000,
                )
            )
            links = _document_links(store, all_tasks)
            lens = request.args.get("lens", "inbox")
            if lens not in {
                "focused", "inbox", "active", "snoozed", "completed", "trash", "triage"
            }:
                lens = "inbox"
            query = {
                "lens": lens,
                "q": request.args.get("q", ""),
                "project": request.args.get("project", ""),
                "namespace": request.args.get("namespace", ""),
                "urgency": request.args.get("urgency", ""),
                "due": request.args.get("due", ""),
                "state": request.args.get("state", ""),
                "note": request.args.get("note", ""),
                "task": request.args.get("task"),
            }
            visible = [
                task
                for task in all_tasks
                if _lens_matches(task, lens)
                and _filters_match(task, links.get(task.task_id), query)
            ]
            selected = None
            selected_id = query["task"]
            if selected_id:
                candidate = service.get(str(selected_id), include_deleted=True)
                if candidate is not None:
                    selected = _task_detail(store, candidate)
            return jsonify(
                {
                    "ok": True,
                    "collection_revision": service.store.collection_revision(),
                    "observed_at": _now(),
                    "access": access(store),
                    "query": query,
                    "facets": _facets(all_tasks),
                    "tasks": [
                        _task_summary(task, links.get(task.task_id)) for task in visible
                    ],
                    "selected_task": selected,
                    "options": _options(all_tasks),
                }
            )
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks")
    def create_task():
        try:
            body = _json_body()
            store, service = stack()
            actor = authorize(
                store,
                operation="create",
                subject=f"task:new:{_client_mutation_id(body)}",
                path="/api/tasks",
                body=body,
            )
            state = str(body.get("attention_state") or "inbox")
            result = service.create(
                description=_required_text(body, "title"),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
                state=state,
                urgency="high" if body.get("urgency") == "critical" else str(body.get("urgency") or "medium"),
                tags=_tag_set(body),
                due_date=body.get("due_date"),
                deadline_date=body.get("deadline_date"),
                summary_text=body.get("summary"),
                outcome_text=body.get("desired_outcome"),
                next_action_text=body.get("next_action"),
                definition_of_done=body.get("definition_of_done"),
                dependencies=body.get("dependencies") or (),
                has_dependency=bool(body.get("dependencies")),
                creation_provenance="dashboard",
                user_required_contexts=body.get("required_contexts") or (),
                required_contexts_source=(
                    "user_authored" if body.get("required_contexts") else None
                ),
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/batch/preview")
    def preview_batch():
        try:
            body = _json_body()
            store, service = stack()
            current = access(store)
            if current["mode"] != "read_write":
                raise TaskDomainError(current.get("reason", "Tasks are read-only."))
            preview, _accepted = _batch_preview(store, service, body)
            return jsonify({"ok": True, "preview": preview})
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/batch")
    def create_batch():
        try:
            body = _json_body()
            if body.get("preview_confirmed") is not True:
                raise TaskValidationError(
                    {"preview_confirmed": "Confirm the server preview before creating tasks."}
                )
            token = body.get("preview_token")
            if not isinstance(token, str) or not token:
                raise TaskValidationError(
                    {"preview_token": "Preview this batch on the server before committing it."}
                )
            store, service = stack()
            batch_id = _client_mutation_id(body)
            actor = authorize(
                store,
                operation="batch_create",
                subject=f"task-batch:{batch_id}",
                path="/api/tasks/batch",
                body=body,
            )
            if _completed_batch_receipt(store, batch_id):
                # Response-loss retries must remain replayable after the first
                # commit changed collection state.  The receipt/service request
                # hash still rejects any changed rows or accepted indices.
                prepared_items = _replay_batch_items(body)
            else:
                preview, prepared_items = _batch_preview(store, service, body)
                if not hmac.compare_digest(token, preview["preview_token"]):
                    raise TaskValidationError(
                        {"preview_token": "This batch preview is stale; preview the rows again."}
                    )
                submitted_indices = _submitted_batch_indices(
                    body,
                    item_count=len(_batch_items(body)),
                )
                if submitted_indices != preview["accepted_indices"]:
                    raise TaskValidationError(
                        {"accepted_indices": "Commit the exact rows returned by batch preview."}
                    )
                if not prepared_items:
                    raise TaskValidationError(
                        {"items": "The preview has no valid, non-duplicate tasks to create."}
                    )
            result = service.batch_create(
                prepared_items,
                client_mutation_id=batch_id,
                actor=actor,
            )
            from work_buddy.dashboard.events import publish
            from work_buddy.tasks.events import publish_pending

            def deliver(event_type: str, payload: dict[str, Any]) -> bool:
                publish(event_type, payload)
                return True

            publish_pending(store, delivery=deliver)
            return jsonify(
                {
                    "ok": True,
                    "result": {
                        "tasks": [
                            _task_summary(item, store.get_task_document_link(item.task_id))
                            for item in result.tasks
                        ],
                        "collection_revision": result.collection_revision,
                        "receipt": result.receipt.to_dict(),
                        "replayed": result.replayed,
                    },
                }
            )
        except Exception as exc:
            return _error_response(exc)

    @blueprint.patch("/api/tasks/<task_id>")
    def update_task(task_id: str):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}"
            actor = authorize(
                store,
                operation="update",
                subject=f"task:{task_id}",
                path=path,
                body=body,
            )
            current = service.get(task_id, include_deleted=True)
            if current is None:
                raise TaskNotFound(task_id)
            requested_state = body.get("attention_state")
            if requested_state in {"snoozed", "done"}:
                if requested_state != current.state:
                    raise TaskValidationError(
                        {
                            "attention_state": (
                                "Use the Snooze or Complete action for that lifecycle change."
                            )
                        }
                    )
                requested_state = None
            result = service.update(
                task_id,
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
                changes=_task_fields(body),
                tags=_tag_set(body, current),
                state=requested_state,
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    def lifecycle(task_id: str, operation: str, invoke: Callable[..., Any]):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}/{operation}"
            actor = authorize(
                store,
                operation=operation,
                subject=f"task:{task_id}",
                path=path,
                body=body,
            )
            result = invoke(
                service,
                task_id,
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
                body=body,
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/<task_id>/complete")
    def complete_task(task_id: str):
        return lifecycle(task_id, "complete", lambda service, task_id, body, **auth: service.complete(task_id, **auth))

    @blueprint.post("/api/tasks/<task_id>/reopen")
    def reopen_task(task_id: str):
        return lifecycle(task_id, "reopen", lambda service, task_id, body, **auth: service.reopen(task_id, **auth))

    @blueprint.post("/api/tasks/<task_id>/focus")
    def focus_task(task_id: str):
        return lifecycle(task_id, "focus", lambda service, task_id, body, **auth: service.focus(task_id, **auth))

    @blueprint.post("/api/tasks/<task_id>/snooze")
    def snooze_task(task_id: str):
        return lifecycle(
            task_id,
            "snooze",
            lambda service, task_id, body, **auth: service.snooze(
                task_id, until=_required_text(body, "snooze_until"), **auth
            ),
        )

    @blueprint.post("/api/tasks/<task_id>/archive")
    def archive_task(task_id: str):
        return lifecycle(task_id, "archive", lambda service, task_id, body, **auth: service.archive(task_id, **auth))

    @blueprint.post("/api/tasks/<task_id>/unarchive")
    def unarchive_task(task_id: str):
        return lifecycle(task_id, "unarchive", lambda service, task_id, body, **auth: service.unarchive(task_id, **auth))

    @blueprint.delete("/api/tasks/<task_id>")
    def delete_task(task_id: str):
        # The provider's operation is `delete` and its exact path has no suffix.
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}"
            actor = authorize(store, operation="delete", subject=f"task:{task_id}", path=path, body=body)
            result = service.delete(
                task_id,
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/<task_id>/restore")
    def restore_task(task_id: str):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}/restore"
            actor = authorize(
                store,
                operation="restore",
                subject=f"task:{task_id}",
                path=path,
                body=body,
            )
            restored = service.restore(
                task_id,
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
            )
            link = store.get_task_document_link(task_id)
            if link is None:
                return jsonify(_mutation_envelope(store, restored))
            knowledge = document_factory().reactivate_retired(
                task_id=task_id,
                store_id=link.store_id,
                document_id=link.document_id,
                binding_id=link.binding_id,
                title=restored.task.description,
                domain_revision=str(restored.task.revision),
                created_by=actor,
            )
            now = _now()
            same_binding = knowledge.binding_id == link.binding_id
            active_link = TaskDocumentLink(
                task_id=task_id,
                note_uuid=link.note_uuid,
                store_id=knowledge.store_id,
                document_id=knowledge.document_id,
                binding_id=knowledge.binding_id,
                lifecycle="active",
                # The task/document-link row keeps its original creation time
                # even when its target is replaced; TaskStore's upsert does
                # the same, which also makes the saga request replay-stable.
                created_at=link.created_at,
                updated_at=link.updated_at if same_binding else now,
            )
            attached = service.attach_document(
                task_id,
                active_link,
                expected_revision=restored.task.revision,
                client_mutation_id=f"{_client_mutation_id(body)}:document-reactivate",
                actor=actor,
            )
            return jsonify(_mutation_envelope(store, attached))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.put("/api/tasks/<task_id>/tags")
    def replace_tags(task_id: str):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}/tags"
            actor = authorize(store, operation="replace_tags", subject=f"task:{task_id}", path=path, body=body)
            current = service.get(task_id, include_deleted=True)
            if current is None:
                raise TaskNotFound(task_id)
            result = service.replace_tags(
                task_id,
                tags=_tag_set(body, current),
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.get("/api/tasks/<task_id>/document")
    def get_document(task_id: str):
        try:
            store = store_factory()
            if store.get(task_id, include_deleted=True) is None:
                raise TaskNotFound(task_id)
            link = store.get_task_document_link(task_id)
            if link is None:
                raise TaskValidationError({"document": "This task has no knowledge document."})
            return jsonify({"ok": True, "document": _document_summary(link)})
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/<task_id>/document")
    def create_document(task_id: str):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}/document"
            actor = authorize(store, operation="create_document", subject=f"task:{task_id}", path=path, body=body)
            task = service.get(task_id, include_deleted=True)
            if task is None:
                raise TaskNotFound(task_id)
            expected = _expected_revision(body)
            existing_link = store.get_task_document_link(task_id)
            if existing_link is None and task.revision != expected:
                raise TaskRevisionConflict(
                    expected=expected,
                    current=task.revision,
                    current_task=task.to_dict(),
                )
            if existing_link is None:
                created = document_factory().create(
                    task_id=task_id,
                    title=task.description,
                    domain_revision=str(task.revision),
                    created_by=actor,
                )
                now = _now()
                link = TaskDocumentLink(
                    task_id=task_id,
                    note_uuid=task.note_uuid or str(uuid.UUID(hex=created.document_id)),
                    store_id=created.store_id,
                    document_id=created.document_id,
                    binding_id=created.binding_id,
                    lifecycle="active",
                    created_at=now,
                    updated_at=now,
                )
            else:
                link = existing_link
            try:
                result = service.attach_document(
                    task_id,
                    link=link,
                    expected_revision=expected,
                    client_mutation_id=_client_mutation_id(body),
                    actor=actor,
                )
            except TaskRevisionConflict:
                # A field edit may race the external document reservation.
                # Attaching the deterministic link cannot overwrite that edit,
                # so finish the saga against the fresh revision.
                current = service.get(task_id, include_deleted=True)
                if current is None:
                    raise TaskNotFound(task_id)
                result = service.attach_document(
                    task_id,
                    link=link,
                    expected_revision=current.revision,
                    client_mutation_id=_client_mutation_id(body),
                    actor=actor,
                )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    def action_item_route(
        task_id: str,
        operation: str,
        suffix: str,
        invoke: Callable[..., Any],
    ):
        try:
            body = _json_body()
            store, service = stack()
            path = f"/api/tasks/{_encoded(task_id)}{suffix}"
            actor = authorize(store, operation=operation, subject=f"task:{task_id}", path=path, body=body)
            result = invoke(
                service,
                body,
                expected_revision=_expected_revision(body),
                client_mutation_id=_client_mutation_id(body),
                actor=actor,
            )
            return jsonify(_mutation_envelope(store, result))
        except Exception as exc:
            return _error_response(exc)

    @blueprint.post("/api/tasks/<task_id>/action-items")
    def create_action_item(task_id: str):
        return action_item_route(
            task_id,
            "action_item_create",
            "/action-items",
            lambda service, body, **auth: service.create_action_item(
                task_id,
                description=_required_text(body, "text"),
                authorship="user",
                **auth,
            ),
        )

    @blueprint.post("/api/tasks/<task_id>/action-items/reorder")
    def reorder_action_items(task_id: str):
        return action_item_route(
            task_id,
            "action_item_reorder",
            "/action-items/reorder",
            lambda service, body, **auth: service.reorder_action_items(
                task_id,
                action_item_ids=[int(value) for value in body.get("action_item_ids", [])],
                **auth,
            ),
        )

    def item_action(task_id: str, action_item_id: str, kind: str):
        try:
            item_id = int(action_item_id)
        except ValueError as exc:
            return _error_response(
                TaskValidationError({"action_item_id": "Action item ID must be an integer."})
            )
        suffix = f"/action-items/{_encoded(action_item_id)}"
        operation = f"action_item_{kind}"
        if kind == "current":
            suffix += "/current"
            invoke = lambda service, body, **auth: service.set_current_action_item(task_id, action_item_id=item_id, **auth)
        elif kind == "approve":
            suffix += "/approve"
            invoke = lambda service, body, **auth: service.approve_action_item(task_id, item_id, **auth)
        elif kind == "restore":
            suffix += "/restore"
            invoke = lambda service, body, **auth: service.restore_action_item(task_id, item_id, **auth)
        elif kind == "delete":
            invoke = lambda service, body, **auth: service.delete_action_item(task_id, item_id, **auth)
        else:
            def invoke(service, body, **auth):
                changes = {
                    key: value
                    for key, value in body.items()
                    if key
                    not in {
                        "task_id",
                        "action_item_id",
                        "expected_revision",
                        "client_mutation_id",
                        "text",
                        "completed",
                    }
                }
                if "text" in body:
                    changes["description"] = body["text"]
                if "completed" in body:
                    if not isinstance(body["completed"], bool):
                        raise TaskValidationError(
                            {"completed": "Completed must be true or false."}
                        )
                    changes["state"] = "done" if body["completed"] else "pending"
                return service.update_action_item(
                    task_id,
                    item_id,
                    changes=changes,
                    **auth,
                )
        return action_item_route(task_id, operation, suffix, invoke)

    @blueprint.patch("/api/tasks/<task_id>/action-items/<action_item_id>")
    def update_action_item(task_id: str, action_item_id: str):
        return item_action(task_id, action_item_id, "update")

    @blueprint.post("/api/tasks/<task_id>/action-items/<action_item_id>/current")
    def current_action_item(task_id: str, action_item_id: str):
        return item_action(task_id, action_item_id, "current")

    @blueprint.post("/api/tasks/<task_id>/action-items/<action_item_id>/approve")
    def approve_action_item(task_id: str, action_item_id: str):
        return item_action(task_id, action_item_id, "approve")

    @blueprint.delete("/api/tasks/<task_id>/action-items/<action_item_id>")
    def delete_action_item(task_id: str, action_item_id: str):
        return item_action(task_id, action_item_id, "delete")

    @blueprint.post("/api/tasks/<task_id>/action-items/<action_item_id>/restore")
    def restore_action_item(task_id: str, action_item_id: str):
        return item_action(task_id, action_item_id, "restore")

    @blueprint.post("/api/tasks/<task_id>/local-files/<link_id>/action")
    def local_file_action(task_id: str, link_id: str):
        try:
            from work_buddy.cowork.folder_api import _has_local_picker_intent
            from work_buddy.cowork.local_files import (
                DefaultLocalFileOsActions,
                LOCAL_FILE_OPEN_INTENT,
                LOCAL_FILE_REVEAL_INTENT,
                LocalFileLinkError,
                LocalFileLinkRegistry,
            )

            body = _json_body()
            store = store_factory()
            path = f"/api/tasks/{_encoded(task_id)}/local-files/{_encoded(link_id)}/action"
            authorize(store, operation="local_file_action", subject=f"task:{task_id}", path=path, body=body)
            document = store.get_task_document_link(task_id)
            if document is None:
                raise TaskValidationError({"document": "This task has no knowledge document."})
            action = _required_text(body, "action")
            expected_intent = (
                LOCAL_FILE_OPEN_INTENT if action == "open" else LOCAL_FILE_REVEAL_INTENT
            )
            if action not in {"open", "reveal"} or not _has_local_picker_intent(expected_intent):
                raise LocalFileLinkError(
                    "local_file_intent_required",
                    "A direct local user action is required.",
                    status=403,
                )
            registry = (
                local_registry_factory()
                if local_registry_factory is not None
                else LocalFileLinkRegistry.default()
            )
            link = registry.get_document_link(
                store_id=document.store_id,
                document_id=document.document_id,
                link_id=link_id,
            )
            if link.task_id not in {None, task_id} or link.allowed_action != action:
                raise LocalFileLinkError(
                    "local_file_action_forbidden",
                    "That action is not allowed for this linked file.",
                    status=403,
                )
            verified = registry.verified_path(link)
            actions = local_os_actions or DefaultLocalFileOsActions()
            if action == "open":
                actions.open_pdf(verified)
            else:
                actions.reveal(verified)
            return jsonify({"ok": True, "action": action, "link_id": link_id})
        except Exception as exc:
            try:
                from work_buddy.cowork.local_files import LocalFileLinkError

                if isinstance(exc, LocalFileLinkError):
                    return jsonify(
                        {
                            "ok": False,
                            "error": {
                                "code": exc.code,
                                "message": str(exc),
                                "retryable": exc.retryable,
                            },
                        }
                    ), exc.status
            except Exception:
                pass
            return _error_response(exc)

    return blueprint


def register_routes(app) -> None:
    app.register_blueprint(create_tasks_blueprint())


__all__ = ["create_tasks_blueprint", "register_routes"]
