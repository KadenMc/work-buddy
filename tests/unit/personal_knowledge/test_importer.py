from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    CutoverMaintenanceFenced,
    authorize_isolated_rehearsal_root,
)
from work_buddy.knowledge.personal.importer import (
    PersonalKnowledgeImportCoordinator,
    PersonalKnowledgeImportError,
    _inventory_digest,
    inventory_personal_markdown,
)
from work_buddy.knowledge.personal.provider import SQLitePersonalKnowledgeProvider
from work_buddy.knowledge.personal.service import PersonalKnowledgeService
from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext


def _write_note(root: Path, relative: str = "work_patterns/focus.md") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "name: Focus Pattern\n"
        "description: Protect long focus blocks.\n"
        "category: work_pattern\n"
        "aliases: [context defense]\n"
        "tags: [focus, calibration]\n"
        "parents: [personal/reference/foundations]\n"
        "last_observed: '2026-08-20'\n"
        "observation_count: 2\n"
        "---\n\n"
        "# Focus Pattern\n\n"
        "## Definition\n\nProtect long focus blocks.\n\n"
        "## Evidence\n\n* 2026-08-20 - A long block worked.\n"
    )
    path.write_bytes(text.encode("utf-8"))
    return path


def _context() -> TrustedIngressContext:
    tenant = "tenant-personal-import-test"
    return TrustedIngressContext(
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
        authorization_fingerprint="b" * 64,
        permitted_purposes=("personal_knowledge.history_import",),
    )


@pytest.fixture
def personal_importer(tmp_path, personal_store):
    sources = SourceStore.create(tmp_path / "sources")
    return (
        PersonalKnowledgeImportCoordinator(personal_store, sources),
        sources,
        _context(),
    )


def _prepare(coordinator, context, *, cohort_id: str, source_root: Path):
    return coordinator.prepare(
        cohort_id=cohort_id,
        source_root=source_root,
        ingress_context=context,
    )


def _rehearsal(personal_store):
    return authorize_isolated_rehearsal_root(
        personal_store.db_path.parent,
        authority_paths={"personal_knowledge": personal_store.db_path},
    )


def test_inventory_is_deterministic_and_quarantines_bad_files(tmp_path):
    _write_note(tmp_path)
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    first = inventory_personal_markdown(tmp_path)
    second = inventory_personal_markdown(tmp_path)
    assert [item.receipt() for item in first] == [item.receipt() for item in second]
    assert [item.relative_path for item in first] == [
        "bad.md",
        "work_patterns/focus.md",
    ]
    assert first[0].disposition == "quarantined"
    assert first[0].reason_code == "missing_frontmatter"
    assert first[1].source_sha256 == second[1].source_sha256


def test_prepare_is_hidden_and_quarantine_blocks_seal(
    tmp_path, personal_store, personal_importer
):
    _write_note(tmp_path)
    (tmp_path / "bad.md").write_text("---\nname: [\n---\n", encoding="utf-8")
    coordinator, _sources, context = personal_importer
    prepared = _prepare(coordinator, context, cohort_id="cohort-1", source_root=tmp_path)
    assert prepared["state"] == "prepared"
    assert prepared["quarantined_count"] == 1
    assert personal_store.list_units() == []
    with pytest.raises(PersonalKnowledgeImportError, match="quarantined"):
        coordinator.seal("cohort-1")
    assert personal_store.list_units() == []


def test_changed_source_rejects_verification(tmp_path, personal_store, personal_importer):
    note = _write_note(tmp_path)
    coordinator, _sources, context = personal_importer
    _prepare(coordinator, context, cohort_id="cohort-1", source_root=tmp_path)
    note.write_text(note.read_text(encoding="utf-8") + "changed", encoding="utf-8")
    with pytest.raises(PersonalKnowledgeImportError, match="corpus changed"):
        coordinator.verify("cohort-1")


