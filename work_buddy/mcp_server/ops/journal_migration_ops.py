"""Guarded Journal content migration operator capability."""

from __future__ import annotations

from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.mcp_server.op_registry import register_op


@requires_consent(
    operation="journal.content_migration.select",
    reason="Assign durable migration identity to an exact Journal passage.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_select() -> None:
    return None


@requires_consent(
    operation="journal.content_migration.shadow_import",
    reason="Capture and bind an exact Journal passage as a parity-checked shadow document.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_shadow_import() -> None:
    return None


@requires_consent(
    operation="journal.content_migration.cutover",
    reason="Move one Journal content entity's authority to its bound Co-work document.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_cutover() -> None:
    return None


@requires_consent(
    operation="journal.content_migration.rollback",
    reason="Fence one Co-work Journal authority epoch and restore Markdown authority.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_rollback() -> None:
    return None


@requires_consent(
    operation="journal.content_migration.reconcile",
    reason="Resume incomplete Journal authority and projection receipts.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_reconcile() -> None:
    return None


@requires_consent(
    operation="journal.content_migration.certify_exit",
    reason="Persist a Journal migration exit-evidence receipt for dependent cutovers.",
    risk="high",
    consent_weight="high",
    default_ttl=0,
)
def _authorize_certify_exit() -> None:
    return None


def journal_content_migration_operator(
    action: str,
    entity_kind: str | None = None,
    entity_id: str | None = None,
    day_id: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    rollback_deadline: str | None = None,
) -> dict[str, Any]:
    """Run one explicit, content-minimized Journal migration action."""

    authorizers = {
        "select": _authorize_select,
        "shadow_import": _authorize_shadow_import,
        "cutover": _authorize_cutover,
        "rollback": _authorize_rollback,
        "reconcile": _authorize_reconcile,
        "certify_exit": _authorize_certify_exit,
    }
    if action != "inventory":
        authorize = authorizers.get(action)
        if authorize is None:
            raise ValueError("unsupported Journal migration action")
        # The consent boundary deliberately precedes configuration, identity,
        # and store construction. A denial therefore leaves every DB and file
        # untouched, not merely the eventual authority binding.
        authorize()

    from work_buddy import config as config_module
    from work_buddy.dashboard import local_identity_api
    from work_buddy.journal_capture.migration import (
        JournalMigrationService,
        build_journal_content_inventory,
    )
    from work_buddy.journal_capture.operator import JournalMigrationOperator
    from work_buddy.journal_capture.store import JournalCaptureStore
    from work_buddy.paths import resolve
    from work_buddy.sources import ActorRef, SourceStore

    cfg = config_module.load_config() or {}
    vault_root = cfg.get("vault_root")
    if not isinstance(vault_root, str) or not vault_root:
        raise ValueError("vault_root is required for Journal migration")
    journal_cfg = cfg.get("journal", {})
    migration_cfg = (
        journal_cfg.get("content_migration", {}) if isinstance(journal_cfg, dict) else {}
    )
    cutover_enabled = (
        isinstance(migration_cfg, dict)
        and migration_cfg.get("enabled") is True
        and migration_cfg.get("cutover_enabled") is True
    )
    if action == "inventory":
        journal_path = resolve("db/journal-capture")
        journal = (
            JournalCaptureStore(journal_path, read_only=True)
            if journal_path.is_file()
            else None
        )
        return build_journal_content_inventory(
            vault_root=vault_root,
            journal_store=journal,
            cutover_enabled=cutover_enabled,
        )
    enrolled = local_identity_api._authority().enrolled_actor()
    principal = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-journal-migration-service",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    with JournalMigrationService(
        vault_root=vault_root,
        journal_store=JournalCaptureStore(resolve("db/journal-capture")),
        source_store=SourceStore.create(resolve("stores/sources")),
        principal=principal,
        cutover_enabled=cutover_enabled,
    ) as service:
        operator = JournalMigrationOperator(service)
        if action == "certify_exit":
            return operator.certify_exit()
        if action == "select":
            if entity_kind is None or day_id is None:
                raise ValueError("select requires entity_kind and day_id")
            return operator.select(
                entity_kind=entity_kind,
                day_id=day_id,
                entity_id=entity_id,
                start_line=start_line,
                end_line=end_line,
            )
        if action == "reconcile" and entity_kind is None and entity_id is None:
            return operator.reconcile()
        if entity_kind is None or entity_id is None:
            raise ValueError(f"{action} requires entity_kind and entity_id")
        if action == "shadow_import":
            return operator.shadow_import(entity_kind, entity_id)
        if action == "cutover":
            if rollback_deadline is None:
                raise ValueError("cutover requires rollback_deadline")
            return operator.cutover(
                entity_kind, entity_id, rollback_deadline=rollback_deadline
            )
        if action == "rollback":
            return operator.rollback(entity_kind, entity_id)
        if action == "reconcile":
            return operator.reconcile(entity_kind, entity_id)
        raise ValueError("unsupported Journal migration action")


register_op(
    "op.wb.journal_content_migration_operator",
    journal_content_migration_operator,
    replace=True,
)


__all__ = ["journal_content_migration_operator"]
