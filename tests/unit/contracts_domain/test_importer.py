from __future__ import annotations

import hashlib

import pytest

from work_buddy import contracts
from work_buddy.contracts_domain.importer import (
    ContractImportError,
    ContractImporter,
)
from work_buddy.contracts_domain.service import ContractService
from work_buddy.contracts_domain.store import ContractStore
from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    authorize_isolated_rehearsal_root,
)
from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext


def _context() -> TrustedIngressContext:
    tenant = "tenant-contract-import-test"
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


def _importer(tmp_path, store, *, source_committed=None):
    sources = SourceStore.create(tmp_path / "sources")
    return (
        ContractImporter(store, sources, source_committed=source_committed),
        sources,
        _context(),
    )


def _rehearsal(store):
    return authorize_isolated_rehearsal_root(
        store.path.parent,
        authority_paths={"contracts": store.path},
    )


def _write_contract(
    root,
    name: str,
    *,
    title: str = "Alpha paper",
    status: str = "active",
    deadline: str = "2026-10-01",
    privacy: str = "sensitive",
) -> bytes:
    data = (
        "---\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "type: paper\n"
        f"deadline: {deadline}\n"
        "last_reviewed: 2026-08-27\n"
        "estimated_progress: 40\n"
        f"privacy_class: {privacy}\n"
        "participants:\n"
        "  - Alex\n"
        "deadline_type: external\n"
        "---\n"
        "# Claim\nShip the analysis.\n\n"
        "# Why it matters\nThe decision depends on it.\n\n"
        "# Current Constraint\nRecruitment.\n\n"
        "# Must-have evidence\n- [ ] Analysis complete\n\n"
        "# Optional / nice-to-have\n- [x] Extra chart\n\n"
        "# Kill rule\nStop after the deadline.\n"
    ).encode("utf-8")
    (root / name).write_bytes(data)
    return data


