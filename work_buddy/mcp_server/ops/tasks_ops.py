"""Task-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The declaration supplies
the prose, parameter schema, and runtime metadata; the op supplies the callable.

``task_create`` also registers the frozen legacy Markdown effect manifest used
to recover an uncertain bridge write.  The manifest is static registry
metadata: its resolver checks authority only when verification actually needs
it, and native runtime guards reject legacy effects before that resolver can
run.  Registry construction therefore never probes the fallible authority
latch.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable

from work_buddy.mcp_server.op_registry import register_op, register_op_effects


def _task_dispatch(
    native_name: str,
    compatibility_module: str,
    compatibility_name: str,
    *args: Any,
    _mutation: bool = False,
    **kwargs: Any,
) -> Any:
    """Resolve task authority at invocation time, not registry-build time.

    The gateway is intentionally long lived and can span a cutover.  Binding a
    legacy reader during process startup would otherwise leave a callable that
    can still reach Obsidian after the native epoch commits.
    """

    from work_buddy.tasks.runtime import (
        native_authority_active,
        native_task_mutation_authority,
    )

    native = (
        native_task_mutation_authority()
        if _mutation
        else native_authority_active()
    )
    if native:
        from work_buddy.tasks import capabilities

        return getattr(capabilities, native_name)(*args, **kwargs)
    compatibility = import_module(compatibility_module)
    return getattr(compatibility, compatibility_name)(*args, **kwargs)


def _dynamic_task_op(
    native_name: str,
    compatibility_module: str,
    compatibility_name: str,
) -> Callable[..., Any]:
    def dispatch(*args: Any, **kwargs: Any) -> Any:
        return _task_dispatch(
            native_name,
            compatibility_module,
            compatibility_name,
            *args,
            **kwargs,
        )

    dispatch.__name__ = f"authority_routed_{native_name}"
    return dispatch


def _compat_task_create_effects_resolver(
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the frozen Markdown manifest only for a legacy invocation.

    Effect metadata must be registered without consulting the durable
    authority latch: op modules are imported while the registry is only
    partially populated, so a latch error must not leave a half-built
    registry.  The gateway and retry sweep reject native task effects before
    reaching this resolver; this check is defense in depth for direct callers.
    The legacy mutations module remains lazy so a native registry build does
    not import the retired task writer.
    """

    from work_buddy.tasks.runtime import native_task_mutation_authority

    if native_task_mutation_authority():
        return None

    from work_buddy.obsidian.tasks.mutations import (
        create_task_effects_resolver,
    )

    return create_task_effects_resolver(params)


_task_read = _dynamic_task_op(
    "task_read", "work_buddy.obsidian.tasks.mutations", "read_task"
)
_task_provenance = _dynamic_task_op(
    "task_provenance", "work_buddy.obsidian.tasks.provenance", "build_task_provenance"
)
_task_note_readers = _dynamic_task_op(
    "task_note_readers", "work_buddy.obsidian.tasks.provenance", "sessions_who_read_task"
)
_task_briefing = _dynamic_task_op(
    "daily_briefing", "work_buddy.obsidian.tasks.manager", "daily_briefing"
)
_task_review_inbox = _dynamic_task_op(
    "review_inbox", "work_buddy.obsidian.tasks.manager", "review_inbox"
)
_task_stale_check = _dynamic_task_op(
    "stale_check", "work_buddy.obsidian.tasks.manager", "stale_check"
)
_task_search = _dynamic_task_op(
    "task_search", "work_buddy.obsidian.tasks.manager", "task_search"
)
_task_list = _dynamic_task_op(
    "task_list", "work_buddy.obsidian.tasks.manager", "task_list"
)
_weekly_review_data = _dynamic_task_op(
    "weekly_review_data", "work_buddy.obsidian.tasks.manager", "weekly_review_data"
)
_task_namespace_suggest = _dynamic_task_op(
    "task_namespace_suggest",
    "work_buddy.obsidian.tasks.namespace_suggest",
    "task_namespace_suggest",
)
_namespace_lookup = _dynamic_task_op(
    "namespace_lookup",
    "work_buddy.obsidian.tasks.namespace_suggest",
    "namespace_lookup",
)


