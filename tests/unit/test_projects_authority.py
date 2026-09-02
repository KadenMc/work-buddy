from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    CutoverMaintenanceFenced,
    authorize_isolated_rehearsal_root,
)
from work_buddy.projects import store
from work_buddy.projects.authority import (
    ProjectAuthorityError,
    ProjectImportCoordinator,
    _inventory_sha,
    authority_status,
    inventory_project_notes,
    reconcile_projects_authoritatively,
    require_markdown_write_allowed,
)
from work_buddy.projects.partition import ProjectsPartition
from work_buddy.sources import ActorRef, SourceStore, TrustedIngressContext


def _note(
    slug: str,
    *,
    name: str | None = None,
    status: str = "active",
    body: str = "description",
) -> str:
    return (
        "---\n"
        f"slug: {slug}\n"
        f"name: {name or slug.title()}\n"
        f"status: {status}\n"
        "---\n"
        f"# {name or slug.title()}\n\n"
        f"{body}\n"
    )


@pytest.fixture
def project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "projects.db"
    root = tmp_path / "legacy-projects"
    root.mkdir()
    monkeypatch.setattr(store, "_db_path", lambda: db)
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-project-import-test"
    context = TrustedIngressContext(
        issuer=ActorRef("test-authority", "project-import", "service", tenant),
        issuer_version="test/v1",
        inputter=ActorRef("test-authority", "legacy-owner", "human", tenant),
        service_principal=ActorRef("test-authority", "projects", "service", tenant),
        tenant_scope_id=tenant,
        surface="project-history-import",
        namespace="project-history-import-staging",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="historical_inputter_only",
        authorization_fingerprint="a" * 64,
        permitted_purposes=("projects.history_import",),
    )
    return db, root, sources, context


def _coordinator(project_env, *, source_committed=None):
    return ProjectImportCoordinator(
        project_env[2], source_committed=source_committed
    )


def _prepare(coordinator, project_env, cohort_id: str):
    return coordinator.prepare(
        cohort_id=cohort_id,
        source_root=project_env[1],
        ingress_context=project_env[3],
    )


def _rehearsal(project_env):
    database = project_env[0]
    return authorize_isolated_rehearsal_root(
        database.parent,
        authority_paths={"projects": database},
    )


def test_hidden_stage_then_atomic_seal_and_replay(project_env):
    _db, root, sources, _context = project_env
    store.upsert_project("existing", description="database value")
    (root / "alpha.md").write_text(
        _note("alpha", name="Alpha", body="legacy alpha"), encoding="utf-8"
    )
    coordinator = _coordinator(project_env)

    prepared = _prepare(coordinator, project_env, "projects-cutover-1")
    assert _prepare(coordinator, project_env, "projects-cutover-1") == prepared
    assert prepared["state"] == "prepared"
    assert prepared["file_count"] == 1
    assert prepared["source_count"] == 1
    assert prepared["source_acknowledged_count"] == 1
    assert store.get_project("alpha") is None
    assert authority_status()["authority"] == "legacy_markdown"

    assert coordinator.verify("projects-cutover-1")["state"] == "verified"
    with pytest.raises(ProjectAuthorityError, match="requires preseal maintenance"):
        coordinator.seal("projects-cutover-1")
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        coordinator.seal(
            "projects-cutover-1",
            allow_unfenced_rehearsal=True,
        )
    sealed = coordinator.seal(
        "projects-cutover-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    )
    assert sealed["state"] == "sealed"
    assert sealed["importedCount"] == 1
    assert store.get_project("alpha")["description"] == "legacy alpha"
    assert authority_status()["authority"] == "sqlite"

    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM source_usage_intents WHERE status='acknowledged'"
        ).fetchone()[0] == 1
    with sqlite3.connect(_db) as connection:
        source_ref = connection.execute(
            "SELECT source_ref FROM project_legacy_import_map"
        ).fetchone()[0]
        assert source_ref.startswith("wb-source://")

    before = _revision_count(_db)
    replay = coordinator.seal(
        "projects-cutover-1",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    )
    assert replay["state"] == "sealed"
    assert _revision_count(_db) == before


