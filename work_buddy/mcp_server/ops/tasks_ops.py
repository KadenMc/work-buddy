"""Task-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The declaration supplies
the prose, parameter schema, and runtime metadata; the op supplies the callable.

``task_create`` also registers an effect manifest — effects are code (an
``EffectSpec`` carries a ``resolver`` callable) so they cannot ride in a data
declaration; the loader threads them onto the resolved capability by op id.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op, register_op_effects


def _register() -> None:
    # Lazy imports inside the registration function, matching the
    # lazy-import discipline of the registry's capability builders
    # (see architecture/mcp-import-discipline).
    from work_buddy import contracts
    from work_buddy.mcp_server.context_wrappers import task_scattered
    from work_buddy.obsidian.effects import EffectSpec
    from work_buddy.obsidian.tasks import manager, mutations
    from work_buddy.obsidian.tasks.namespace_suggest import (
        namespace_lookup,
        task_namespace_suggest,
    )
    from work_buddy.obsidian.tasks.sync import task_sync
    from work_buddy.projects.markdown_db import reconcile_projects

    register_op("op.wb.task_read", mutations.read_task)
    register_op("op.wb.task_briefing", manager.daily_briefing)
    register_op("op.wb.task_review_inbox", manager.review_inbox)
    register_op("op.wb.task_stale_check", manager.stale_check)
    register_op("op.wb.task_search", manager.task_search)
    register_op("op.wb.weekly_review_data", manager.weekly_review_data)
    register_op("op.wb.task_create", mutations.create_task)
    register_op("op.wb.task_set_tags", mutations.set_task_tags_on_line)
    register_op("op.wb.task_assign", mutations.assign_task)
    register_op("op.wb.task_toggle", mutations.toggle_task)
    register_op("op.wb.task_delete", mutations.delete_task)
    register_op("op.wb.task_change_state", mutations.update_task)
    register_op("op.wb.task_update_description", mutations.update_task_description)
    register_op("op.wb.task_archive", mutations.archive_completed)
    register_op("op.wb.task_namespace_suggest", task_namespace_suggest)
    register_op("op.wb.namespace_lookup", namespace_lookup)
    register_op("op.wb.task_sync", task_sync)
    register_op("op.wb.project_sync", reconcile_projects)
    register_op("op.wb.task_scattered", task_scattered)
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
