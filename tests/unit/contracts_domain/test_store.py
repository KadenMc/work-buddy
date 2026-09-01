from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from work_buddy.contracts_domain.migrations import CONTRACT_MIGRATIONS
from work_buddy.contracts_domain.service import (
    ContractConflict,
    ContractService,
    ContractValidationError,
    IdempotencyConflict,
    WipLimitExceeded,
)
from work_buddy.contracts_domain.store import ContractStore, ContractStoreError


def _store(tmp_path) -> ContractStore:
    return ContractStore.create(tmp_path / "contracts.db")


def _payload(title: str = "Paper one", *, status: str = "draft") -> dict:
    return {
        "title": title,
        "status": status,
        "type": "paper",
        "privacy_class": "sensitive",
        "estimated_progress": 25,
        "aliases": [
            {"alias": f"{title.casefold().replace(' ', '-')}.md", "kind": "legacy_path"},
            {"alias": title.casefold().replace(" ", "-"), "kind": "logical_name"},
        ],
        "dates": {"deadline": "2026-10-01", "last_reviewed": "2026-08-27"},
        "commitments": [{"kind": "claim", "text": "Ship a defensible result"}],
        "constraints": [{"kind": "current", "text": "Recruit participants"}],
        "health_inputs": {"deadline_type": "external", "risk": "medium"},
        "participants": [{"entity_ref": "person:test-user", "role": "owner"}],
        "evidence_links": [
            {
                "evidence_ref": "artifact://analysis",
                "label": "Analysis complete",
                "requirement": "must_have",
                "state": "open",
            }
        ],
        "body_roles": [
            {
                "role": "brief",
                "mode": "plain",
                "plain_body": "The bounded contract brief.",
                "interaction_contract_id": "human_value",
                "interaction_contract_version": 1,
                "privacy_class": "sensitive",
            }
        ],
    }


def test_schema_and_structured_contract_round_trip(tmp_path):
    store = _store(tmp_path)
    service = ContractService(store)

    created = service.create(
        _payload(), actor="user:test-user", intent_id="create-paper-one"
    )

    assert len(created["contract_id"]) == 32
    assert service.get("paper-one.md") == created
    assert service.get("PAPER-ONE") == created
    assert created["dates"]["deadline"]["value"] == "2026-10-01"
    assert created["commitments"][0]["text"] == "Ship a defensible result"
    assert created["constraints"][0]["text"] == "Recruit participants"
    assert created["participants"][0]["entity_ref"] == "person:test-user"
    assert created["evidence_links"][0]["evidence_ref"] == "artifact://analysis"
    assert created["body_roles"][0]["plain_body"] == "The bounded contract brief."
    assert service.pending_search_events()[0]["privacy_class"] == "sensitive"

    with store.read_transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            CONTRACT_MIGRATIONS.target_version
        )
        assert connection.execute("SELECT COUNT(*) FROM contract_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM contract_search_outbox").fetchone()[0] == 1
    store.validate()


def test_cas_idempotency_revisions_tombstone_and_outbox_are_atomic(tmp_path):
    service = ContractService(_store(tmp_path))
    payload = _payload()
    created = service.create(payload, actor="user:test-user", intent_id="create-1")
    assert service.create(payload, actor="user:test-user", intent_id="create-1") == created

    with pytest.raises(IdempotencyConflict):
        service.create(
            {**payload, "title": "A different request"},
            actor="user:test-user",
            intent_id="create-1",
        )

    updated = service.update(
        created["contract_id"],
        {"estimated_progress": 50},
        expected_revision=1,
        actor="user:test-user",
        intent_id="update-1",
    )
    assert updated["current_revision"] == 2
    assert updated["estimated_progress"] == 50
    assert service.update(
        created["contract_id"],
        {"estimated_progress": 50},
        expected_revision=1,
        actor="user:test-user",
        intent_id="update-1",
    ) == updated

    with pytest.raises(ContractConflict):
        service.update(
            created["contract_id"],
            {"estimated_progress": 75},
            expected_revision=1,
            actor="user:test-user",
            intent_id="stale-update",
        )

    tombstoned = service.tombstone(
        created["contract_id"],
        expected_revision=2,
        actor="user:test-user",
        intent_id="delete-1",
    )
    assert tombstoned["lifecycle"] == "tombstoned"
    assert service.get(created["contract_id"]) is None
    assert service.get(created["contract_id"], include_tombstoned=True) == tombstoned
    events = service.pending_search_events()
    assert [row["event_kind"] for row in events] == [
        "upsert",
        "upsert",
        "delete",
    ]
    assert service.mark_search_event_delivered(
        events[0]["event_id"], expected_content_sha256=events[0]["content_sha256"]
    )
    assert not service.mark_search_event_delivered(
        events[0]["event_id"], expected_content_sha256=events[0]["content_sha256"]
    )
    assert len(service.pending_search_events(limit=2)) == 2
    with pytest.raises(ContractConflict, match="content changed"):
        service.mark_search_event_delivered(
            events[1]["event_id"], expected_content_sha256="0" * 64
        )
    with pytest.raises(ContractConflict, match="tombstoned"):
        service.update(
            created["contract_id"],
            {"title": "Cannot resurrect"},
            expected_revision=3,
            actor="user:test-user",
            intent_id="resurrection",
        )