def test_projects_context_uses_read_only_native_renderer_after_seal(
    project_env,
    monkeypatch: pytest.MonkeyPatch,
):
    db, root, _sources, _context = project_env
    (root / "alpha.md").write_text(
        _note("alpha", name="Alpha", body="native context"),
        encoding="utf-8",
    )
    coordinator = _coordinator(project_env)
    _prepare(coordinator, project_env, "projects-context-cutover")
    coordinator.verify("projects-context-cutover")
    coordinator.seal(
        "projects-context-cutover",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    )
    revisions_before = _revision_count(db)

    from work_buddy.mcp_server import context_wrappers
    from work_buddy.projects import sync

    def legacy_access(*_args, **_kwargs):
        raise AssertionError("post-seal Project context touched a legacy source")

    monkeypatch.setattr(context_wrappers, "_cfg_with_overrides", legacy_access)
    monkeypatch.setattr(sync, "sync_projects", legacy_access)

    rendered = context_wrappers.get_projects_context()

    assert "### alpha" in rendered
    assert "**Evidence:** sqlite" in rendered
    assert "native context" in rendered
    assert _revision_count(db) == revisions_before


def test_projects_context_retains_legacy_sync_before_seal(
    project_env,
    monkeypatch: pytest.MonkeyPatch,
):
    from work_buddy.mcp_server import context_wrappers
    from work_buddy.projects import sync

    expected_config = {"vault_root": "legacy-root"}
    monkeypatch.setattr(
        context_wrappers,
        "_cfg_with_overrides",
        lambda: expected_config,
    )
    calls = []

    def legacy_sync(config, *, statuses=None):
        calls.append((config, statuses))
        return "legacy-project-context"

    monkeypatch.setattr(sync, "sync_projects", legacy_sync)

    assert context_wrappers.get_projects_context(statuses=["paused"]) == (
        "legacy-project-context"
    )
    assert calls == [(expected_config, ["paused"])]


def test_drift_and_quarantine_block_seal(project_env):
    _db, root, _sources, _context = project_env
    (root / "good.md").write_text(_note("good"), encoding="utf-8")
    (root / "bad.md").write_text("not frontmatter", encoding="utf-8")
    coordinator = _coordinator(project_env)
    prepared = _prepare(coordinator, project_env, "projects-cutover-2")
    assert prepared["quarantined_count"] == 1
    with pytest.raises(ProjectAuthorityError, match="quarantined"):
        coordinator.verify("projects-cutover-2")

    (root / "good.md").write_text(_note("good", body="changed"), encoding="utf-8")
    with pytest.raises(ProjectAuthorityError, match="changed"):
        coordinator.verify("projects-cutover-2", allow_quarantined=True)
    assert coordinator.abort("projects-cutover-2")["state"] == "aborted"


def test_post_seal_fence_idempotency_cas_and_outbox(project_env):
    db, root, _sources, _context = project_env
    (root / "alpha.md").write_text(_note("alpha"), encoding="utf-8")
    coordinator = _coordinator(project_env)
    _prepare(coordinator, project_env, "projects-cutover-3")
    coordinator.verify("projects-cutover-3")
    coordinator.seal(
        "projects-cutover-3",
        allow_unfenced_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    )

    initial = store.get_project("alpha")
    result = store.update_project(
        "alpha",
        description="database-only",
        expected_revision_id=initial["current_revision_id"],
        intent_id="mutation-1",
    )
    assert result["description"] == "database-only"
    revision_count = _revision_count(db)
    replay = store.update_project(
        "alpha",
        description="database-only",
        expected_revision_id=initial["current_revision_id"],
        intent_id="mutation-1",
    )
    assert replay == result
    assert _revision_count(db) == revision_count

    with pytest.raises(ValueError, match="different mutation"):
        store.update_project("alpha", description="other", intent_id="mutation-1")
    with pytest.raises(ValueError, match="stale project revision"):
        store.update_project("alpha", name="stale", expected_revision_id=1)
    with pytest.raises(ProjectAuthorityError, match="frozen legacy"):
        require_markdown_write_allowed()
    assert reconcile_projects_authoritatively()["status"] == "disabled"

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM project_outbox WHERE project_id=?",
            (result["id"],),
        ).fetchone()[0] >= 2
        assert conn.execute(
            "SELECT first_native_write_at FROM project_authority_state WHERE singleton=1"
        ).fetchone()[0]


