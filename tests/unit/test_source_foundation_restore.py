from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosurePreflight,
    DisclosureSelector,
    DisclosureState,
)
from work_buddy.backups.source_foundation_reconciliation import (
    SourceFoundationPaths,
    archive_cleared_restore_fence,
    inspect_source_foundation_cohorts,
    record_identity_trust,
    reconstitute_sanitized_identity,
)
from work_buddy.backups.source_foundation_restore import (
    SourceFoundationRestorePending,
    write_restore_fence,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.domain_service import DomainContentStoreManager
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
from work_buddy.hindsight_projection.runtime import run_projection_tick
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.security.local_identity import LocalIdentityAuthority
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import ActorRef
from work_buddy.sources.store import SourceStore
from work_buddy.task_notes.store import TaskNoteMigrationStore
from work_buddy.truth.identity import new_id
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore
from work_buddy.truth.ydoc_store import write_snapshot


def _profile(store_id: str) -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "test",
        "title": "Restore fence test",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": False,
    }


@pytest.fixture
def isolated_fence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    marker = tmp_path / "db" / "source_foundation_restore_pending.json"
    monkeypatch.setattr(
        "work_buddy.backups.source_foundation_restore.restore_fence_path",
        lambda: marker,
    )
    return marker


def _write_enrollment(authority: LocalIdentityAuthority, path: Path) -> str:
    actor = authority.enrolled_actor()
    payload = {
        "schema": "wb.local-identity-enrollment-export/v1",
        "schema_version": "1",
        "issuer_authority_id": actor.issuer_authority_id,
        "tenant_scope_id": actor.tenant_scope_id,
        "local_actor_id": actor.subject,
        "restores_live_sessions": False,
        "trust_required_before_identity_reuse": True,
    }
    raw = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _cohort(tmp_path: Path) -> tuple[SourceFoundationPaths, Path, str]:
    paths = SourceFoundationPaths(
        agent_execution_db=tmp_path / "db" / "agent_execution.db",
        cowork_conversation_source_dependencies_db=(
            tmp_path / "db" / "cowork_conversation_source_dependencies.db"
        ),
        conversations_db=tmp_path / "agents" / "conversations.db",
        sources_root=tmp_path / "db" / "sources",
        local_identity_db=tmp_path / "db" / "local_identity.db",
        journal_capture_db=tmp_path / "db" / "journal_capture.db",
        task_note_migration_db=tmp_path / "db" / "task_note_migration.db",
        truth_registry_db=tmp_path / "db" / "truth_registry.db",
    )
    SourceStore.create(paths.sources_root)
    DisclosureManifestStore(paths.agent_execution_db)
    authority = LocalIdentityAuthority(paths.local_identity_db)
    enrollment = tmp_path / "db" / "local_identity_enrollment.json"
    enrollment_sha = _write_enrollment(authority, enrollment)
    JournalCaptureStore(paths.journal_capture_db)
    TaskNoteMigrationStore(paths.task_note_migration_db)
    TruthStoreRegistry(paths.truth_registry_db)
    from work_buddy.cowork.conversation_source_dependencies import (
        conversation_dependencies_for_document,
    )

    conversation_dependencies_for_document(
        "0" * 32,
        "1" * 32,
        path=paths.cowork_conversation_source_dependencies_db,
    )
    paths.conversations_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.conversations_db) as conn:
        from work_buddy.conversations.store import _ensure_schema

        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
    return paths, enrollment, enrollment_sha


