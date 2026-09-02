from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.backups.sensitive import (
    create_sensitive_checkpoint,
    rehearse_sensitive_checkpoint_restore,
)
from work_buddy.contracts_domain.importer import ContractImporter
from work_buddy.contracts_domain.store import ContractStore
from work_buddy.journal_capture.import_cohort import (
    JournalImportTarget,
    LegacyJournalImportMapping,
    LegacyJournalImportService,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.knowledge.personal.importer import PersonalKnowledgeImportCoordinator
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore
from work_buddy.projects import store as project_store
from work_buddy.projects.authority import ProjectImportCoordinator
from work_buddy.sources import (
    ActorRef,
    CUTOVER_IMPORT_PURPOSES,
    CutoverSourceAuthorization,
    ExactImportSourceService,
    ImportAuthorization,
    InvalidSourceRequest,
    SourceRef,
    SourceStore,
    import_sources,
    verify_cutover_source_dependencies,
)


def _authorization() -> CutoverSourceAuthorization:
    tenant = "tenant-cutover-test"
    authority = "cutover-test-authority"
    return CutoverSourceAuthorization(
        issuer=ActorRef(authority, "cutover-ingress", "service", tenant),
        inputter=ActorRef(authority, "legacy-owner", "human", tenant),
        principal=ActorRef(authority, "cutover-operator", "service", tenant),
        tenant_scope_id=tenant,
        authorization_fingerprint="d" * 64,
    )


def _journal_mapping() -> LegacyJournalImportMapping:
    return LegacyJournalImportMapping(
        mapping_version="test-cutover-mapping/v1",
        targets={
            "log_section": JournalImportTarget(
                item_kind="record",
                module_instance_id="simple.stream",
                module_instance_version=1,
            ),
        },
    )


def _write_personal_note(root: Path) -> None:
    (root / "focus.md").write_text(
        "---\n"
        "name: Focus\n"
        "description: Protect focus.\n"
        "category: work_pattern\n"
        "last_observed: '2026-08-20'\n"
        "observation_count: 1\n"
        "---\n\n"
        "# Focus\n\n"
        "## Definition\n\nProtect focus.\n\n"
        "## Evidence\n\n* 2026-08-20 - Worked.\n",
        encoding="utf-8",
    )


def _write_contract(root: Path) -> None:
    (root / "alpha.md").write_text(
        "---\n"
        "title: Alpha\n"
        "status: active\n"
        "type: paper\n"
        "deadline: 2026-10-01\n"
        "last_reviewed: 2026-08-27\n"
        "estimated_progress: 40\n"
        "privacy_class: sensitive\n"
        "participants: [owner]\n"
        "deadline_type: external\n"
        "---\n"
        "# Claim\nShip.\n"
        "# Why it matters\nRequired.\n"
        "# Current Constraint\nTime.\n"
        "# Must-have evidence\n- [ ] Complete\n"
        "# Optional / nice-to-have\n- [ ] Extra\n"
        "# Kill rule\nStop after deadline.\n",
        encoding="utf-8",
    )


def test_cutover_contexts_share_identity_but_only_grant_domain_and_export():
    authorization = _authorization()

    contexts = {
        domain: authorization.ingress_context(domain)
        for domain in CUTOVER_IMPORT_PURPOSES
    }

    assert {context.service_principal for context in contexts.values()} == {
        authorization.principal
    }
    assert {context.authorization_fingerprint for context in contexts.values()} == {
        authorization.authorization_fingerprint
    }
    for domain, context in contexts.items():
        assert context.permitted_purposes == (
            CUTOVER_IMPORT_PURPOSES[domain],
            "export",
        )
    with pytest.raises(InvalidSourceRequest):
        authorization.ingress_context("unsupported")

    restore = authorization.restore_authorization()
    merge = authorization.merge_authorization()
    assert restore.principal == merge.principal == authorization.principal
    assert (
        restore.authorization_fingerprint
        == merge.authorization_fingerprint
        == authorization.authorization_fingerprint
    )
    assert restore.collision_policy == merge.collision_policy == "reject"
    assert restore.restore_operational_state is True
    assert merge.restore_operational_state is False
    assert restore.merge_operational_state is False
    assert merge.merge_operational_state is True


def test_all_four_staged_domains_export_and_restore_under_one_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authorization = _authorization()
    sources = SourceStore.create(tmp_path / "sources", authority_id="cutover-sources")

    journal_root = tmp_path / "journal-legacy"
    journal_root.mkdir()
    (journal_root / "2026-08-20.md").write_text(
        "# **Log**\n* 9:00 AM - staged history\n",
        encoding="utf-8",
    )
    journal_store = JournalCaptureStore(tmp_path / "journal.db")
    journal = LegacyJournalImportService(journal_store, sources)
    journal_cohort = journal.prepare(
        journal_root,
        mapping=_journal_mapping(),
        client_mutation_id="cutover-journal-prepare-0001",
        actor={"kind": "migration_operator", "id": "test"},
    )
    journal.stage(
        journal_cohort.cohort_id,
        journal_root,
        ingress_context=authorization.ingress_context("journal"),
    )

    projects_root = tmp_path / "projects-legacy"
    projects_root.mkdir()
    (projects_root / "alpha.md").write_text(
        "---\nslug: alpha\nname: Alpha\nstatus: active\n---\n# Alpha\n\nHistory.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(project_store, "_db_path", lambda: tmp_path / "projects.db")
    projects = ProjectImportCoordinator(sources)
    projects_prepared = projects.prepare(
        cohort_id="cutover-projects-0001",
        source_root=projects_root,
        ingress_context=authorization.ingress_context("projects"),
    )

    personal_root = tmp_path / "personal-legacy"
    personal_root.mkdir()
    _write_personal_note(personal_root)
    personal_store = PersonalKnowledgeStore(tmp_path / "personal.db")
    personal = PersonalKnowledgeImportCoordinator(
        personal_store,
        sources,
    )
    personal_prepared = personal.prepare(
        cohort_id="cutover-personal-0001",
        source_root=personal_root,
        ingress_context=authorization.ingress_context("personal_knowledge"),
    )

    contracts_root = tmp_path / "contracts-legacy"
    contracts_root.mkdir()
    _write_contract(contracts_root)
    contract_store = ContractStore.create(tmp_path / "contracts.db")
    contracts = ContractImporter(
        contract_store,
        sources,
    )
    contracts_staged = contracts.stage(
        contracts_root,
        actor="operator:test",
        intent_id="cutover-contracts-stage-0001",
        ingress_context=authorization.ingress_context("contracts"),
    )

    checkpoint = create_sensitive_checkpoint(
        tmp_path / "checkpoint",
        journal_db=journal_store.path,
        source_store=sources,
        source_authorization=authorization.export_authorization(),
        idempotency_key="cutover-post-staging-checkpoint-0001",
        created_at="2026-08-27T00:00:00+00:00",
    )
    replay = create_sensitive_checkpoint(
        tmp_path / "checkpoint",
        journal_db=journal_store.path,
        source_store=sources,
        source_authorization=authorization.export_authorization(),
        idempotency_key="cutover-post-staging-checkpoint-0001",
        created_at="ignored-on-replay",
    )
    restored = rehearse_sensitive_checkpoint_restore(
        checkpoint.path,
        tmp_path / "restored",
        source_authorization=authorization.restore_authorization(),
    )
    restored_sources = SourceStore.create(
        restored.path / "sources",
        authority_id=sources.authority_id,
    )
    parity = verify_cutover_source_dependencies(
        restored_sources,
        journal_db=restored.path / "journal_capture.db",
        journal_cohort_id=journal_cohort.cohort_id,
        projects_db=tmp_path / "projects.db",
        projects_cohort_id=projects_prepared["cohort_id"],
        personal_knowledge_db=personal_store.db_path,
        personal_knowledge_cohort_id=personal_prepared["cohort_id"],
        contracts_db=contract_store.path,
        contracts_cohort_id=contracts_staged["cohort_id"],
    )

    assert replay == checkpoint
    assert checkpoint.source_item_count == 4
    assert restored.source_item_count == 4
    assert restored.imported_source_count == 4
    assert restored.journal_source_dependency_count == 1
    assert restored.journal_source_dependency_gaps == 0
    assert parity.to_dict() == {
        "schema": "wb.cutover-source-dependency-parity/v1",
        "totalCount": 4,
        "totalGaps": 0,
        "domains": {
            "journal": {"count": 1, "gaps": 0},
            "projects": {"count": 1, "gaps": 0},
            "personalKnowledge": {"count": 1, "gaps": 0},
            "contracts": {"count": 1, "gaps": 0},
        },
    }
    with sources.connect() as connection:
        access = connection.execute(
            "SELECT purpose,COUNT(*) FROM source_access_bindings "
            "WHERE revoked_at IS NULL GROUP BY purpose ORDER BY purpose"
        ).fetchall()
        assert {str(row[0]): int(row[1]) for row in access} == {
            "contracts.history_import": 1,
            "export": 4,
            "journal.history_import": 1,
            "personal_knowledge.history_import": 1,
            "projects.history_import": 1,
        }


def test_cutover_delta_exact_merges_ingress_identity_for_deterministic_replay(
    tmp_path: Path,
):
    authorization = _authorization()
    sources = SourceStore.create(tmp_path / "sources", authority_id="cutover-sources")
    service = ExactImportSourceService(
        sources,
        purpose=CUTOVER_IMPORT_PURPOSES["journal"],
        consumer_domain="journal_import",
        use_kind="legacy_file_exact_bytes",
    )
    original = service.retain(
        exact_content=b"neutral historical bytes",
        client_mutation_id="cutover-delta-source-0001",
        consumer_id="cutover-delta-consumer-0001",
        context=authorization.ingress_context("journal"),
    )
    service.acknowledge(original.usage_id)
    source_ref = original.source_ref

    first_archive = authorization.export_after_staging(
        sources,
        tmp_path / "first.jsonl",
        source_refs=[SourceRef.parse(source_ref)],
        idempotency_key="cutover-delta-export-0001",
    )
    recovered = SourceStore.create(
        tmp_path / "recovered",
        authority_id=sources.authority_id,
    )
    first_import = import_sources(
        recovered,
        first_archive.path,
        authorization=ImportAuthorization(
            authorization.principal,
            authorization.authorization_fingerprint,
            collision_policy="reject",
        ),
    )
    assert first_import.reused_count == 0

    second_archive = authorization.export_after_staging(
        sources,
        tmp_path / "second.jsonl",
        source_refs=[SourceRef.parse(source_ref)],
        idempotency_key="cutover-delta-export-0002",
    )
    merged = import_sources(
        recovered,
        second_archive.path,
        authorization=authorization.merge_authorization(),
    )
    assert merged.reused_count == 1
    replay_service = ExactImportSourceService(
        recovered,
        purpose=CUTOVER_IMPORT_PURPOSES["journal"],
        consumer_domain="journal_import",
        use_kind="legacy_file_exact_bytes",
    )
    replay = replay_service.retain(
        exact_content=b"neutral historical bytes",
        client_mutation_id="cutover-delta-source-0001",
        consumer_id="cutover-delta-consumer-0001",
        context=authorization.ingress_context("journal"),
    )
    assert replay.source_ref == original.source_ref
    assert replay.representation_id == original.representation_id
    assert replay.submission_id == original.submission_id
    assert replay.usage_id == original.usage_id
    with recovered.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_idempotency"
        ).fetchone()[0] == 1