def test_concurrent_cas_allows_exactly_one_writer(tmp_path):
    service = ContractService(_store(tmp_path))
    created = service.create(
        _payload(), actor="user:test-user", intent_id="create-before-race"
    )
    barrier = Barrier(2)

    def update(index: int) -> str:
        barrier.wait(timeout=5)
        try:
            service.update(
                created["contract_id"],
                {"estimated_progress": 50 + index},
                expected_revision=1,
                actor=f"agent:{index}",
                intent_id=f"race-{index}",
            )
            return "committed"
        except ContractConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(update, (0, 1)))
    assert outcomes == ["committed", "conflict"]
    assert service.get(created["contract_id"])["current_revision"] == 2
    assert len(service.pending_search_events()) == 2


def test_wip_limit_is_enforced_on_activation(tmp_path):
    service = ContractService(_store(tmp_path))
    created = []
    for index in range(3):
        created.append(
            service.create(
                _payload(f"Active {index}", status="active"),
                actor="user:test-user",
                intent_id=f"active-{index}",
            )
        )
    assert service.wip_status() == {
        "within_limit": True,
        "active_count": 3,
        "limit": 3,
        "active_titles": ["Active 0", "Active 1", "Active 2"],
    }
    with pytest.raises(WipLimitExceeded):
        service.create(
            _payload("Active 3", status="active"),
            actor="user:test-user",
            intent_id="active-3",
        )
    assert service.wip_status()["active_count"] == 3

    archived = service.update(
        created[0]["contract_id"],
        {"lifecycle": "archived"},
        expected_revision=1,
        actor="user:test-user",
        intent_id="archive-active-0",
    )
    replacement = service.create(
        _payload("Active replacement", status="active"),
        actor="user:test-user",
        intent_id="active-replacement",
    )
    assert replacement["status"] == "active"
    with pytest.raises(WipLimitExceeded):
        service.update(
            archived["contract_id"],
            {"lifecycle": "current"},
            expected_revision=2,
            actor="user:test-user",
            intent_id="restore-active-0",
        )


def test_body_role_is_plain_or_document_never_both(tmp_path):
    store = _store(tmp_path)
    service = ContractService(store)
    invalid = _payload()
    invalid["body_roles"] = [
        {
            "role": "brief",
            "mode": "document",
            "plain_body": "must not coexist",
            "binding": {"store_id": "store-1", "document_id": "doc-1"},
        }
    ]
    with pytest.raises(ContractValidationError):
        service.create(invalid, actor="user:test-user", intent_id="invalid-body")

    created = service.create(
        _payload(), actor="user:test-user", intent_id="valid-plain-body"
    )
    binding_id = "b" * 32
    with pytest.raises(sqlite3.IntegrityError):
        with store.write_transaction() as connection:
            connection.execute(
                "INSERT INTO contract_document_bindings "
                "(binding_id,contract_id,body_role,store_id,document_id,"
                "interaction_contract_id,interaction_contract_version,lifecycle,"
                "authority_epoch,created_at) VALUES (?,?,?,?,?,?,?,'current',?,?)",
                (
                    binding_id,
                    created["contract_id"],
                    "brief",
                    "store-1",
                    "doc-1",
                    "human_value",
                    1,
                    1,
                    created["updated_at"],
                ),
            )
            connection.execute(
                "UPDATE contract_body_roles SET current_document_binding_id=? "
                "WHERE contract_id=? AND body_role='brief'",
                (binding_id, created["contract_id"]),
            )