def test_restore_fence_makes_foundation_stores_read_only(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    sources = SourceStore.create(tmp_path / "sources")
    truth = TruthStore.create(tmp_path / "truth", _profile(new_id()))
    causality = DocumentCausalityStore(truth.paths.sidecar)
    disclosure = DisclosureManifestStore(tmp_path / "agent-execution.db")
    task_notes = TaskNoteMigrationStore(tmp_path / "task-note.db")
    hindsight = TruthHindsightProjectionStore(truth.paths.db)
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    registry.register(truth)
    write_restore_fence({"snapshot_id": "snap-test"}, path=isolated_fence)

    assert SourceStore.open(sources.paths.root).authority_id == sources.authority_id
    assert TruthStore.open(truth.paths.sidecar).store_id == truth.store_id
    assert DocumentCausalityStore(truth.paths.sidecar).export_bundle()["tables"] == (
        causality.export_bundle()["tables"]
    )
    with pytest.raises(SourceFoundationRestorePending):
        with sources.write_transaction():
            pass
    with pytest.raises(SourceFoundationRestorePending):
        with truth.write_transaction():
            pass
    with pytest.raises(SourceFoundationRestorePending):
        with causality.transaction():
            pass
    with pytest.raises(SourceFoundationRestorePending):
        disclosure.create_run(run_id="run-1", worker_session_id="worker-1")
    with pytest.raises(SourceFoundationRestorePending):
        with task_notes.transaction():
            pass
    with pytest.raises(SourceFoundationRestorePending):
        with hindsight.write_transaction():
            pass
    with pytest.raises(SourceFoundationRestorePending):
        write_snapshot(truth, snapshot=b"must-not-write")
    with pytest.raises(SourceFoundationRestorePending):
        run_projection_tick(limit_per_store=1)
    assert TruthStoreRegistry(registry.db_path).list_stores(refresh=True)[0].store_id == (
        truth.store_id
    )
    with pytest.raises(SourceFoundationRestorePending):
        registry.touch(truth)


def test_restore_fence_blocks_truth_import_and_domain_store_creation_before_files(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    source = TruthStore.create(tmp_path / "portable-source", _profile(new_id()))
    portable = export_store(source)
    registry = TruthStoreRegistry(tmp_path / "truth-registry.db")
    target = tmp_path / "portable-target"
    domain_root = tmp_path / "domain-content"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    write_restore_fence({"snapshot_id": "snap-create-guards"}, path=isolated_fence)

    with pytest.raises(SourceFoundationRestorePending):
        import_store(portable.path, target, registry=registry)
    assert not target.exists()

    manager = DomainContentStoreManager(root=domain_root, registry=registry)
    with pytest.raises(SourceFoundationRestorePending):
        manager.ensure(vault_root)
    assert not domain_root.exists()


def test_identity_trust_is_explicit_and_never_imports_sessions_or_gestures(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    authority = LocalIdentityAuthority(paths.local_identity_db)
    with authority._connect() as conn:  # exact recovery fixture inspection
        before = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "local_browser_sessions",
                "local_session_csrf_tokens",
                "local_gesture_challenges",
            )
        }
    write_restore_fence(
        {
            "snapshot_id": "snap-identity",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )

    status = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )
    assert {item["code"] for item in status["blockers"]} == {
        "local_identity_trust_required"
    }
    trust = record_identity_trust(
        enrollment,
        marker_path=isolated_fence,
        paths=paths,
    )
    assert trust["restored_sessions"] is False
    assert trust["restored_gestures"] is False
    with authority._connect() as conn:
        after = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in before
        }
    assert after == before
    assert inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )["state"] == "ready_to_clear"


def test_sanitized_identity_reconstitution_restores_no_live_authority(
    tmp_path: Path,
) -> None:
    source_authority = LocalIdentityAuthority(tmp_path / "source-identity.db")
    enrollment = tmp_path / "enrollment.json"
    enrollment_sha = _write_enrollment(source_authority, enrollment)
    target = tmp_path / "fresh" / "local-identity.db"

    receipt = reconstitute_sanitized_identity(
        enrollment,
        local_identity_db=target,
        expected_sha256=enrollment_sha,
    )

    assert receipt["restored_sessions"] is False
    assert receipt["restored_gestures"] is False
    assert LocalIdentityAuthority(target).enrolled_actor() == source_authority.enrolled_actor()
    conn = sqlite3.connect(target)
    try:
        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "local_bootstrap_tokens",
                "local_browser_sessions",
                "local_session_csrf_tokens",
                "local_gesture_challenges",
            )
        } == {
            "local_bootstrap_tokens": 0,
            "local_browser_sessions": 0,
            "local_session_csrf_tokens": 0,
            "local_gesture_challenges": 0,
        }
    finally:
        conn.close()


