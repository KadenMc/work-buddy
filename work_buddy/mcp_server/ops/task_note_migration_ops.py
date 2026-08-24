"""Conservative one-note migration operator capability."""

from __future__ import annotations

from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.mcp_server.op_registry import register_op


@requires_consent(
    "tasks.task_note_authority_change",
    "Change a task-note migration gate or its canonical content authority.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_authority_change() -> None:
    return None


def task_note_migration_operator(
    action: str,
    note_uuid: str | None = None,
    rollback_deadline: str | None = None,
    gate: str | None = None,
    enabled: bool | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Run one explicit migration/recovery action without reading note content out."""

    from work_buddy.tasks.runtime import native_authority_active

    if native_authority_active():
        return {
            "success": False,
            "retired": True,
            "error": (
                "The per-note Markdown migration operator is retired under "
                "native task authority. Use the native task cutover/rollback operator."
            ),
        }

    from work_buddy import config as config_module
    from work_buddy.dashboard import local_identity_api
    from work_buddy.journal_capture.migration import latest_current_exit_evidence
    from work_buddy.journal_capture.store import JournalCaptureStore
    from work_buddy.paths import resolve
    from work_buddy.sources import ActorRef, SourceStore
    from work_buddy.task_notes.operator import TaskNoteMigrationOperator
    from work_buddy.task_notes.store import TaskNoteMigrationStore

    cfg = config_module.load_config() or {}
    vault_root = cfg.get("vault_root")
    if not isinstance(vault_root, str) or not vault_root:
        raise ValueError("vault_root is required for task-note migration")
    journal_cfg = cfg.get("journal", {})
    migration_cfg = (
        journal_cfg.get("content_migration", {})
        if isinstance(journal_cfg, dict)
        else {}
    )
    journal_cutover_enabled = (
        isinstance(migration_cfg, dict)
        and migration_cfg.get("enabled") is True
        and migration_cfg.get("cutover_enabled") is True
    )
    journal_store = JournalCaptureStore(resolve("db/journal-capture"))
    enrolled = local_identity_api._authority().enrolled_actor()
    principal = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-task-note-service",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    operator = TaskNoteMigrationOperator(
        vault_root=vault_root,
        migrations=TaskNoteMigrationStore(resolve("db/task-note-migration")),
        sources=SourceStore.create(resolve("stores/sources")),
        principal=principal,
        journal_exit_evidence_provider=lambda: latest_current_exit_evidence(
            vault_root=vault_root,
            journal_store=journal_store,
            cutover_enabled=journal_cutover_enabled,
        ),
    )
    if action == "inventory":
        return operator.inventory()
    if action == "set_gate":
        _authorize_authority_change()
        if gate is None or enabled is None:
            raise ValueError("set_gate requires gate and enabled")
        return operator.set_gate(gate, enabled)
    if action == "recover":
        return operator.recover(limit=max(1, min(int(limit), 100)))
    if note_uuid is None:
        raise ValueError(f"{action} requires note_uuid")
    if action == "shadow_import":
        return operator.shadow_import(note_uuid)
    if action == "validate_parity":
        return operator.validate_parity(note_uuid)
    if action == "cutover":
        _authorize_authority_change()
        if rollback_deadline is None:
            raise ValueError("cutover requires rollback_deadline")
        return operator.cutover(note_uuid, rollback_deadline=rollback_deadline)
    if action == "rollback":
        _authorize_authority_change()
        return operator.rollback(note_uuid)
    raise ValueError("unsupported task-note migration action")


register_op(
    "op.wb.task_note_migration_operator",
    task_note_migration_operator,
    replace=True,
)


__all__ = ["task_note_migration_operator"]