def _compat_archive_completed(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _task_dispatch(
        "archive_completed",
        "work_buddy.obsidian.tasks.mutations",
        "archive_completed",
        *args,
        _mutation=True,
        **kwargs,
    )


def _compat_task_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _task_dispatch(
        "retired_legacy_surface",
        "work_buddy.obsidian.tasks.sync",
        "task_sync",
        *args,
        _mutation=True,
        **kwargs,
    )


def _task_scattered(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _task_dispatch(
        "retired_legacy_surface",
        "work_buddy.mcp_server.context_wrappers",
        "task_scattered",
        *args,
        **kwargs,
    )


def session_tasks_get(session_id: str) -> dict[str, Any]:
    """Return the tasks a session was assigned to, with text + state.

    The reverse of task→sessions. Reads the ``task_sessions`` table and
    enriches each row from the SQLite task store — bridge-independent, so
    it stays callable when the Obsidian bridge is down (unlike a
    ``task_read``-based enrichment, which would hang on a downed bridge).
    Returns ``{"tasks": [{task_id, assigned_at, task_text, state}, ...]}``
    oldest-first.
    """
    from work_buddy.tasks.runtime import native_authority_active

    if native_authority_active():
        from work_buddy.tasks.capabilities import session_tasks_get as native_get

        return native_get(session_id)

    from work_buddy.obsidian.tasks import store
    from work_buddy.threads.models import Task

    out: list[dict[str, Any]] = []
    for row in store.get_tasks_for_session(session_id):
        # Enrich through the WorkItem family; Task.load carries its row.
        _t = Task.load(row["task_id"])
        rec = _t.row if _t is not None else None
        out.append({
            "task_id": row["task_id"],
            "assigned_at": row.get("assigned_at"),
            "task_text": (rec or {}).get("description"),
            "state": (rec or {}).get("state"),
        })
    return {"tasks": out}


def _register() -> None:
    # Lazy imports inside the registration function, matching the
    # lazy-import discipline of the registry's capability builders
    # (see architecture/mcp-import-discipline).
    from work_buddy import contracts
    from work_buddy.projects.authority import reconcile_projects_authoritatively
    from work_buddy.obsidian.effects import EffectSpec
    from work_buddy.threads.models import Task
    from work_buddy.tasks.capabilities import task_creation_reconcile
    from work_buddy.work_item import task_adapter

    # Every task read resolves authority on each invocation.  A registry built
    # before cutover therefore cannot retain a callable into Obsidian.
    register_op("op.wb.task_read", _task_read)
    register_op("op.wb.task_provenance", _task_provenance)
    register_op("op.wb.task_note_readers", _task_note_readers)
    register_op("op.wb.task_briefing", _task_briefing)
    register_op("op.wb.task_review_inbox", _task_review_inbox)
    register_op("op.wb.task_stale_check", _task_stale_check)
    register_op("op.wb.task_search", _task_search)
    register_op("op.wb.task_list", _task_list)
    register_op("op.wb.weekly_review_data", _weekly_review_data)
    # Mutator ops route through the WorkItem write port (the ``Task`` instance
    # methods delegate there too). The port resolves authority for every call:
    # revisioned TaskStore services under native authority, fenced Obsidian
    # compatibility only before cutover. ``task_create`` routes through the
    # ``Task.create`` classmethod; verb ops take a task_id.
    register_op("op.wb.task_create", Task.create)
    register_op(
        "op.wb.task_creation_reconcile",
        task_creation_reconcile,
    )
    register_op("op.wb.task_set_tags", task_adapter.set_tags)
    register_op("op.wb.task_assign", task_adapter.assign)
    register_op("op.wb.task_toggle", task_adapter.toggle)
    register_op("op.wb.task_delete", task_adapter.delete)
    register_op("op.wb.task_change_state", task_adapter.update)
    register_op("op.wb.task_update_description", task_adapter.set_description)
    register_op("op.wb.task_archive", _compat_archive_completed)
    register_op("op.wb.task_namespace_suggest", _task_namespace_suggest)
    register_op("op.wb.namespace_lookup", _namespace_lookup)
    register_op("op.wb.task_sync", _compat_task_sync)
    register_op("op.wb.task_scattered", _task_scattered)
    register_op("op.wb.project_sync", reconcile_projects_authoritatively)
    register_op("op.wb.session_tasks_get", session_tasks_get)
    register_op("op.wb.contract_constraints", contracts.get_constraints)
    register_op("op.wb.contract_wip_check", contracts.check_wip_limit)

    # task_create's effect manifest — the multi-effect verifier uses it to
    # detect partial PostWriteUncertain states. The resolver pulls task_id /
    # note_uuid from the idempotency cache.
    register_op_effects("op.wb.task_create", [
        EffectSpec(
            kind="file_write",
            path_template="tasks/notes/{note_uuid}.md",
            witness_template="{task_text}",
            witness_mode="substring",
            resolver=_compat_task_create_effects_resolver,
        ),
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            witness_mode="substring",
            resolver=_compat_task_create_effects_resolver,
        ),
    ])


_register()