def test_restore_operator_is_high_consent_and_clears_only_validated_cohort(
    isolated_fence: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.consent import get_consent_metadata
    from work_buddy.mcp_server.ops import source_foundation_restore_ops as restore_ops

    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    write_restore_fence(
        {
            "snapshot_id": "snap-operator",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    monkeypatch.setattr(
        restore_ops.SourceFoundationPaths,
        "current",
        classmethod(lambda _cls: paths),
    )
    monkeypatch.setattr(restore_ops, "_authorize", lambda *_args: None)

    metadata = get_consent_metadata("source_foundation.restore_reconcile")
    assert metadata is not None
    assert metadata["consent_weight"] == "high"
    assert metadata["grant_policy"] == "per_invocation"
    assert restore_ops.source_foundation_restore_operator("status")["state"] == (
        "blocked"
    )

    result = restore_ops.source_foundation_restore_operator(
        "reconcile",
        snapshot_id="snap-operator",
    )

    assert result["state"] == "clear"
    assert result["cleared"] is True
    assert result["identityTrust"]["restored_sessions"] is False
    assert result["identityTrust"]["restored_gestures"] is False
    assert Path(result["receiptPath"]).is_file()
    assert not isolated_fence.exists()


def test_ambiguous_disclosure_blocks_clear_and_reconciliation_never_replays(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    from work_buddy.mcp_server.ops.source_foundation_restore_ops import (
        _reconcile_disclosures,
    )

    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    authority = LocalIdentityAuthority(paths.local_identity_db)
    tenant = authority.enrolled_actor().tenant_scope_id
    sources = SourceStore.open(paths.sources_root)
    service = SourcesDisclosureService(sources, tenant_scope_id=tenant)
    content = b"never replay these source bytes"
    captured = service.capture_for_disclosure(
        exact_content=content,
        source_role="human_input",
        run_id="restore-run",
        tool_call_id="input-1",
        idempotency_key="capture-1",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        purpose="restore-test",
        authorization_ref="authorization:restore-test",
        recipient="account-backed-model",
        provider_id="test-provider",
        model_id="test-model",
    )
    manifest = DisclosureManifestStore(paths.agent_execution_db)
    gateway = DisclosureGateway(manifest, service)
    preflight = DisclosurePreflight(
        run_id="restore-run",
        worker_session_id="worker-1",
        tool_call_id="input-1",
        idempotency_key="disclosure-1",
        direction=DisclosureDirection.INBOUND_TO_MODEL,
        source_ref=captured.source_ref,
        representation_id=captured.representation_id,
        selector=DisclosureSelector(kind="whole"),
        content_sha256=captured.content_sha256,
        byte_length=captured.byte_length,
        recipient="account-backed-model",
        provider_id="test-provider",
        model_id="test-model",
        authorization_ref="authorization:restore-test",
        purpose="restore-test",
    )
    entry = gateway.preflight(preflight)
    gateway.mark_possibly_sent(entry.id)
    write_restore_fence(
        {
            "snapshot_id": "snap-ambiguous",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    record_identity_trust(
        enrollment,
        marker_path=isolated_fence,
        paths=paths,
    )
    blocked = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )
    assert "agent_disclosure_possibly_sent" in {
        item["code"] for item in blocked["blockers"]
    }

    reconciled = _reconcile_disclosures(paths, {entry.id: "not_sent"})
    assert reconciled == [{"entryId": entry.id, "outcome": "not_sent"}]
    assert manifest.get_entry(entry.id).state is DisclosureState.NOT_SENT
    ready = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )
    assert ready["state"] == "ready_to_clear"
    receipt = archive_cleared_restore_fence(
        marker_path=isolated_fence,
        expected_snapshot_id="snap-ambiguous",
    )
    assert receipt.is_file()
    assert not isolated_fence.exists()


def test_restore_reconciliation_blocks_a_tampered_sources_blob(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    sources = SourceStore.open(paths.sources_root)
    sources.capture_source(
        content=b"x" * (70 * 1024),
        source_role="imported_file",
        tenant_scope_id="tenant-test",
        originating_surface="restore-test",
    )
    conn = sources.connect()
    try:
        blob = conn.execute(
            "SELECT content_sha256,relative_path FROM source_blobs"
        ).fetchone()
    finally:
        conn.close()
    assert blob is not None
    (paths.sources_root / "blobs" / str(blob["relative_path"])).write_bytes(
        b"tampered"
    )
    write_restore_fence(
        {
            "snapshot_id": "snap-tampered-source",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    record_identity_trust(
        enrollment,
        marker_path=isolated_fence,
        paths=paths,
    )

    status = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )

    assert status["state"] == "blocked"
    assert "sources_blob_cohort_mismatch" in {
        item["code"] for item in status["blockers"]
    }


def test_restore_reconciliation_keeps_conversation_review_work_fenced(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    from work_buddy.cowork.conversation_source_dependencies import (
        record_conversation_source_dependency,
    )

    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    content = "A retained semantic derivative that still needs human review."
    with sqlite3.connect(paths.conversations_db) as conn:
        metadata = json.dumps(
            {
                "cowork_store_id": "a" * 32,
                "cowork_document_id": "b" * 32,
                "cowork_kind": "document_conversation",
            }
        )
        conn.execute(
            "INSERT INTO conversations "
            "(conversation_id,title,status,created_at,updated_at,source,metadata) "
            "VALUES ('conversation-1','','open','2026-01-01','2026-01-01',"
            "'cowork_document',?)",
            (metadata,),
        )
        conn.execute(
            "INSERT INTO messages "
            "(message_id,conversation_id,role,content,created_at) "
            "VALUES ('message-1','conversation-1','agent',?,'2026-01-01')",
            (content,),
        )
    dependency = record_conversation_source_dependency(
        store_id="a" * 32,
        document_id="b" * 32,
        conversation_id="conversation-1",
        message_id="message-1",
        role="agent",
        content=content,
        path=paths.cowork_conversation_source_dependencies_db,
    )
    with sqlite3.connect(paths.cowork_conversation_source_dependencies_db) as conn:
        conn.execute(
            "UPDATE cowork_conversation_source_dependencies "
            "SET state='review_required' WHERE dependency_id=?",
            (dependency.dependency_id,),
        )
    write_restore_fence(
        {
            "snapshot_id": "snap-conversation-review",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    record_identity_trust(
        enrollment,
        marker_path=isolated_fence,
        paths=paths,
    )

    status = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )

    assert status["state"] == "blocked"
    assert "cowork_conversation_dependency_review_required" in {
        item["code"] for item in status["blockers"]
    }


def test_restore_reconciliation_never_infers_missing_cowork_dependency(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    metadata = json.dumps(
        {
            "cowork_store_id": "c" * 32,
            "cowork_document_id": "d" * 32,
            "cowork_kind": "document_conversation",
        }
    )
    with sqlite3.connect(paths.conversations_db) as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(conversation_id,title,status,created_at,updated_at,source,metadata) "
            "VALUES ('conversation-missing','','open','2026-01-01','2026-01-01',"
            "'cowork_document',?)",
            (metadata,),
        )
        conn.execute(
            "INSERT INTO messages "
            "(message_id,conversation_id,role,content,created_at) "
            "VALUES ('message-missing','conversation-missing','user','retained','2026-01-01')"
        )
    write_restore_fence(
        {
            "snapshot_id": "snap-missing-dependency",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    record_identity_trust(enrollment, marker_path=isolated_fence, paths=paths)

    status = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )

    missing = [
        item
        for item in status["blockers"]
        if item["code"] == "cowork_conversation_dependency_missing"
    ]
    assert len(missing) == 1
    assert missing[0]["context"] == {
        "message_id": "message-missing",
        "conversation_id": "conversation-missing",
        "store_id": "c" * 32,
        "document_id": "d" * 32,
        "role": "user",
    }


def test_restore_reconciliation_rejects_dependency_owned_by_another_document(
    isolated_fence: Path,
    tmp_path: Path,
) -> None:
    from work_buddy.cowork.conversation_source_dependencies import (
        record_conversation_source_dependency,
    )

    paths, enrollment, enrollment_sha = _cohort(tmp_path)
    metadata = json.dumps(
        {
            "cowork_store_id": "e" * 32,
            "cowork_document_id": "f" * 32,
            "cowork_kind": "document_conversation",
        }
    )
    with sqlite3.connect(paths.conversations_db) as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(conversation_id,title,status,created_at,updated_at,source,metadata) "
            "VALUES ('conversation-owner','','open','2026-01-01','2026-01-01',"
            "'cowork_document',?)",
            (metadata,),
        )
        conn.execute(
            "INSERT INTO messages "
            "(message_id,conversation_id,role,content,created_at) "
            "VALUES ('message-owner','conversation-owner','agent','retained','2026-01-01')"
        )
    record_conversation_source_dependency(
        store_id="0" * 32,
        document_id="1" * 32,
        conversation_id="conversation-owner",
        message_id="message-owner",
        role="agent",
        content="retained",
        path=paths.cowork_conversation_source_dependencies_db,
    )
    write_restore_fence(
        {
            "snapshot_id": "snap-owner-mismatch",
            "truth_stores": [],
            "identity_enrollment": {
                "member": enrollment.name,
                "sha256": enrollment_sha,
                "trusted": False,
            },
            "reconciliation": {"state": "pending", "identity_trust": None},
        },
        path=isolated_fence,
    )
    record_identity_trust(enrollment, marker_path=isolated_fence, paths=paths)

    status = inspect_source_foundation_cohorts(
        marker_path=isolated_fence,
        paths=paths,
    )
    assert "cowork_conversation_dependency_message_mismatch" in {
        item["code"] for item in status["blockers"]
    }
