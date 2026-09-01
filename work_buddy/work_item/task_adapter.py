"""Task write port with a runtime authority-epoch strangler seam.

The single place that translates "write intent against a task" into the native
TaskStore application service.  Before cutover only, the same functions retain
a fenced compatibility branch into ``work_buddy.obsidian.tasks.mutations``.
Authority is resolved for every invocation so a long-lived process cannot keep
writing task Markdown after native activation.

Design rules this module honours:

* **Stateless, id-keyed.** Every function takes a ``task_id`` (or, for
  :func:`create`, the new task's text) plus field values — never a ``Task``
  instance. The module has no dependency on ``work_buddy.threads``, which keeps
  the dependency one-way (``Task`` → adapter → ``mutations``) and free of import
  cycles.
* **Native first.** Native writes produce revisioned receipts and never render
  Markdown. The Obsidian mutation import is reachable only while the verified
  authority epoch remains pre-cutover.
* **Import-light.** Both authority implementations are imported inside each
  function, never at module top, per ``architecture/mcp-import-discipline``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator


_creation_attribution: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "task_creation_attribution", default=None,
)


def _creation_authorship(actor: str) -> str:
    """Map the established task actor vocabulary to field authorship."""

    actor_kind = str(actor).strip().partition(":")[0].casefold()
    return "human" if actor_kind in {"human", "user", "dashboard"} else "ai"


@contextmanager
def task_creation_attribution(
    *, actor: str, session_id: str | None = None,
) -> Iterator[None]:
    """Bind a trusted durable create command's original actor across retries.

    TaskStore receipts bind both mutation key and actor. A recovered Thread
    command must therefore retain the recorded human approver rather than use
    the current sidecar/gateway session. This is attribution only: native epoch,
    mutation fencing, and the caller's approval boundary remain in force. The
    context is not exposed as a capability parameter and affects create only.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("A recorded task creation actor is required")
    token = _creation_attribution.set((actor, session_id))
    try:
        yield
    finally:
        _creation_attribution.reset(token)


def _native_active() -> bool:
    from work_buddy.tasks.runtime import native_task_mutation_authority

    return native_task_mutation_authority()


def _native_authority(operation: str, supplied: str | None = None) -> dict[str, Any]:
    from work_buddy.tasks.runtime import (
        mutation_actor,
        new_client_mutation_id,
        originating_session,
    )

    attribution = _creation_attribution.get() if operation == "create" else None
    return {
        "actor": attribution[0] if attribution is not None else mutation_actor(),
        "session_id": attribution[1] if attribution is not None else originating_session(),
        "client_mutation_id": new_client_mutation_id(operation, supplied),
    }


def _native_result(result: Any, **extra: Any) -> dict[str, Any]:
    from work_buddy.tasks.events import publish_pending_async
    from work_buddy.tasks.store import TaskStore

    publish_pending_async(TaskStore())
    return {
        "success": True,
        "task_id": result.task.task_id,
        "task": result.task.to_dict(),
        "revision": result.task.revision,
        "collection_revision": result.collection_revision,
        "receipt": result.receipt.to_dict(),
        "replayed": result.replayed,
        **extra,
    }


def _native_task(task_id: str):
    from work_buddy.tasks.errors import TaskNotFound
    from work_buddy.tasks.store import TaskStore

    task = TaskStore().get(task_id, include_deleted=True)
    if task is None:
        raise TaskNotFound(task_id)
    return task


