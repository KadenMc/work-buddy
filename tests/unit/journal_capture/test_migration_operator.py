from __future__ import annotations

import sqlite3

import pytest

from work_buddy.consent import ConsentRequired, get_consent_metadata
from work_buddy.knowledge.capability_loader import load_declared_capabilities
from work_buddy.knowledge.store import load_store
from work_buddy.mcp_server import op_registry
from work_buddy.mcp_server.ops import journal_migration_ops
from work_buddy.journal_capture.authority import JournalAuthorityStateError


MUTATIONS = (
    "select",
    "shadow_import",
    "cutover",
    "rollback",
    "reconcile",
    "certify_exit",
)


def test_operator_is_retired_from_discovery_but_warm_op_remains_fenced() -> None:
    op_registry.clear_ops()
    op_registry.load_builtin_ops()
    store = load_store()
    capabilities, issues = load_declared_capabilities(store)
    assert not [
        issue
        for issue in issues
        if issue["path"] == "journal/journal-content-migration-operator"
    ]
    assert not [
        item
        for item in capabilities
        if item.name == "journal_content_migration_operator"
    ]
    # A process that loaded the old declaration may still hold the raw Op.
    # The runtime authority fence is therefore retained as defense in depth.
    assert op_registry.get_op("op.wb.journal_content_migration_operator") is (
        journal_migration_ops.journal_content_migration_operator
    )


def test_every_mutation_has_high_weight_zero_ttl_consent() -> None:
    for action in MUTATIONS:
        metadata = get_consent_metadata(f"journal.content_migration.{action}")
        assert metadata is not None
        assert metadata["risk"] == "high"
        assert metadata["consent_weight"] == "high"
        assert metadata["default_ttl"] == 0


def test_inventory_does_not_create_identity_sources_or_journal_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    journal_db = tmp_path / "state" / "journal.db"
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "vault_root": str(vault),
            "journal": {
                "content_migration": {"enabled": False, "cutover_enabled": False}
            },
        },
    )
    monkeypatch.setattr(
        "work_buddy.paths.resolve",
        lambda _key: journal_db,
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: (_ for _ in ()).throw(AssertionError("inventory requested identity")),
    )
    monkeypatch.setattr(
        "work_buddy.sources.SourceStore.create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory created Sources state")
        ),
    )

    result = journal_migration_ops.journal_content_migration_operator(
        action="inventory"
    )

    assert result["schema"] == "wb.journal-content-inventory/v1"
    assert result["cutoverGate"] == "closed"
    assert not journal_db.exists()
    assert not journal_db.parent.exists()


@pytest.mark.parametrize(
    ("action", "values"),
    (
        ("select", {"entity_kind": "logical_day_log", "day_id": "2026-08-10"}),
        ("shadow_import", {"entity_kind": "logical_day_log", "entity_id": "2026-08-10"}),
        (
            "cutover",
            {
                "entity_kind": "logical_day_log",
                "entity_id": "2026-08-10",
                "rollback_deadline": "2099-01-01T00:00:00+00:00",
            },
        ),
        ("rollback", {"entity_kind": "logical_day_log", "entity_id": "2026-08-10"}),
        ("reconcile", {}),
        ("certify_exit", {}),
    ),
)
def test_consent_denial_precedes_configuration_and_store_construction(
    action: str,
    values: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_configuration = False

    def forbidden_configuration():
        nonlocal reached_configuration
        reached_configuration = True
        raise AssertionError("configuration must not be read before consent")

    monkeypatch.setattr(
        "work_buddy.consent._cache.is_granted", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr("work_buddy.config.load_config", forbidden_configuration)

    with pytest.raises(ConsentRequired) as raised:
        journal_migration_ops.journal_content_migration_operator(
            action=action, **values
        )
    assert raised.value.operation == f"journal.content_migration.{action}"
    assert reached_configuration is False


def test_database_authority_seal_fences_retired_content_migration_operator(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    journal_db = tmp_path / "journal.db"
    with sqlite3.connect(journal_db) as conn:
        conn.execute(
            "CREATE TABLE journal_authority_control("
            "singleton INTEGER PRIMARY KEY,mode TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO journal_authority_control(singleton,mode) "
            "VALUES(1,'database_only')"
        )
    monkeypatch.setattr(journal_migration_ops, "_authorize_select", lambda: None)
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "vault_root": str(vault),
            "journal": {
                "content_migration": {"enabled": True, "cutover_enabled": True}
            },
        },
    )
    monkeypatch.setattr("work_buddy.paths.resolve", lambda _key: journal_db)
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: (_ for _ in ()).throw(AssertionError("identity opened after seal")),
    )

    with pytest.raises(JournalAuthorityStateError, match="Markdown writes are fenced"):
        journal_migration_ops.journal_content_migration_operator(
            action="select",
            entity_kind="logical_day_log",
            day_id="2026-08-20",
        )
