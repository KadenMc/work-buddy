"""Schema-v8 provenance and historical import-source safety."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from work_buddy.truth import documents, migrations, ydoc_store
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.store import DocumentRecord, TruthStore


HUMAN = Actor("human", "user-provenance-v8")


def _profile() -> dict[str, object]:
    return {
        "store_id": new_id(),
        "profile": "test",
        "title": "Provenance v8",
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


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def test_v8_backfills_legacy_import_source_without_losing_producer_meta():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for migration in (
        migrations._m001_initial_schema,
        migrations._m002_document_surface,
        migrations._m003_cowork_document_foundation,
        migrations._m004_cowork_lifecycle_intents,
        migrations._m005_cowork_verify_cothink,
        migrations._m006_cothink_item_lifecycle,
        migrations._m007_portable_cowork_coordination,
    ):
        migration(conn)

    document_id = "11" * 16
    version_id = "12" * 16
    digest = "a" * 64
    snapshot = "b" * 64
    head = "c" * 64
    conn.execute(
        "INSERT INTO documents "
        "(id, path, title, document_class, content_sha256, "
        "ydoc_snapshot_sha256, created_at, created_by_kind, created_by_ref, "
        "meta_json) VALUES (?, 'drafts/paper.md', 'Paper', 'co_authored', ?, ?, "
        "'2026-07-01T00:00:00+00:00', 'agent_run', 'run-1', ?)",
        (
            document_id,
            digest,
            snapshot,
            canonical_json(
                {
                    "model": "test-model",
                    "harness": "pytest",
                    "surface": "cowork",
                    "session_id": "session-1",
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO document_versions "
        "(id, document_id, kind, projection_sha256, ydoc_snapshot_sha256, "
        "structured_head_sha256, created_at, actor_kind, actor_ref, detail) "
        "VALUES (?, ?, 'initial_import', ?, ?, ?, "
        "'2026-07-01T00:00:00+00:00', 'human', 'user-1', 'import')",
        (version_id, document_id, digest, snapshot, head),
    )

    migrations._m008_document_provenance_attestations(conn)
    assert migrations.backfill_v8_legacy_import_provenance(conn) == 1
    assert migrations.backfill_v8_legacy_import_provenance(conn) == 0
    meta = json.loads(
        conn.execute(
            "SELECT meta_json FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()[0]
    )
    assert meta["model"] == "test-model"
    assert meta["harness"] == "pytest"
    assert meta["surface"] == "cowork"
    assert meta["session_id"] == "session-1"
    assert meta["source"] == {
        "kind": "file_import",
        "path": "drafts/paper.md",
        "sha256": digest,
        "writeback_policy": "never",
    }
    backfilled = conn.execute(
        "SELECT * FROM document_provenance_attestations "
        "WHERE document_version_id = ?",
        (version_id,),
    ).fetchone()
    assert backfilled["authorship_kind"] == "unknown"
    assert backfilled["review_status"] == "unknown"
    assert backfilled["basis_kind"] == "migration_backfill"
    assert backfilled["basis_ref"] == "truth-schema-v8:legacy-file-import"
    assert backfilled["attested_by_kind"] == "system"
    assert json.loads(backfilled["source_json"]) == {
        "kind": "file_import",
        "path": "drafts/paper.md",
        "sha256": digest,
    }
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM ledger_records "
            "WHERE record_type = 'document_provenance_attestation' "
            "AND record_key = ?",
            (backfilled["id"],),
        ).fetchone()[0]
        == 1
    )

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(document_provenance_attestations)"
        )
    }
    assert {
        "document_version_id",
        "document_span_id",
        "human_contributors_json",
        "human_reviewers_json",
        "source_json",
        "supersedes_id",
        "canonical_sha256",
        "attested_by_kind",
        "attested_by_ref",
    } <= columns
    bootstrap_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(cowork_bootstrap_intents)"
        )
    }
    assert {
        "importer_id",
        "source_media_type",
        "import_attestation_sha256",
    } <= bootstrap_columns
    try:
        conn.execute(
            "UPDATE documents SET meta_json = '{}' WHERE id = ?",
            (document_id,),
        )
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("v8 did not restore the document update trigger")
    conn.close()


def test_open_backfills_early_v8_store_without_changing_migration_hash(
    tmp_path: Path,
):
    root = tmp_path / "early-v8"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    projection = b"# Early v8 import\n"
    snapshot = b"early-v8-import-snapshot"
    snapshot_sha = ydoc_store.write_snapshot(store, snapshot=snapshot)
    head = ydoc_store.structured_head_from_segments(snapshot, ())
    document, version, _ = documents.register_ready_document(
        store,
        path="drafts/early-v8.md",
        title="Early v8",
        document_class="co_authored",
        projection_bytes=projection,
        ydoc_snapshot_sha256=snapshot_sha,
        structured_head_sha256=head,
        actor=HUMAN,
        mode="import",
        document_meta={
            "source": {
                "kind": "file_import",
                "path": "drafts/early-v8.md",
                "sha256": sha256_bytes(projection),
                "writeback_policy": "never",
            }
        },
    )
    assert store.list_document_provenance_attestations(document.id) == ()

    reopened = TruthStore.open(store.paths.sidecar)
    rows = reopened.list_document_provenance_attestations(document.id)
    assert len(rows) == 1
    assert rows[0].document_version_id == version.id
    assert rows[0].authorship_kind == "unknown"
    assert rows[0].review_status == "unknown"
    assert rows[0].basis_kind == "migration_backfill"

    reopened_again = TruthStore.open(store.paths.sidecar)
    assert reopened_again.list_document_provenance_attestations(
        document.id
    ) == rows


def test_source_writeback_policy_fails_closed_for_explicit_unknown_metadata():
    base = DocumentRecord(
        id="21" * 16,
        path="drafts/paper.md",
        title="Paper",
        document_class="co_authored",
        content_sha256="d" * 64,
        ydoc_snapshot_sha256=None,
        created_at="2026-07-01T00:00:00+00:00",
        created_by_kind="human",
        created_by_ref="user-1",
        meta_json=None,
    )
    assert documents.source_writeback_policy(base) == "same_file"
    assert (
        documents.source_writeback_policy(
            replace(
                base,
                meta_json=canonical_json(
                    {"source": {"writeback_policy": "future-value"}}
                ),
            )
        )
        == "never"
    )
    assert (
        documents.source_writeback_policy(
            replace(base, meta_json=canonical_json({"source": {"kind": "file"}}))
        )
        == "never"
    )
    assert (
        documents.source_writeback_policy(replace(base, meta_json="{broken"))
        == "never"
    )


def test_v7_export_upcast_marks_import_source_non_writeback(
    tmp_path: Path,
):
    source_root = tmp_path / "source"
    source_root.mkdir()
    store = TruthStore.create(source_root, _profile())
    projection = b"# Historical import\n"
    snapshot = b"historical-import-snapshot"
    snapshot_sha = ydoc_store.write_snapshot(store, snapshot=snapshot)
    head = ydoc_store.structured_head_from_segments(snapshot, ())
    document, _version, _ = documents.register_ready_document(
        store,
        path="drafts/historical.md",
        title="Historical",
        document_class="co_authored",
        projection_bytes=projection,
        ydoc_snapshot_sha256=snapshot_sha,
        structured_head_sha256=head,
        actor=HUMAN,
        mode="import",
    )
    objects = [
        json.loads(line)
        for line in export_store(store).path.read_text(encoding="utf-8").splitlines()
    ]
    objects[0]["format_version"] = 7
    objects[0]["store_info"]["schema_version"] = 7
    prefix = b"".join(
        (canonical_json(value) + "\n").encode("utf-8")
        for value in objects[:-1]
    )
    objects[-1]["stream_sha256"] = sha256_bytes(prefix)
    legacy_payload = prefix + (canonical_json(objects[-1]) + "\n").encode("utf-8")

    target_root = tmp_path / "restored"
    target_root.mkdir()
    restored = import_store(
        legacy_payload,
        target_root,
        registry=_EmptyRegistry(),
    ).store
    imported = documents.get_document(restored, document.id)
    meta = json.loads(imported.meta_json or "{}")
    assert meta["source"] == {
        "kind": "file_import",
        "path": "drafts/historical.md",
        "sha256": sha256_bytes(projection),
        "writeback_policy": "never",
    }
    assert documents.source_writeback_policy(imported) == "never"