def test_document_authoritative_description_rejects_plain_mutation(project_env):
    db, _root, _sources, _context = project_env
    project = store.upsert_project("bound", description="seed")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE project_body_roles SET body_mode='document',"
            "document_binding_id='binding-1' WHERE project_id=? AND role='description'",
            (project["id"],),
        )
        conn.commit()
    with pytest.raises(ValueError, match="document-authoritative"):
        store.update_project("bound", description="illegal")


def test_projects_partition_uses_stable_ids_and_revision_hashes(project_env):
    db, _root, _sources, _context = project_env
    project = store.upsert_project(
        "searchable", name="Searchable", description="indexed database body"
    )
    partition = ProjectsPartition()
    first = {ref.item_id: ref.content_hash for ref in partition.discover()}
    docs = partition.parse(str(project["id"]))
    assert len(docs) == 1
    assert docs[0].metadata["projectId"] == project["id"]
    assert docs[0].fields["content"] == "indexed database body"

    # Certification injects an immutable connection and must not fall through
    # to Projects' migrate-on-connect store helper during parse.
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def immutable_connection():
        connection = sqlite3.connect(
            f"file:{db.resolve().as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    immutable_docs = ProjectsPartition(
        connection_factory=immutable_connection
    ).parse(str(project["id"]))
    assert immutable_docs[0].fields["content"] == "indexed database body"

    store.update_project("searchable", description="new indexed body")
    second = {ref.item_id: ref.content_hash for ref in partition.discover()}
    assert first[str(project["id"])] != second[str(project["id"])]

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE project_body_roles SET body_mode='document',"
            "document_binding_id='binding-2' WHERE project_id=? AND role='description'",
            (project["id"],),
        )
        conn.commit()
    assert partition.parse(str(project["id"])) == []


def test_source_commit_crash_replays_without_duplicate_sources(project_env):
    db, root, sources, context = project_env
    (root / "alpha.md").write_text(_note("alpha"), encoding="utf-8")
    crashed = False

    def crash_once(_cohort_id: str, _relative_path: str) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated source commit crash")

    coordinator = _coordinator(project_env, source_committed=crash_once)
    with pytest.raises(RuntimeError, match="simulated source commit crash"):
        _prepare(coordinator, project_env, "projects-crash-boundary")
    assert store.get_project("alpha") is None

    replay = ProjectImportCoordinator(sources).prepare(
        cohort_id="projects-crash-boundary",
        source_root=root,
        ingress_context=context,
    )
    assert replay["source_acknowledged_count"] == 1
    with sources.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingress_submissions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM source_usage_intents").fetchone()[0] == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM project_import_items").fetchone()[0] == 1
        assert connection.execute(
            "SELECT source_usage_state FROM project_import_source_dependencies"
        ).fetchone()[0] == "acknowledged"


def test_prepare_refuses_a_null_or_caller_synthesized_source(project_env):
    db, root, _sources, context = project_env
    (root / "alpha.md").write_text(_note("alpha"), encoding="utf-8")
    with pytest.raises(ProjectAuthorityError, match="Sources authority"):
        ProjectImportCoordinator().prepare(
            cohort_id="projects-no-source",
            source_root=root,
            ingress_context=context,
        )
    with pytest.raises(ProjectAuthorityError, match="caller-supplied"):
        ProjectImportCoordinator(project_env[2]).prepare(
            cohort_id="projects-synthetic-source",
            source_root=root,
            source_refs={"alpha.md": "legacy-project:alpha"},
            ingress_context=context,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_import_cohorts"
        ).fetchone()[0] == 0