def test_atomic_seal_parity_replay_and_no_post_seal_vault_access(
    tmp_path, personal_store, personal_importer
):
    source = tmp_path / "source"
    source.mkdir()
    _write_note(source)
    coordinator, sources, context = personal_importer
    prepared = coordinator.prepare(
        cohort_id="cohort-1",
        source_root=source,
        ingress_context=context,
    )
    assert prepared["staged_count"] == 1
    replayed_prepare = coordinator.prepare(
        cohort_id="cohort-1",
        source_root=source,
        ingress_context=context,
    )
    assert replayed_prepare["request_sha256"] == prepared["request_sha256"]
    assert personal_store.list_units() == []
    coordinator.verify("cohort-1")
    with pytest.raises(
        PersonalKnowledgeImportError, match="requires preseal maintenance"
    ):
        coordinator.seal("cohort-1")
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        coordinator.seal("cohort-1", allow_unfenced_rehearsal=True)
    sealed = coordinator.seal(
        "cohort-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(personal_store),
    )
    assert sealed["state"] == "sealed"
    assert sealed["imported_count"] == 1
    assert sealed["items"][0]["parity_status"] == "exact"
    assert sealed["receipt"]["schema"] == "wb.personal-knowledge-import-receipt/v1"
    assert sealed["receipt"]["importedCount"] == 1
    unit = personal_store.get_unit("personal/work_patterns/focus")
    assert unit["name"] == "Focus Pattern"
    assert unit["source_ref"].startswith("wb-source://")
    assert unit["observation_count"] == 2
    assert len(personal_store.observations(unit["unit_id"])) == 1
    assert len(personal_store.revisions(unit["unit_id"])) == 1
    assert len(personal_store.pending_outbox()) == 1
    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE status='acknowledged'"
        ).fetchone()[0] == 1

    retired = tmp_path / "retired"
    source.rename(retired)
    # Sealed replays, provider reads, and native writes are receipt/database
    # operations. They remain successful with the source tree gone.
    assert coordinator.seal(
        "cohort-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(personal_store),
    )["state"] == "sealed"
    provider = SQLitePersonalKnowledgeProvider(personal_store)
    assert provider.load_units()["personal/work_patterns/focus"].name == "Focus Pattern"
    service = PersonalKnowledgeService(personal_store)
    created = service.create(
        logical_path="personal/preferences/native",
        name="Native",
        body="Created after seal.",
        categories=["preference"],
        idempotency_key="native-create",
    )
    assert created["status"] == "created"


def test_failed_atomic_seal_leaves_no_visible_partial_rows(
    tmp_path, personal_store, personal_importer
):
    _write_note(tmp_path, "a.md")
    _write_note(tmp_path, "b.md")
    coordinator, _sources, context = personal_importer
    _prepare(coordinator, context, cohort_id="cohort-1", source_root=tmp_path)
    coordinator.verify("cohort-1")
    # Introduce a path collision after staging, without touching the frozen
    # source. The seal must roll back the earlier staged insert too.
    personal_store.create_unit(
        logical_path="personal/b", name="Existing", body="x",
        idempotency_key="existing",
    )
    with pytest.raises(Exception, match="logical path already exists"):
        coordinator.seal(
            "cohort-1",
            allow_unfenced_rehearsal=True,
            rehearsal_authorization=_rehearsal(personal_store),
        )
    paths = [row["current_path"] for row in personal_store.list_units()]
    assert paths == ["personal/b"]
    assert coordinator.status("cohort-1")["state"] == "verified"


def test_source_commit_crash_replays_without_duplicate_sources(
    tmp_path, personal_store
):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_note(source)
    sources = SourceStore.create(tmp_path / "sources")
    context = _context()
    crashed = False

    def crash_once(_cohort_id: str, _relative_path: str) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated source commit crash")

    coordinator = PersonalKnowledgeImportCoordinator(
        personal_store, sources, source_committed=crash_once
    )
    with pytest.raises(RuntimeError, match="simulated source commit crash"):
        _prepare(coordinator, context, cohort_id="crash-cohort", source_root=source)
    assert personal_store.list_units() == []

    replay = PersonalKnowledgeImportCoordinator(personal_store, sources).prepare(
        cohort_id="crash-cohort",
        source_root=source,
        ingress_context=context,
    )
    assert replay["source_count"] == replay["source_acknowledged_count"] == 1
    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_usage_intents").fetchone()[0] == 1
    conn = personal_store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM personal_import_items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT source_usage_state FROM personal_import_source_dependencies"
        ).fetchone()[0] == "acknowledged"
    finally:
        conn.close()


