from __future__ import annotations

import pytest

from work_buddy.contracts_domain.importer import ContractImportError, ContractImporter
from work_buddy.contracts_domain.service import ContractService
from work_buddy.contracts_domain.store import ContractStore
from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    CutoverMaintenanceFenced,
    authorize_isolated_rehearsal_root,
)
from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext


ACTOR = "user:test-user"
EVIDENCE = {
    "databaseCheckpoint": "1" * 64,
    "search": "2" * 64,
    "detachment": "3" * 64,
}


def _payload(title: str = "Bounded commitment") -> dict:
    return {
        "title": title,
        "status": "draft",
        "type": "other",
        "privacy_class": "private",
    }


def _context() -> TrustedIngressContext:
    tenant = "tenant-contract-maintenance-test"
    return TrustedIngressContext(
        issuer=ActorRef("test-authority", "contract-import", "service", tenant),
        issuer_version="test/v1",
        inputter=ActorRef("test-authority", "legacy-owner", "human", tenant),
        service_principal=ActorRef("test-authority", "contracts", "service", tenant),
        tenant_scope_id=tenant,
        surface="contract-history-import",
        namespace="contract-history-import-staging",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="historical_inputter_only",
        authorization_fingerprint="c" * 64,
        permitted_purposes=("contracts.history_import",),
    )


def _write_legacy_contract(root) -> None:
    root.mkdir()
    (root / "bounded.md").write_text(
        "---\n"
        "title: Bounded commitment\n"
        "status: active\n"
        "type: other\n"
        "deadline: 2026-10-01\n"
        "privacy_class: private\n"
        "participants:\n"
        "  - Alex\n"
        "---\n"
        "# Claim\nComplete the bounded work.\n",
        encoding="utf-8",
    )


def _rehearsal(store):
    return authorize_isolated_rehearsal_root(
        store.path.parent,
        authority_paths={"contracts": store.path},
    )


def test_preseal_pause_is_idempotent_tamper_evident_and_blocks_normal_writes(
    tmp_path,
):
    store = ContractStore.create(tmp_path / "contracts.db")
    service = ContractService(store)
    created = service.create(
        _payload(), actor=ACTOR, intent_id="create-before-maintenance"
    )
    sources = SourceStore.create(tmp_path / "sources")
    importer = ContractImporter(store, sources)
    scope = {
        "cohort_id": "contract-cohort-preview",
        "inventory_sha256": "a" * 64,
        "mutation_id": "contract-pause-001",
        "actor": ACTOR,
    }

    paused = importer.pause_mutations(**scope)
    assert paused == importer.pause_mutations(**scope)
    assert paused["state"] == "preseal_fenced"

    with pytest.raises(CutoverMaintenanceError, match="identity was reused"):
        importer.pause_mutations(
            **{**scope, "inventory_sha256": "b" * 64}
        )

    with pytest.raises(CutoverMaintenanceFenced):
        service.create(
            _payload("Blocked create"),
            actor=ACTOR,
            intent_id="blocked-create",
        )
    with pytest.raises(CutoverMaintenanceFenced):
        service.update(
            created["contract_id"],
            {"estimated_progress": 50},
            expected_revision=1,
            actor=ACTOR,
            intent_id="blocked-update",
        )
    with pytest.raises(CutoverMaintenanceFenced):
        service.tombstone(
            created["contract_id"],
            expected_revision=1,
            actor=ACTOR,
            intent_id="blocked-tombstone",
        )

    event = service.pending_search_events()[0]
    assert service.mark_search_event_delivered(
        event["event_id"], expected_content_sha256=event["content_sha256"]
    )
    empty_archive = tmp_path / "empty-archive"
    empty_archive.mkdir()
    staged = importer.stage(
        empty_archive,
        actor=ACTOR,
        intent_id="stage-while-fenced",
        ingress_context=_context(),
    )
    assert staged["item_count"] == 0

    resumed = importer.resume_preseal_mutations(
        cohort_id=scope["cohort_id"],
        mutation_id="contract-resume-001",
        actor=ACTOR,
    )
    assert resumed == importer.resume_preseal_mutations(
        cohort_id=scope["cohort_id"],
        mutation_id="contract-resume-001",
        actor=ACTOR,
    )
    assert resumed["state"] == "open"
    assert service.update(
        created["contract_id"],
        {"estimated_progress": 50},
        expected_revision=1,
        actor=ACTOR,
        intent_id="update-after-resume",
    )["estimated_progress"] == 50

    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cutover_maintenance_receipts"
        ).fetchone()[0] == 2