def test_explicit_document_binding_is_the_only_body_authority(tmp_path):
    store = _store(tmp_path)
    service = ContractService(store)
    payload = _payload("Document contract")
    payload["body_roles"] = [
        {
            "role": "brief",
            "mode": "document",
            "binding": {
                "store_id": "truth-store-1",
                "document_id": "cowork-document-1",
                "authority_epoch": 4,
            },
            "interaction_contract_id": "provenance_document",
            "interaction_contract_version": 2,
            "privacy_class": "private",
        }
    ]

    created = service.create(
        payload, actor="user:test-user", intent_id="create-document-contract"
    )
    role = created["body_roles"][0]
    assert role["mode"] == "document"
    assert role["plain_body"] is None
    assert role["binding"]["store_id"] == "truth-store-1"
    assert len(role["binding"]["binding_id"]) == 32
    with store.read_transaction() as connection:
        persisted = connection.execute(
            "SELECT b.lifecycle,b.authority_epoch,br.plain_body "
            "FROM contract_body_roles br JOIN contract_document_bindings b "
            "ON b.binding_id=br.current_document_binding_id "
            "WHERE br.contract_id=? AND br.body_role='brief'",
            (created["contract_id"],),
        ).fetchone()
    assert dict(persisted) == {
        "lifecycle": "current",
        "authority_epoch": 4,
        "plain_body": None,
    }
    with pytest.raises(ContractValidationError, match="authority conversion"):
        service.update(
            created["contract_id"],
            {
                "body_roles": [
                    {
                        "role": "brief",
                        "mode": "plain",
                        "plain_body": "destructive downgrade",
                        "interaction_contract_id": "provenance_document",
                        "interaction_contract_version": 2,
                    }
                ]
            },
            expected_revision=1,
            actor="user:test-user",
            intent_id="document-to-plain",
        )
    with pytest.raises(ContractConflict, match="dedicated coordinator"):
        service.tombstone(
            created["contract_id"],
            expected_revision=1,
            actor="user:test-user",
            intent_id="unsafe-document-tombstone",
        )
    store.validate()


def test_plain_body_edit_advances_body_revision_without_changing_authority(tmp_path):
    service = ContractService(_store(tmp_path))
    created = service.create(
        _payload(), actor="user:test-user", intent_id="create-before-body-edit"
    )
    updated = service.update(
        created["contract_id"],
        {
            "body_roles": [
                {
                    "role": "brief",
                    "mode": "plain",
                    "plain_body": "Revised bounded brief.",
                    "interaction_contract_id": "human_value",
                    "interaction_contract_version": 1,
                    "privacy_class": "sensitive",
                }
            ]
        },
        expected_revision=1,
        actor="user:test-user",
        intent_id="edit-plain-body",
    )
    assert updated["body_roles"][0]["plain_body"] == "Revised bounded brief."
    assert updated["body_roles"][0]["body_revision"] == 2


def test_restore_validation_rejects_unsealed_native_authority(tmp_path):
    store = _store(tmp_path)
    with store.write_transaction() as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE contract_authority SET state='native', sealed_cohort_id='missing',"
            "coordinator_decision_id='decision',coordinator_decision_sha256=?,sealed_at=? "
            "WHERE singleton=1",
            ("0" * 64, "2026-08-27T00:00:00+00:00"),
        )
        connection.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ContractStoreError, match="matching sealed cohort"):
        store.validate()


def test_dashboard_contract_summary_uses_native_authority_without_archive_read(
    tmp_path, monkeypatch
):
    from work_buddy.dashboard import api as dashboard_api

    store = _store(tmp_path)
    ContractService(store).create(
        _payload("Native contract", status="active"),
        actor="user:test-user",
        intent_id="create-native-dashboard-contract",
    )
    with store.write_transaction() as connection:
        connection.execute(
            "UPDATE contract_authority SET state='native',"
            "sealed_cohort_id='dashboard-native-cohort',"
            "coordinator_decision_id='dashboard-native-decision',"
            "coordinator_decision_sha256=?,sealed_at=? WHERE singleton=1",
            ("d" * 64, "2026-08-27T00:00:00+00:00"),
        )

    archive = tmp_path / "vault" / "legacy-contracts"
    archive.mkdir(parents=True)
    stale = archive / "stale.md"
    stale.write_text(
        "---\ntitle: Stale archive contract\nstatus: active\n---\n",
        encoding="utf-8",
    )
    original_read_text = type(stale).read_text

    def reject_archive_read(path, *args, **kwargs):
        if path.resolve() == stale.resolve():
            raise AssertionError("retired Contracts Markdown was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(stale), "read_text", reject_archive_read)
    monkeypatch.setattr(
        "work_buddy.contracts_domain.provider.default_db_path",
        lambda: store.path,
    )
    monkeypatch.setattr(
        dashboard_api,
        "load_config",
        lambda: {
            "vault_root": str(tmp_path / "vault"),
            "contracts": {"vault_path": "legacy-contracts"},
        },
    )

    result = dashboard_api.get_contracts_summary()

    assert result == {
        "contracts": [
            {
                "file": "native-contract.md",
                "title": "Native contract",
                "status": "active",
                "type": "paper",
                "deadline": "2026-10-01",
                "priority": "",
                "vault_path": None,
            }
        ]
    }