def test_projects_cutover_maintenance_is_roll_forward_after_seal(project_env):
    _db, root, _sources, _context = project_env
    (root / "alpha.md").write_text(_note("alpha"), encoding="utf-8")
    coordinator = _coordinator(project_env)
    cohort_id = "projects-maintenance-cohort"
    inventory_sha = _inventory_sha(inventory_project_notes(root))
    paused = coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="projects-pause-001",
        actor="test-operator",
    )
    assert paused["state"] == "preseal_fenced"
    assert coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="projects-pause-001",
        actor="test-operator",
    ) == paused
    with pytest.raises(CutoverMaintenanceError, match="reused"):
        coordinator.pause_mutations(
            cohort_id=cohort_id,
            inventory_sha256="f" * 64,
            mutation_id="projects-pause-001",
            actor="test-operator",
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        store.upsert_project("blocked", description="blocked")

    _prepare(coordinator, project_env, cohort_id)
    coordinator.verify(cohort_id)
    with pytest.raises(ProjectAuthorityError, match="remain held"):
        coordinator.seal(cohort_id)
    assert coordinator.seal(
        cohort_id, retain_maintenance_fence=True
    )["state"] == "sealed"
    with pytest.raises(ProjectAuthorityError, match="cannot resume"):
        coordinator.resume_preseal_mutations(
            cohort_id=cohort_id,
            mutation_id="projects-resume-001",
            actor="test-operator",
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        store.update_project("alpha", description="still blocked")
    with pytest.raises(CutoverMaintenanceError, match="authorization is required"):
        coordinator.release_postseal_mutations(
            cohort_id=cohort_id,
            mutation_id="projects-release-missing-capability",
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
            mutation_id="projects-release-001",
            actor="test-operator",
            rehearsal_evidence_sha256s={"search": "1" * 64},
            allow_unvalidated_rehearsal=True,
            rehearsal_authorization=_rehearsal(project_env),
        )
    with pytest.raises(CutoverMaintenanceFenced, match="fenced"):
        store.update_project("alpha", description="failed release")

    evidence = {
        "databaseCheckpoint": "1" * 64,
        "search": "2" * 64,
        "detachment": "3" * 64,
    }
    released = coordinator.release_postseal_mutations(
        cohort_id=cohort_id,
        mutation_id="projects-release-002",
        actor="test-operator",
        rehearsal_evidence_sha256s=evidence,
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    )
    assert released["state"] == "open"
    assert {
        key: released["evidenceSha256s"][key] for key in evidence
    } == evidence
    assert len(released["evidenceSha256s"]["authorityHead"]) == 64
    assert coordinator.release_postseal_mutations(
        cohort_id=cohort_id,
        mutation_id="projects-release-002",
        actor="test-operator",
        rehearsal_evidence_sha256s=evidence,
        allow_unvalidated_rehearsal=True,
        rehearsal_authorization=_rehearsal(project_env),
    ) == released
    assert store.update_project("alpha", description="released")["description"] == (
        "released"
    )


def test_projects_cutover_maintenance_resumes_before_seal(project_env):
    _db, root, _sources, _context = project_env
    (root / "alpha.md").write_text(_note("alpha"), encoding="utf-8")
    coordinator = _coordinator(project_env)
    cohort_id = "projects-resume-cohort"
    inventory_sha = _inventory_sha(inventory_project_notes(root))
    coordinator.pause_mutations(
        cohort_id=cohort_id,
        inventory_sha256=inventory_sha,
        mutation_id="projects-pause-resume",
        actor="test-operator",
    )
    resumed = coordinator.resume_preseal_mutations(
        cohort_id=cohort_id,
        mutation_id="projects-resume-safe",
        actor="test-operator",
    )
    assert resumed["state"] == "open"
    assert coordinator.resume_preseal_mutations(
        cohort_id=cohort_id,
        mutation_id="projects-resume-safe",
        actor="test-operator",
    ) == resumed
    assert store.upsert_project("resumed", description="available")["slug"] == (
        "resumed"
    )


def _revision_count(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM project_revisions").fetchone()[0])