def test_retained_seal_rolls_forward_and_release_requires_exact_evidence(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_contract(archive)
    store = ContractStore.create(tmp_path / "contracts.db")
    sources = SourceStore.create(tmp_path / "sources")
    importer = ContractImporter(store, sources)
    service = ContractService(store)
    staged = importer.stage(
        archive,
        actor=ACTOR,
        intent_id="stage-retained-seal",
        ingress_context=_context(),
    )
    importer.pause_mutations(
        cohort_id=staged["cohort_id"],
        inventory_sha256=staged["inventory_sha256"],
        mutation_id="contract-pause-retained",
        actor=ACTOR,
    )

    sealed = importer.seal(
        staged["cohort_id"],
        expected_inventory_sha256=staged["inventory_sha256"],
        actor=ACTOR,
        intent_id="contract-seal-retained",
        retain_maintenance_fence=True,
    )
    assert sealed["authority"] == "native"
    assert service.get("bounded.md")["title"] == "Bounded commitment"
    with store.read_transaction() as connection:
        maintenance = connection.execute(
            "SELECT state,cohort_id,inventory_sha256 "
            "FROM cutover_maintenance WHERE singleton=1"
        ).fetchone()
        assert dict(maintenance) == {
            "state": "postseal_pending",
            "cohort_id": staged["cohort_id"],
            "inventory_sha256": staged["inventory_sha256"],
        }

    with pytest.raises(CutoverMaintenanceFenced):
        service.create(
            _payload("Blocked postseal create"),
            actor=ACTOR,
            intent_id="blocked-postseal-create",
        )
    with pytest.raises(ContractImportError, match="cannot resume"):
        importer.resume_preseal_mutations(
            cohort_id=staged["cohort_id"],
            mutation_id="contract-invalid-postseal-resume",
            actor=ACTOR,
        )

    event = service.pending_search_events()[0]
    assert service.mark_search_event_delivered(
        event["event_id"], expected_content_sha256=event["content_sha256"]
    )
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        importer.release_postseal_mutations(
            cohort_id=staged["cohort_id"],
            mutation_id="contract-release-missing-capability",
            actor=ACTOR,
            rehearsal_evidence_sha256s=EVIDENCE,
            allow_unvalidated_rehearsal=True,
        )
    with pytest.raises(CutoverMaintenanceError, match="evidence is incomplete"):
        importer.release_postseal_mutations(
            cohort_id=staged["cohort_id"],
            mutation_id="contract-release-001",
            actor=ACTOR,
            rehearsal_evidence_sha256s={
                "databaseCheckpoint": EVIDENCE["databaseCheckpoint"],
                "search": EVIDENCE["search"],
            },
            allow_unvalidated_rehearsal=True,
            rehearsal_authorization=_rehearsal(store),
        )
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT state FROM cutover_maintenance WHERE singleton=1"
        ).fetchone()[0] == "postseal_pending"
    with pytest.raises(CutoverMaintenanceFenced):
        service.update(
            service.get("bounded.md")["contract_id"],
            {"estimated_progress": 60},
            expected_revision=1,
            actor=ACTOR,
            intent_id="blocked-after-release-failure",
        )

    release_args = {
        "cohort_id": staged["cohort_id"],
        "mutation_id": "contract-release-001",
        "actor": ACTOR,
        "rehearsal_evidence_sha256s": EVIDENCE,
        "allow_unvalidated_rehearsal": True,
        "rehearsal_authorization": _rehearsal(store),
    }
    released = importer.release_postseal_mutations(**release_args)
    assert released == importer.release_postseal_mutations(**release_args)
    assert released["state"] == "open"
    assert {
        key: released["evidenceSha256s"][key] for key in EVIDENCE
    } == EVIDENCE
    assert len(released["evidenceSha256s"]["authorityHead"]) == 64

    with pytest.raises(CutoverMaintenanceError, match="identity was reused"):
        importer.release_postseal_mutations(
            **{
                **release_args,
                "rehearsal_evidence_sha256s": {
                    **EVIDENCE,
                    "search": "4" * 64,
                },
            }
        )

    imported = service.get("bounded.md")
    assert service.update(
        imported["contract_id"],
        {"estimated_progress": 60},
        expected_revision=1,
        actor=ACTOR,
        intent_id="update-after-postseal-release",
    )["estimated_progress"] == 60
    store.validate()


def test_active_preseal_fence_cannot_be_dropped_by_plain_seal(tmp_path):
    archive = tmp_path / "legacy"
    _write_legacy_contract(archive)
    store = ContractStore.create(tmp_path / "contracts.db")
    importer = ContractImporter(store, SourceStore.create(tmp_path / "sources"))
    staged = importer.stage(
        archive,
        actor=ACTOR,
        intent_id="stage-before-plain-seal",
        ingress_context=_context(),
    )
    importer.pause_mutations(
        cohort_id=staged["cohort_id"],
        inventory_sha256=staged["inventory_sha256"],
        mutation_id="contract-pause-before-plain-seal",
        actor=ACTOR,
    )

    with pytest.raises(ContractImportError, match="must remain held"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256=staged["inventory_sha256"],
            actor=ACTOR,
            intent_id="unsafe-plain-seal",
        )

    assert store.authority()["state"] == "legacy"
    assert ContractService(store).list() == []
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT state FROM cutover_maintenance WHERE singleton=1"
        ).fetchone()[0] == "preseal_fenced"
