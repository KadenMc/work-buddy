from __future__ import annotations

import pytest

from work_buddy.document_kernel.causality import DomainDocumentBinding
from work_buddy.document_kernel.cowork_integration import (
    project_bound_document,
    reconcile_document_source_dependency,
    reconcile_journal_documents,
)
from work_buddy.journal_capture.store import JournalCaptureStore


def _retire_legacy_projection(store: JournalCaptureStore, mode: str) -> None:
    with store.transaction() as conn:
        if mode == "database_only":
            conn.execute(
                "UPDATE journal_authority_control SET mode='database_only' "
                "WHERE singleton=1"
            )
        else:
            conn.execute(
                "UPDATE journal_authority_control SET mode='recovery_fenced',"
                "prior_mode='legacy_compatibility',fence_code='test',"
                "fenced_at='2026-08-27T00:00:00+00:00' WHERE singleton=1"
            )
        conn.execute(
            "UPDATE journal_domain_state SET value=? WHERE key='content_authority'",
            (mode,),
        )


def _legacy_binding() -> DomainDocumentBinding:
    return DomainDocumentBinding(
        binding_id="legacy-journal-binding",
        domain_namespace="journal",
        domain_kind="running_note",
        domain_entity_id="legacy-entry",
        domain_revision="journal-entry-v1",
        store_id="legacy-store",
        document_id="legacy-document",
        role="running_note",
        lifecycle="current",
        content_authority="co_work",
        content_authority_epoch=1,
        projection_path="journal/2026-08-20.md",
        projection_mode="managed_section",
        migration_origin="legacy",
        created_at="2026-08-20T00:00:00+00:00",
        created_by="test",
        superseded_at=None,
        superseded_by=None,
    )


@pytest.mark.parametrize("mode", ["database_only", "recovery_fenced"])
def test_retired_journal_authority_never_reads_or_projects_legacy_binding(
    tmp_path, monkeypatch, mode
):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    _retire_legacy_projection(journal, mode)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("legacy Journal projection state was read")

    monkeypatch.setattr(journal, "list_document_bindings", unexpected)
    monkeypatch.setattr(journal, "list_migrations", unexpected)
    monkeypatch.setattr(journal, "get_document_binding", unexpected)

    assert reconcile_journal_documents(
        journal_store=journal,
        source_store=object(),
        source_principal=object(),
        vault_root=tmp_path / "must-not-be-read",
    ) == ()
    assert reconcile_document_source_dependency(
        object(),
        binding=_legacy_binding(),
        source_store=object(),
        source_principal=object(),
        journal_store=journal,
    ) is None
    assert project_bound_document(
        object(),
        binding=_legacy_binding(),
        change=None,
        source_store=object(),
        source_principal=object(),
        journal_store=journal,
        vault_root=tmp_path / "must-not-be-read",
    ) is None