def create(
    task_text: str,
    *,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
    contract: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    client_mutation_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a task under the currently verified authority epoch.

    The long GTD/risk/context keyword tail of ``create_task`` (``task_kind``,
    ``density``, ``creation_provenance``, ``user_involvement``,
    ``risk_profile_json``, …) is forwarded verbatim through ``**kwargs``.
    A scalar summary never selects a document role. Callers must request the
    task note, initial content, and optional Truth activation explicitly.
    Returns the stable task id plus native revision/receipt metadata after
    cutover; the compatibility result shape is preserved before cutover.
    """
    if _native_active():
        import hashlib

        from work_buddy.tasks.aggregate_creation import TaskAggregateCreationService
        from work_buddy.tasks.creation import FieldDerivation
        from work_buddy.tasks.models import Tag
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        store = TaskStore()
        service = TaskApplicationService(store)
        authority = _native_authority("create", client_mutation_id)
        session_id = authority.pop("session_id")
        native_tags = [Tag(str(tag).strip(" #"), True) for tag in (tags or [])]
        if project:
            native_tags.append(Tag(f"projects/{project.strip().strip('#/')}", True))
        accepted = {
            key: kwargs[key]
            for key in (
                "task_id",
                "state",
                "complexity",
                "deadline_date",
                "task_kind",
                "density",
                "outcome_text",
                "next_action_text",
                "definition_of_done",
                "creation_effort",
                "user_involvement",
                "creation_provenance",
                "has_dependency",
                "dependencies",
                "dependency_hint",
                "risk_profile_json",
                "automation_tier_achievable",
                "agent_required_contexts",
                "user_required_contexts",
                "required_contexts_source",
            )
            if key in kwargs
        }
        if "dependencies" in accepted and "has_dependency" not in accepted:
            accepted["has_dependency"] = bool(accepted["dependencies"])
        task_values = dict(
            description=task_text,
            urgency=urgency,
            due_date=due_date,
            contract=contract,
            summary_text=summary,
            tags=native_tags,
            **accepted,
        )
        requested_role = kwargs.get("requested_note_role")
        explicit_note = kwargs.get("initial_note", kwargs.get("note_markdown"))
        requested_truth_resolution = kwargs.get("requested_truth_policy_resolution")
        if requested_role is None and explicit_note is not None:
            raise ValueError("Choose a task note role before supplying note text")
        if requested_role is None and requested_truth_resolution is not None:
            raise ValueError("Truth requires a task note")
        rich_requested = requested_role is not None
        if rich_requested:
            if requested_role != "working_document/v1":
                raise ValueError("Unsupported task note role")
            truth_resolution = requested_truth_resolution or "disabled"
            if truth_resolution not in {"disabled", "enabled"}:
                raise ValueError("Task note Truth policy must be disabled or enabled")
            initial_markdown = str(explicit_note or "")
            authorship = _creation_authorship(str(authority["actor"]))
            result = TaskAggregateCreationService(store, task_service=service).create(
                client_mutation_id=str(authority["client_mutation_id"]),
                actor=str(authority["actor"]),
                session_id=session_id,
                task_values=task_values,
                initial_note=initial_markdown,
                requested_truth_policy_resolution=str(truth_resolution),
                field_derivations=(
                    FieldDerivation(
                        field_name="description",
                        value_sha256=hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
                        authorship=authorship,
                    ),
                    FieldDerivation(
                        field_name="task_note.initial_body",
                        value_sha256=hashlib.sha256(
                            initial_markdown.encode("utf-8")
                        ).hexdigest(),
                        authorship=authorship,
                    ),
                ),
            )
        else:
            result = service.create(
                **task_values,
                session_id=session_id,
                **authority,
            )
        return _native_result(result, note_uuid=result.task.note_uuid)

    from work_buddy.obsidian.tasks import mutations

    # These controls belong to the native document coordinator. Keep the
    # pre-cutover compatibility writer callable while mapping an explicitly
    # requested note body onto the one legacy field that can represent it.
    legacy_kwargs = dict(kwargs)
    requested_role = legacy_kwargs.pop("requested_note_role", None)
    explicit_note = legacy_kwargs.pop(
        "initial_note", legacy_kwargs.pop("note_markdown", None)
    )
    requested_truth_resolution = legacy_kwargs.pop(
        "requested_truth_policy_resolution", None
    )
    if requested_role is None and explicit_note is not None:
        raise ValueError("Choose a task note role before supplying note text")
    if requested_role is None and requested_truth_resolution is not None:
        raise ValueError("Truth requires a task note")
    if requested_role is not None:
        if requested_role != "working_document/v1":
            raise ValueError("Unsupported task note role")
        if requested_truth_resolution not in {None, "disabled", "enabled"}:
            raise ValueError("Task note Truth policy must be disabled or enabled")
        summary = str(explicit_note or "")

    return mutations.create_task(
        task_text=task_text,
        urgency=urgency,
        project=project,
        due_date=due_date,
        contract=contract,
        summary=summary,
        tags=tags,
        **legacy_kwargs,
    )


def toggle(
    task_id: str,
    *,
    done: bool | None = None,
    file_path: str | None = None,
    done_date: str | None = None,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Toggle completion through the authority-routed task service."""
    if _native_active():
        if file_path is not None:
            raise ValueError("file_path is unavailable under native task authority")
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        task = _native_task(task_id)
        authority = _native_authority("toggle", client_mutation_id)
        authority["expected_revision"] = (
            task.revision if expected_revision is None else expected_revision
        )
        service = TaskApplicationService(TaskStore())
        if done is True:
            result = service.complete(task_id, done_date=done_date, **authority)
        elif done is False:
            result = service.reopen(task_id, **authority)
        else:
            result = service.toggle(task_id, done_date=done_date, **authority)
        return _native_result(result)

    from work_buddy.obsidian.tasks import mutations

    return mutations.toggle_task(
        task_id=task_id, done=done, file_path=file_path, done_date=done_date,
    )


def update(
    task_id: str | None = None,
    *,
    description_match: str | None = None,
    state: str | None = None,
    urgency: str | None = None,
    complexity: str | None = None,
    contract: str | None = None,
    snooze_until: str | None = None,
    due_date: str | None = None,
    reason: str | None = None,
    file_path: str | None = None,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Update task metadata through the authority-routed task service.

    ``description_match`` (a substring fallback for tasks without an id) is
    carried for full parity with the underlying mutation — instance callers
    always have an id and pass ``task_id``, but the ``task_change_state`` op
    exposes the fallback. Cannot set ``state='done'`` — ``mutations.update_task``
    rejects it; use :func:`toggle` for completion.
    """
    if _native_active():
        if file_path is not None:
            raise ValueError("file_path is unavailable under native task authority")
        from work_buddy.tasks.errors import TaskNotFound, TaskValidationError
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        store = TaskStore()
        service = TaskApplicationService(store)
        if task_id is None:
            if not description_match:
                raise TaskValidationError(
                    {"task_id": "Task ID is required under native authority."}
                )
            matches = service.search(description_match, limit=3)
            if len(matches) != 1:
                raise TaskValidationError(
                    {
                        "description_match": (
                            "Description fallback must identify exactly one task."
                        )
                    }
                )
            task_id = matches[0].task_id
        task = store.get(task_id, include_deleted=True)
        if task is None:
            raise TaskNotFound(task_id)
        revision = task.revision if expected_revision is None else expected_revision
        authority = _native_authority("update", client_mutation_id)
        changes = {
            key: value
            for key, value in {
                "urgency": urgency,
                "complexity": complexity,
                "contract": contract,
                "due_date": due_date,
            }.items()
            if value is not None
        }
        if state == "done":
            raise TaskValidationError(
                {"state": "Use task_toggle to complete a task."}
            )
        # Snooze is a lifecycle operation. Apply any ordinary fields first so
        # both changes retain their own receipt and optimistic-lock boundary.
        if state == "snoozed":
            if changes:
                interim = service.update(
                    task_id,
                    expected_revision=revision,
                    changes=changes,
                    reason=reason,
                    client_mutation_id=(
                        f"{authority['client_mutation_id']}:fields"
                    ),
                    actor=str(authority["actor"]),
                    session_id=authority.get("session_id"),
                )
                revision = interim.task.revision
            if not snooze_until:
                raise TaskValidationError(
                    {"snooze_until": "A snooze date is required."}
                )
            result = service.snooze(
                task_id,
                until=snooze_until,
                expected_revision=revision,
                client_mutation_id=str(authority["client_mutation_id"]),
                actor=str(authority["actor"]),
                session_id=authority.get("session_id"),
            )
        else:
            result = service.update(
                task_id,
                expected_revision=revision,
                changes=changes,
                state=state,
                reason=reason,
                **authority,
            )
        return _native_result(result)

    from work_buddy.obsidian.tasks import mutations

    return mutations.update_task(
        task_id=task_id,
        description_match=description_match,
        state=state,
        urgency=urgency,
        complexity=complexity,
        contract=contract,
        snooze_until=snooze_until,
        due_date=due_date,
        reason=reason,
        file_path=file_path,
    )


def set_description(
    task_id: str,
    new_description: str,
    *,
    file_path: str | None = None,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Rewrite a task's native description (or pre-cutover legacy line)."""
    if _native_active():
        if file_path is not None:
            raise ValueError("file_path is unavailable under native task authority")
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        task = _native_task(task_id)
        authority = _native_authority("description", client_mutation_id)
        result = TaskApplicationService(TaskStore()).update(
            task_id,
            expected_revision=(
                task.revision if expected_revision is None else expected_revision
            ),
            changes={"description": new_description},
            **authority,
        )
        return _native_result(result)

    from work_buddy.obsidian.tasks import mutations

    return mutations.update_task_description(
        task_id=task_id, new_description=new_description, file_path=file_path,
    )


def set_tags(
    task_id: str,
    namespace_tags: list[str],
    *,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Replace the complete desired namespace-tag set."""
    if _native_active():
        from work_buddy.tasks.models import Tag
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        task = _native_task(task_id)
        authority = _native_authority("tags", client_mutation_id)
        result = TaskApplicationService(TaskStore()).replace_tags(
            task_id,
            tags=[Tag(str(tag).strip(" #"), True) for tag in namespace_tags],
            expected_revision=(
                task.revision if expected_revision is None else expected_revision
            ),
            **authority,
        )
        return _native_result(result)

    from work_buddy.obsidian.tasks import mutations

    return mutations.set_task_tags_on_line(
        task_id=task_id, namespace_tags=namespace_tags,
    )


def delete(
    task_id: str,
    *,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Soft-delete a native task (legacy destructive semantics pre-cutover)."""
    if _native_active():
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        task = _native_task(task_id)
        authority = _native_authority("delete", client_mutation_id)
        result = TaskApplicationService(TaskStore()).delete(
            task_id,
            expected_revision=(
                task.revision if expected_revision is None else expected_revision
            ),
            **authority,
        )
        return _native_result(result, soft_deleted=True)

    from work_buddy.obsidian.tasks import mutations

    return mutations.delete_task(task_id=task_id)


def assign(
    task_id: str,
    *,
    expected_revision: int | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Claim a task for the current agent session and return its context."""
    if _native_active():
        from work_buddy.tasks.errors import TaskValidationError
        from work_buddy.tasks.runtime import originating_session
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore

        task = _native_task(task_id)
        session_id = originating_session()
        if not session_id:
            raise TaskValidationError(
                {"session_id": "An originating agent session is required."}
            )
        authority = _native_authority("assign", client_mutation_id)
        authority.pop("session_id")
        result = TaskApplicationService(TaskStore()).assign(
            task_id,
            session_id,
            expected_revision=(
                task.revision if expected_revision is None else expected_revision
            ),
            actor_session_id=session_id,
            **authority,
        )
        link = TaskStore().get_task_document_link(task_id)
        return _native_result(
            result,
            session_id=session_id,
            document=None if link is None else link.to_dict(),
        )

    from work_buddy.obsidian.tasks import mutations

    return mutations.assign_task(task_id=task_id)
