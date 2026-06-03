"""Task-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The declaration supplies
the prose, parameter schema, and runtime metadata; the op supplies the callable.

``task_create`` also registers an effect manifest — effects are code (an
``EffectSpec`` carries a ``resolver`` callable) so they cannot ride in a data
declaration; the loader threads them onto the resolved capability by op id.
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op, register_op_effects


def session_tasks_get(session_id: str) -> dict[str, Any]:
    """Return the tasks a session was assigned to, with text + state.

    The reverse of task→sessions. Reads the ``task_sessions`` table and
    enriches each row from the SQLite task store — bridge-independent, so
    it stays callable when the Obsidian bridge is down (unlike a
    ``task_read``-based enrichment, which would hang on a downed bridge).
    Returns ``{"tasks": [{task_id, assigned_at, task_text, state}, ...]}``
    oldest-first.
    """
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
    from work_buddy.mcp_server.context_wrappers import task_scattered
    from work_buddy.obsidian.effects import EffectSpec
    from work_buddy.obsidian.tasks import manager, mutations, provenance
    from work_buddy.obsidian.tasks.namespace_suggest import (
        namespace_lookup,
        task_namespace_suggest,
    )
    from work_buddy.obsidian.tasks.sync import task_sync
    from work_buddy.projects.markdown_db import reconcile_projects
    from work_buddy.threads.models import Task
    from work_buddy.work_item import task_adapter

    register_op("op.wb.task_read", mutations.read_task)
    register_op("op.wb.task_provenance", provenance.build_task_provenance)
    register_op("op.wb.task_note_readers", provenance.sessions_who_read_task)
    register_op("op.wb.task_briefing", manager.daily_briefing)
    register_op("op.wb.task_review_inbox", manager.review_inbox)
    register_op("op.wb.task_stale_check", manager.stale_check)
    register_op("op.wb.task_search", manager.task_search)
    register_op("op.wb.weekly_review_data", manager.weekly_review_data)
    # Mutator ops route through the WorkItem write port (``work_item.task_adapter``
    # — the surface ``Task`` instance methods also delegate to) so no task
    # mutation bypasses the WorkItem family. The port is a pure pass-through to
    # the mutation layer, which keeps owning the atomic dual-surface write,
    # plugin-marker preservation, consent, bridge-retry, and event emission — so
    # dispatch is unchanged in behaviour. ``task_create`` routes through the
    # ``Task.create`` classmethod (it mints a new id); the verb ops take a task_id.
    register_op("op.wb.task_create", Task.create)
    register_op("op.wb.task_set_tags", task_adapter.set_tags)
    register_op("op.wb.task_assign", task_adapter.assign)
    register_op("op.wb.task_toggle", task_adapter.toggle)
    register_op("op.wb.task_delete", task_adapter.delete)
    register_op("op.wb.task_change_state", task_adapter.update)
    register_op("op.wb.task_update_description", task_adapter.set_description)
    # Reads, the bulk archive sweep, and aggregates stay on the mutation layer.
    register_op("op.wb.task_archive", mutations.archive_completed)
    register_op("op.wb.task_namespace_suggest", task_namespace_suggest)
    register_op("op.wb.namespace_lookup", namespace_lookup)
    register_op("op.wb.task_sync", task_sync)
    register_op("op.wb.project_sync", reconcile_projects)
    register_op("op.wb.task_scattered", task_scattered)
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
            resolver=mutations.create_task_effects_resolver,
        ),
        EffectSpec(
            kind="line_append",
            path="tasks/master-task-list.md",
            witness_template="🆔 {task_id}",
            witness_mode="substring",
            resolver=mutations.create_task_effects_resolver,
        ),
    ])


_register()