def _stage_fixture(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    valid = _write_contract(source, "alpha.md")
    _write_contract(source, "bad-status.md", title="Bad status", status="in progress")
    _write_contract(source, "bad-date.md", title="Bad date", deadline="08/99/2026")
    _write_contract(source, "bad-privacy.md", title="Bad privacy", privacy="public")
    (source / "_template.md").write_text("template placeholder", encoding="utf-8")
    store = ContractStore.create(tmp_path / "contracts.db")
    importer, sources, context = _importer(tmp_path, store)
    return source, valid, store, importer, sources, context


def test_import_stages_frozen_inputs_invisibly_and_quarantines_ambiguity(tmp_path):
    source, valid, store, importer, sources, context = _stage_fixture(tmp_path)

    result = importer.stage(
        source, actor="user:test-user", intent_id="stage-legacy-contracts",
        ingress_context=context,
    )
    replay = importer.stage(
        source, actor="user:test-user", intent_id="stage-legacy-contracts"
    )

    assert replay == result
    assert result["item_count"] == 5
    assert result["accepted_count"] == 1
    assert result["quarantined_count"] == 3
    assert result["ignored_count"] == 1
    assert ContractService(store).list() == []
    quarantine = importer.quarantine(result["cohort_id"])
    assert [item["source_key"] for item in quarantine] == [
        "bad-date.md",
        "bad-privacy.md",
        "bad-status.md",
    ]

    with store.read_transaction() as connection:
        row = connection.execute(
            "SELECT frozen_bytes,source_sha256,disposition FROM contract_import_inventory "
            "WHERE cohort_id=? AND source_key='alpha.md'",
            (result["cohort_id"],),
        ).fetchone()
        assert bytes(row["frozen_bytes"]) == valid
        assert row["source_sha256"] == hashlib.sha256(valid).hexdigest()
        assert row["disposition"] == "accepted"
        assert connection.execute(
            "SELECT COUNT(*) FROM contract_import_stage WHERE cohort_id=?",
            (result["cohort_id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM contract_import_source_dependencies "
            "WHERE source_usage_state='acknowledged'",
        ).fetchone()[0] == 5
    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE status='acknowledged'"
        ).fetchone()[0] == 5

    _write_contract(source, "alpha.md", title="Changed after freeze")
    assert importer.stage(
        source, actor="user:test-user", intent_id="stage-legacy-contracts"
    ) == result


def test_seal_atomically_publishes_frozen_records_and_legacy_aliases(tmp_path):
    source, _, store, importer, _sources, context = _stage_fixture(tmp_path)
    staged = importer.stage(
        source, actor="user:test-user", intent_id="stage-1", ingress_context=context
    )

    # Publication consumes only the hash-frozen stage. The source can disappear
    # after inventory without changing the reviewed cohort.
    (source / "alpha.md").unlink()
    with pytest.raises(ContractImportError, match="requires preseal maintenance"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256=staged["inventory_sha256"],
            actor="user:test-user",
            intent_id="seal-default-must-fail",
        )
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256=staged["inventory_sha256"],
            actor="user:test-user",
            intent_id="seal-rehearsal-capability-required",
            allow_unfenced_rehearsal=True,
        )
    sealed = importer.seal(
        staged["cohort_id"],
        expected_inventory_sha256=staged["inventory_sha256"],
        actor="user:test-user",
        intent_id="seal-1",
        coordinator_decision_id="decision:contracts-cutover",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )
    replay = importer.seal(
        staged["cohort_id"],
        expected_inventory_sha256=staged["inventory_sha256"],
        actor="user:test-user",
        intent_id="seal-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
        coordinator_decision_id="decision:contracts-cutover",
    )

    assert replay == sealed
    assert sealed["authority"] == "native"
    assert store.authority()["sealed_cohort_id"] == staged["cohort_id"]
    service = ContractService(store)
    assert service.get("alpha.md")["title"] == "Alpha paper"
    assert service.get("alpha")["title"] == "Alpha paper"
    assert service.get("alpha.md")["body_roles"][0]["plain_body"].startswith("# Claim")
    assert service.get("alpha.md")["commitments"][1]["kind"] == "why_it_matters"
    assert service.pending_search_events()[0]["privacy_class"] == "sensitive"
    with store.read_transaction() as connection:
        source_ref = connection.execute(
            "SELECT source_ref FROM contract_revisions WHERE operation='legacy_import'"
        ).fetchone()[0]
        assert source_ref.startswith("wb-source://")
    store.validate()

    # Exact stage replay is served from its receipt without touching the now
    # incomplete archive. A new post-seal import is fenced before file reads.
    assert importer.stage(
        source, actor="user:test-user", intent_id="stage-1"
    ) == staged
    with pytest.raises(ContractImportError, match="already sealed"):
        importer.stage(source, actor="user:test-user", intent_id="stage-after-seal")

    with pytest.raises(ContractImportError, match="decision cannot be changed"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256=staged["inventory_sha256"],
            actor="user:test-user",
            intent_id="seal-conflicting-decision",
            allow_unfenced_rehearsal=True,
            rehearsal_authorization=_rehearsal(store),
            coordinator_decision_id="decision:replacement",
        )


def test_failed_seal_leaves_cohort_invisible_and_authority_legacy(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_contract(source, "alpha.md")
    _write_contract(source, "zeta.md", title="Zeta paper")
    store = ContractStore.create(tmp_path / "contracts.db")
    service = ContractService(store)
    importer, _sources, context = _importer(tmp_path, store)
    staged = importer.stage(
        source, actor="user:test-user", intent_id="stage-1", ingress_context=context
    )
    service.create(
        {
            "title": "Existing",
            "status": "draft",
            "type": "other",
            "aliases": [{"alias": "zeta.md", "kind": "legacy_path"}],
        },
        actor="user:test-user",
        intent_id="existing-contract",
    )

    with pytest.raises(ContractImportError, match="conflicts"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256=staged["inventory_sha256"],
            actor="user:test-user",
            intent_id="seal-conflict",
            allow_unfenced_rehearsal=True,
            rehearsal_authorization=_rehearsal(store),
        )

    assert store.authority()["state"] == "legacy"
    assert [item["title"] for item in service.list()] == ["Existing"]
    assert service.get("alpha.md") is None
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT state FROM contract_import_cohorts WHERE cohort_id=?",
            (staged["cohort_id"],),
        ).fetchone()[0] == "staged"
        assert connection.execute("SELECT COUNT(*) FROM contract_import_seals").fetchone()[0] == 0


def test_seal_requires_the_reviewed_inventory_digest(tmp_path):
    source, _, store, importer, _sources, context = _stage_fixture(tmp_path)
    staged = importer.stage(
        source, actor="user:test-user", intent_id="stage-1", ingress_context=context
    )
    with pytest.raises(ContractImportError, match="inventory changed"):
        importer.seal(
            staged["cohort_id"],
            expected_inventory_sha256="0" * 64,
            actor="user:test-user",
            intent_id="seal-wrong-inventory",
            allow_unfenced_rehearsal=True,
            rehearsal_authorization=_rehearsal(store),
        )
    assert store.authority()["state"] == "legacy"
    assert ContractService(store).list() == []


def test_existing_contract_queries_never_read_markdown_after_seal(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_contract(source, "alpha.md")
    store = ContractStore.create(tmp_path / "contracts.db")
    importer, _sources, context = _importer(tmp_path, store)
    staged = importer.stage(
        source, actor="user:test-user", intent_id="stage-1", ingress_context=context
    )
    importer.seal(
        staged["cohort_id"],
        expected_inventory_sha256=staged["inventory_sha256"],
        actor="user:test-user",
        intent_id="seal-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(store),
    )
    ContractService(store).create(
        {
            "title": "Archived active label",
            "status": "active",
            "type": "other",
            "lifecycle": "archived",
            "aliases": [{"alias": "archived.md", "kind": "legacy_path"}],
        },
        actor="user:test-user",
        intent_id="native-write-after-seal",
    )

    from work_buddy.contracts_domain import provider

    monkeypatch.setattr(provider, "default_db_path", lambda: store.path)
    monkeypatch.setattr(
        contracts,
        "parse_frontmatter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sealed authority read Markdown")
        ),
    )
    monkeypatch.setattr(
        contracts,
        "_contracts_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sealed authority touched the Markdown directory")
        ),
    )

    with pytest.raises(contracts.ContractAuthorityError, match="SQLite authority"):
        contracts.get_contracts_dir()
    assert contracts.load_contract(source / "alpha.md")["title"] == "Alpha paper"
    assert [item["title"] for item in contracts.load_all_contracts(source)] == [
        "Alpha paper",
        "Archived active label",
    ]
    assert [item["title"] for item in contracts.active_contracts(source)] == [
        "Alpha paper"
    ]
    assert contracts.check_wip_limit(source) == {
        "within_limit": True,
        "active_count": 1,
        "limit": 3,
        "active_titles": ["Alpha paper"],
    }
    assert "active: 1" in contracts.contract_health_check(source)


