"""Explicit backup classes for Source Foundation persistent resources."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackupResourcePolicy:
    resource_id: str
    backup_class: str
    strategy: str
    restore_rule: str


SOURCE_FOUNDATION_BACKUP_POLICY = {
    "db/agent-execution": BackupResourcePolicy(
        "db/agent-execution",
        "content_free_vital",
        "sqlite_hot_backup",
        "Retain possibly_sent state; never infer not-sent from absence.",
    ),
    "db/cowork-conversation-source-dependencies": BackupResourcePolicy(
        "db/cowork-conversation-source-dependencies",
        "content_free_vital",
        "sqlite_hot_backup",
        "Validate every retained message dependency; review-required rows remain blocking.",
    ),
    "db/task-note-migration": BackupResourcePolicy(
        "db/task-note-migration",
        "content_free_vital",
        "sqlite_hot_backup",
        "Start authority changes paused until document epochs reconcile.",
    ),
    "db/installed-authority": BackupResourcePolicy(
        "db/installed-authority",
        "content_free_vital",
        "sqlite_hot_backup_and_irreversible_restore_union",
        "Never discard a live or restored sealed-domain latch; conflicting bindings block restore.",
    ),
    "db/local-identity": BackupResourcePolicy(
        "db/local-identity",
        "authorized_sensitive_export_only",
        "sanitized_enrollment_manifest",
        "Never restore sessions or gestures; enrollment requires explicit trust.",
    ),
    "db/journal-capture": BackupResourcePolicy(
        "db/journal-capture",
        "authorized_sensitive_export_only",
        "preserve_live_or_reconstruct_review_required",
        "An installed seal makes missing or invalid Journal state fail closed; never resume Markdown.",
    ),
    "stores/sources": BackupResourcePolicy(
        "stores/sources",
        "authorized_sensitive_export_only",
        "sources_maintenance_operator",
        "Never auto-restore before current redaction fencing is proven.",
    ),
    "truth-store/document-causality": BackupResourcePolicy(
        "truth-store/document-causality",
        "content_free_vital",
        "identity_bound_portable_companion",
        "Import only beside the matching portable Truth ledger into a clean target.",
    ),
}


def validate_source_foundation_backup_policy(resource_ids: set[str]) -> None:
    missing = resource_ids - set(SOURCE_FOUNDATION_BACKUP_POLICY)
    if missing:
        raise RuntimeError(
            "unclassified Source Foundation persistent resources: "
            + ", ".join(sorted(missing))
        )
    valid = {
        "content_free_vital",
        "authorized_sensitive_export_only",
        "ephemeral/rebuildable",
    }
    invalid = {
        key: value.backup_class
        for key, value in SOURCE_FOUNDATION_BACKUP_POLICY.items()
        if value.backup_class not in valid
    }
    if invalid:
        raise RuntimeError(f"invalid backup resource classes: {invalid}")


__all__ = [
    "BackupResourcePolicy",
    "SOURCE_FOUNDATION_BACKUP_POLICY",
    "validate_source_foundation_backup_policy",
]