def test_prepare_refuses_a_null_or_caller_synthesized_source(
    tmp_path, personal_store
):
    source = tmp_path / "legacy"
    source.mkdir()
    _write_note(source)
    context = _context()
    with pytest.raises(PersonalKnowledgeImportError, match="Sources authority"):
        PersonalKnowledgeImportCoordinator(personal_store).prepare(
            cohort_id="personal-no-source",
            source_root=source,
            ingress_context=context,
        )
    sources = SourceStore.create(tmp_path / "sources")
    with pytest.raises(PersonalKnowledgeImportError, match="caller-supplied"):
        PersonalKnowledgeImportCoordinator(personal_store, sources).prepare(
            cohort_id="personal-synthetic-source",
            source_root=source,
            source_refs={"work_patterns/focus.md": "legacy-personal:focus"},
            ingress_context=context,
        )
    conn = personal_store.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM personal_import_cohorts"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_cutover_maintenance_holds_native_writes_until_postseal_evidence(
    tmp_path, personal_store, personal_importer
):
    source = tmp_path / "legacy-maintenance"
    source.mkdir()
    _write_note(source)
    coordinator, _sources, context = personal_importer
    cohort_id = "personal-maintenance-cohort"
    inventory_sha = _inventory_digest(inventory_personal_markdown(source))

    paused = coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="personal-pause-001",
        actor="test-operator",
    )
    assert paused["state"] == "preseal_fenced"
    assert coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="personal-pause-001",
        actor="test-operator",
    ) == paused
    with pytest.raises(CutoverMaintenanceError, match="reused"):
        coordinator.pause_mutations(
            cohort_id=cohort_id,
            inventory_sha256="f" * 64,
            mutation_id="personal-pause-001",
            actor="test-operator",
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        personal_store.create_unit(
            logical_path="personal/preferences/blocked",
            name="Blocked",
            idempotency_key="blocked-create",
        )

    # Import staging is operator-only and remains available inside the fence.
    coordinator.prepare(
        cohort_id=cohort_id,
        source_root=source,
        ingress_context=context,
    )
    coordinator.verify(cohort_id)
    with pytest.raises(PersonalKnowledgeImportError, match="remain held"):
        coordinator.seal(cohort_id)
    sealed = coordinator.seal(cohort_id, retain_maintenance_fence=True)
    assert sealed["state"] == "sealed"

    with pytest.raises(PersonalKnowledgeImportError, match="cannot resume"):
        coordinator.resume_preseal_mutations(
            cohort_id=cohort_id,
            mutation_id="personal-resume-001",
            actor="test-operator",
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        personal_store.create_unit(
            logical_path="personal/preferences/still-blocked",
            name="Still blocked",
            idempotency_key="still-blocked-create",
        )
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        coordinator.release_postseal_mutations(
            cohort_id=cohort_id,
            mutation_id="personal-release-missing-capability",
            actor="test-operator",
            rehearsal_evidence_sha256s={
                "databaseCheckpoint": "1" * 64,
                "search": "2" * 64,
                "detachment": "3" * 64,
            },
            allow_unvalidated_rehearsal=True,
        )
    with pytest.raises(CutoverMaintenanceError, match="incomplete"):
        coordinator.release_postseal_mutations(
            cohort_id=cohort_id,
            mutation_id="personal-release-001",
            actor="test-operator",
            rehearsal_evidence_sha256s={"search": "1" * 64},
            allow_unvalidated_rehearsal=True,
            rehearsal_authorization=_rehearsal(personal_store),
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        personal_store.create_unit(
            logical_path="personal/preferences/failed-release",
            name="Failed release",
            idempotency_key="failed-release-create",
        )

    evidence = {
        "databaseCheckpoint": "1" * 64,
        "search": "2" * 64,
        "detachment": "3" * 64,
    }
    released = coordinator.release_postseal_mutations(
        cohort_id=cohort_id,
        mutation_id="personal-release-002",
        actor="test-operator",
        rehearsal_evidence_sha256s=evidence,
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(personal_store),
    )
    assert released["state"] == "open"
    assert {
        key: released["evidenceSha256s"][key] for key in evidence
    } == evidence
    assert len(released["evidenceSha256s"]["authorityHead"]) == 64
    assert coordinator.release_postseal_mutations(
        cohort_id=cohort_id,
        mutation_id="personal-release-002",
        actor="test-operator",
        rehearsal_evidence_sha256s=evidence,
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(personal_store),
    ) == released
    assert personal_store.create_unit(
        logical_path="personal/preferences/released",
        name="Released",
        idempotency_key="released-create",
    )["status"] == "created"


def test_cutover_maintenance_can_resume_only_before_seal(
    tmp_path, personal_store, personal_importer
):
    source = tmp_path / "legacy-resume"
    source.mkdir()
    _write_note(source)
    coordinator, _sources, _context = personal_importer
    cohort_id = "personal-resume-cohort"
    inventory_sha = _inventory_digest(inventory_personal_markdown(source))
    coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="personal-pause-resume",
        actor="test-operator",
    )
    resumed = coordinator.resume_preseal_mutations(
        cohort_id=cohort_id,
        mutation_id="personal-resume-safe",
        actor="test-operator",
    )
    assert resumed["state"] == "open"
    assert coordinator.resume_preseal_mutations(
        cohort_id=cohort_id,
        mutation_id="personal-resume-safe",
        actor="test-operator",
    ) == resumed
    assert personal_store.create_unit(
        logical_path="personal/preferences/resumed",
        name="Resumed",
        idempotency_key="resumed-create",
    )["status"] == "created"
