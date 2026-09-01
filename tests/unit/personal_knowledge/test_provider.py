from __future__ import annotations

from pathlib import Path

from work_buddy.knowledge.model import VaultUnit


def test_unified_store_preserves_vault_unit_api(personal_store, personal_provider):
    created = personal_store.create_unit(
        logical_path="personal/preferences/answer-style",
        name="Answer style",
        description="Prefer concise answers.",
        summary="Concise answers are easier to scan.",
        body="Keep it short.",
        categories=["preference", "work_pattern"],
        aliases=["brevity"],
        tags=["communication"],
        privacy_class="restricted",
        disclosure_class="consent_required",
        idempotency_key="create-answer-style",
    )
    personal_provider.invalidate()
    import work_buddy.knowledge.store as unified

    unified._VAULT_STORE = None
    units = unified.load_store(scope="personal")
    unit = units["personal/preferences/answer-style"]
    assert isinstance(unit, VaultUnit)
    assert unit.unit_id == created["unit_id"]
    assert unit.category == "preference"
    assert unit.categories == ["preference", "work_pattern"]
    assert unit.content["full"] == "Keep it short."
    assert unit.tier("summary")["privacy_class"] == "restricted"


def test_old_logical_path_resolves_after_rename(personal_store, personal_provider):
    created = personal_store.create_unit(
        logical_path="personal/reference/old",
        name="Reference",
        body="Body",
        idempotency_key="create-old",
    )
    personal_store.update_unit(
        created["unit_id"],
        {"logical_path": "personal/reference/new"},
        expected_revision=1,
        idempotency_key="rename",
    )
    personal_provider.invalidate()
    import work_buddy.knowledge.store as unified

    unified._VAULT_STORE = None
    unit = unified.get_unit("personal/reference/old")
    assert unit is not None
    assert unit.path == "personal/reference/new"
    assert unit.unit_id == created["unit_id"]


def test_partition_uses_stable_id_and_privacy_metadata(personal_store, personal_provider):
    created = personal_store.create_unit(
        logical_path="personal/reference/indexed",
        name="Indexed",
        body="Searchable body",
        categories=["reference"],
        privacy_class="private",
        idempotency_key="create-indexed",
    )
    personal_provider.invalidate()
    from work_buddy.knowledge.partition import KnowledgePartition
    from work_buddy.knowledge.store import load_store

    partition = KnowledgePartition(store_loader=lambda: load_store(scope="personal", force=True))
    refs = list(partition.discover())
    assert refs[0].item_id == created["unit_id"]
    doc = partition.parse(created["unit_id"])[0]
    assert doc.doc_id == f"knowledge:{created['unit_id']}"
    assert doc.metadata["path"] == "personal/reference/indexed"
    assert doc.metadata["privacy_class"] == "private"
    assert doc.metadata["revision"] == 1


def test_cold_start_keeps_legacy_visible_without_creating_database(
    tmp_path: Path, monkeypatch
):
    data_root = tmp_path / "data"
    vault = tmp_path / "vault"
    legacy_root = vault / "Meta" / "WorkBuddy"
    legacy_root.mkdir(parents=True)
    (legacy_root / "preference.md").write_text(
        "---\nname: Legacy Preference\ncategory: preference\n---\n\n"
        "Prefer the legacy unit until seal.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORK_BUDDY_DATA_DIR", str(data_root))
    import work_buddy.knowledge.personal.legacy as legacy
    import work_buddy.knowledge.store as unified
    from work_buddy.knowledge.personal.provider import set_personal_knowledge_provider

    monkeypatch.setattr(
        legacy,
        "load_config",
        lambda: {
            "vault_root": str(vault),
            "personal_knowledge": {"enabled": True, "vault_path": "Meta/WorkBuddy"},
        },
    )
    set_personal_knowledge_provider(None)
    unified._VAULT_STORE = None
    db_path = data_root / "db" / "personal_knowledge.db"

    units = unified.load_store(scope="personal", force=True)

    assert units["personal/preference"].name == "Legacy Preference"
    assert not db_path.exists()
    unified._VAULT_STORE = None
    set_personal_knowledge_provider(None)


def test_seal_switches_default_provider_and_writer_without_vault_access(
    tmp_path: Path, monkeypatch
):
    data_root = tmp_path / "data"
    vault = tmp_path / "vault"
    legacy_root = vault / "Meta" / "WorkBuddy"
    legacy_root.mkdir(parents=True)
    (legacy_root / "seed.md").write_text(
        "---\nname: Seed\ncategory: preference\n---\n\nSeed body.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORK_BUDDY_DATA_DIR", str(data_root))
    import work_buddy.knowledge.personal.legacy as legacy
    import work_buddy.knowledge.store as unified
    from work_buddy.knowledge.personal.importer import PersonalKnowledgeImportCoordinator
    from work_buddy.knowledge.personal.provider import set_personal_knowledge_provider
    from work_buddy.knowledge.vault_editor import mint_personal_unit
    from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext
    from work_buddy import cutover_maintenance as maintenance

    monkeypatch.setattr(
        legacy,
        "load_config",
        lambda: {
            "vault_root": str(vault),
            "personal_knowledge": {"enabled": True, "vault_path": "Meta/WorkBuddy"},
        },
    )
    set_personal_knowledge_provider(None)
    unified._VAULT_STORE = None
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-personal-provider-cutover"
    context = TrustedIngressContext(
        issuer=ActorRef("test-authority", "personal-import", "service", tenant),
        issuer_version="test/v1",
        inputter=ActorRef("test-authority", "legacy-owner", "human", tenant),
        service_principal=ActorRef("test-authority", "personal", "service", tenant),
        tenant_scope_id=tenant,
        surface="personal-history-import",
        namespace="personal-history-import-staging",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="historical_inputter_only",
        authorization_fingerprint="d" * 64,
        permitted_purposes=("personal_knowledge.history_import",),
    )
    coordinator = PersonalKnowledgeImportCoordinator(source_store=sources)
    coordinator.prepare(
        cohort_id="cold-cutover",
        source_root=legacy_root,
        ingress_context=context,
    )
    coordinator.verify("cold-cutover")
    database = data_root / "db" / "personal_knowledge.db"
    monkeypatch.setattr(
        maintenance,
        "_configured_live_authorities",
        lambda: ((Path.cwd() / ".data").resolve(), ()),
    )
    rehearsal_authorization = maintenance.authorize_isolated_rehearsal_root(
        tmp_path,
        authority_paths={"personal_knowledge": database},
    )
    coordinator.seal(
        "cold-cutover",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=rehearsal_authorization,
    )
    retired = tmp_path / "retired"
    legacy_root.rename(retired)
    monkeypatch.setattr(
        legacy,
        "mint_legacy_personal_unit",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy write touched")),
    )

    units = unified.load_store(scope="personal", force=True)
    assert units["personal/seed"].name == "Seed"
    created = mint_personal_unit(
        name="Native after seal",
        category="preference",
        content_body="Database body.",
        idempotency_key="native-after-seal",
    )
    assert created["status"] == "created"
    assert (data_root / "db" / "personal_knowledge.db").is_file()
    unified._VAULT_STORE = None
    set_personal_knowledge_provider(None)