def test_unsealed_authority_keeps_legacy_read_only_fallback(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_contract(source, "alpha.md")
    store = ContractStore.create(tmp_path / "contracts.db")
    from work_buddy.contracts_domain import provider

    monkeypatch.setattr(provider, "default_db_path", lambda: store.path)
    assert store.authority()["state"] == "legacy"
    assert [item["title"] for item in contracts.load_all_contracts(source)] == [
        "Alpha paper"
    ]


def test_importer_accepts_utf8_bom_and_crlf_without_changing_frozen_bytes(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    raw = _write_contract(source, "bom.md")
    frozen = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
    (source / "bom.md").write_bytes(frozen)
    store = ContractStore.create(tmp_path / "contracts.db")
    importer, _sources, context = _importer(tmp_path, store)

    staged = importer.stage(
        source, actor="user:test-user", intent_id="stage-bom", ingress_context=context
    )
    assert staged["accepted_count"] == 1
    with store.read_transaction() as connection:
        stored = connection.execute(
            "SELECT frozen_bytes FROM contract_import_inventory WHERE cohort_id=?",
            (staged["cohort_id"],),
        ).fetchone()[0]
    assert bytes(stored) == frozen


def test_source_commit_crash_replays_without_duplicate_sources(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_contract(source, "alpha.md")
    store = ContractStore.create(tmp_path / "contracts.db")
    sources = SourceStore.create(tmp_path / "sources")
    context = _context()
    crashed = False

    def crash_once(_cohort_id: str, _source_key: str) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated source commit crash")

    importer = ContractImporter(store, sources, source_committed=crash_once)
    with pytest.raises(RuntimeError, match="simulated source commit crash"):
        importer.stage(
            source,
            actor="user:test-user",
            intent_id="crash-stage",
            ingress_context=context,
        )
    assert ContractService(store).list() == []

    replay = ContractImporter(store, sources).stage(
        source,
        actor="user:test-user",
        intent_id="crash-stage",
        ingress_context=context,
    )
    verification = ContractImporter(store, sources).verify(replay["cohort_id"])
    assert (
        verification["source_count"]
        == verification["source_acknowledged_count"]
        == 1
    )
    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_usage_intents"
        ).fetchone()[0] == 1
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM contract_import_inventory"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT source_usage_state FROM contract_import_source_dependencies"
        ).fetchone()[0] == "acknowledged"


def test_stage_refuses_missing_sources_authority(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_contract(source, "alpha.md")
    store = ContractStore.create(tmp_path / "contracts.db")

    with pytest.raises(ContractImportError, match="Sources authority"):
        ContractImporter(store).stage(
            source,
            actor="user:test-user",
            intent_id="missing-sources",
            ingress_context=_context(),
        )
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM contract_import_cohorts"
        ).fetchone()[0] == 0
