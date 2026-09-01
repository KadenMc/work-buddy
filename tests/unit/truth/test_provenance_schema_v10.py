"""Schema-v10 direct-entry provenance basis migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from work_buddy.cowork import provenance
from work_buddy.truth import documents, export as truth_export, migrations, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import new_id
from work_buddy.truth.store import TruthStore


HUMAN = Actor("human", "schema-v10-user")


def _profile() -> dict[str, object]:
    return {
        "store_id": new_id(),
        "profile": "provenance-schema-v10",
        "title": "Provenance schema v10",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "document_surface": {
            "enabled": True,
            "allowed_document_classes": ["co_authored", "generated"],
            "feedback_capture": True,
        },
    }


def _attestation(*, authorship: str, review: str) -> dict[str, object]:
    contributors = (
        [
            {
                "kind": "current_user",
                "ref": HUMAN.ref,
                "identity_status": "local_actor_ref",
            }
        ]
        if authorship in {"human", "mixed"}
        else []
    )
    reviewers = (
        [
            {
                "kind": "current_user",
                "ref": HUMAN.ref,
                "identity_status": "local_actor_ref",
            }
        ]
        if review == "reviewed"
        else []
    )
    return {
        "schema": provenance.INPUT_ATTESTATION_SCHEMA,
        "authorship": {"kind": authorship, "contributors": contributors},
        "human_review": {
            "status": review,
            "reviewers": reviewers,
        },
    }


def test_v10_migration_preserves_lineage_and_admits_direct_entry_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    current_runner = migrations.TRUTH_MIGRATIONS
    current_format_version = truth_export.FORMAT_VERSION
    v9_runner = migrations._TruthMigrationRunner(
        "truth",
        migrations=list(current_runner.migrations[:9]),
    )
    monkeypatch.setattr(migrations, "TRUTH_MIGRATIONS", v9_runner)
    monkeypatch.setattr(truth_export, "FORMAT_VERSION", 9)
    monkeypatch.setattr(
        documents,
        "_provision_default_document_truth_policy",
        lambda *_args, **_kwargs: None,
    )

    root = tmp_path / "v9-store"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    snapshot = b"schema-v10-opaque-ydoc-snapshot"
    snapshot_sha256 = ydoc_store.write_snapshot(store, snapshot=snapshot)
    head = ydoc_store.structured_head_from_segments(snapshot, ())
    document, _version, _created = documents.register_ready_document(
        store,
        path="docs/schema-v10.md",
        title="Schema v10",
        document_class="co_authored",
        projection_bytes=b"# Schema v10\n\nExisting AI text.\n",
        ydoc_snapshot_sha256=snapshot_sha256,
        structured_head_sha256=head,
        actor=HUMAN,
        mode="create",
    )
    original, span_id = provenance.record_span_attestation(
        store,
        document_id=document.id,
        exact="Existing AI text.",
        attestation=_attestation(authorship="ai", review="not_reviewed"),
        actor=HUMAN,
        idempotency_key="schema-v9-origin-0001",
        source={"kind": "paste", "format": "plain_text"},
        basis_kind="user_attestation",
        expected_structured_head_sha256=head,
    )
    successor = provenance.record_human_review(
        store,
        document_id=document.id,
        attestation_id=original.id,
        actor=HUMAN,
        idempotency_key="schema-v9-review-0001",
        expected_structured_head_sha256=head,
    )
    before = store.list_document_provenance_attestations(document.id)
    assert before == (original, successor)
    assert successor.supersedes_id == original.id
    with store.connect() as conn:
        assert migrations.current_version(conn) == 9

    monkeypatch.setattr(migrations, "TRUTH_MIGRATIONS", current_runner)
    monkeypatch.setattr(truth_export, "FORMAT_VERSION", current_format_version)
    migrated = TruthStore.open(store.paths.sidecar)
    after = migrated.list_document_provenance_attestations(document.id)

    assert after == before
    assert [row.id for row in after] == [original.id, successor.id]
    assert [row.idempotency_key for row in after] == [
        "schema-v9-origin-0001",
        "schema-v9-review-0001",
    ]
    assert [row.canonical_sha256 for row in after] == [
        original.canonical_sha256,
        successor.canonical_sha256,
    ]
    assert after[1].supersedes_id == after[0].id
    with migrated.connect() as conn:
        assert migrations.current_version(conn) == 11
        assert conn.execute(
            "SELECT schema_version FROM store_info"
        ).fetchone()[0] == 11
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'document_provenance_attestations'"
        ).fetchone()[0]
        assert "automatic_direct_entry_attribution" in table_sql
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE document_provenance_attestations SET basis_ref = 'changed' "
                "WHERE id = ?",
                (original.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM document_provenance_attestations WHERE id = ?",
                (successor.id,),
            )

    direct, direct_span_id = provenance.record_span_attestation(
        migrated,
        document_id=document.id,
        exact="Schema v10",
        attestation=_attestation(authorship="human", review="not_applicable"),
        actor=HUMAN,
        idempotency_key="schema-v10-direct-entry-0001",
        source={"kind": "direct_entry", "format": "plain_text"},
        basis_kind="automatic_direct_entry_attribution",
        expected_structured_head_sha256=head,
    )
    assert direct.document_span_id == direct_span_id
    assert direct_span_id != span_id
    assert direct.source_kind == "direct_entry"
    assert direct.basis_kind == "automatic_direct_entry_attribution"
