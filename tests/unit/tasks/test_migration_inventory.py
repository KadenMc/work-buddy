from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.tasks.import_legacy import (
    LegacyTaskDocumentImporter,
    _kernel_projection_equivalent,
    _literal_markdown_envelope,
    _literal_projection_equivalent,
    main,
    rewrite_local_references,
)
from work_buddy.tasks.migration import (
    InventoryItem,
    LegacyInventoryError,
    LegacyManifestEntry,
    LegacyTaskInventoryBuilder,
    deterministic_import_task_id,
)
from work_buddy.tasks.migrations import TASK_MIGRATIONS
from work_buddy.tasks.store import TaskStore


LIVE_NOTE = "11111111-1111-4111-8111-111111111111"
IDLESS_NOTE = "22222222-2222-4222-8222-222222222222"
DELETED_NOTE = "33333333-3333-4333-8333-333333333333"
RECOVERY_NOTE = "44444444-4444-4444-8444-444444444444"
MISSING_NOTE = "55555555-5555-4555-8555-555555555555"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _fixture(tmp_path: Path):
    source = tmp_path / "legacy-tasks"
    files = {
        "master-task-list.md": (
            f"- [ ] #todo Existing task [[{LIVE_NOTE}|📓]] "
            "#projects/alpha 📅 2026-09-01 🆔 t-a1\r\n"
            f"- [ ] #todo Get local files [[{IDLESS_NOTE}|📓]] #projects/home\r\n"
        ).encode("utf-8"),
        "archive.md": b"# Archived\n",
        f"notes/{LIVE_NOTE}.md": (
            b"# Existing\n\nAPI routes:\n- `GET /`\n\nUse `JOBS_DIR`.\n"
        ),
        f"notes/{IDLESS_NOTE}.md": (
            "# Local links\n\n![[assets/report.pdf]]\n"
            "[key](assets/private.ppk)\n"
        ).encode("utf-8"),
        f"notes/{DELETED_NOTE}.md": b"# Deleted\n\nPreserve me.\n",
        f"notes/{RECOVERY_NOTE}.md": b"# Recovery\n\nUnattached.\n",
        "notes/_bridge_test_new.md": b"diagnostic\n",
        "notes/assets/report.pdf": b"%PDF-1.4 isolated fixture\n",
        "notes/assets/private.ppk": b"fixture-private-key-not-real\n",
        "notes/assets/assets.md": b"# Asset index\n",
        ".space/context.mdb": b"fixture-mdb",
        "task-dashboard.md": b"",
    }
    for relative, content in files.items():
        _write(source / relative, content)
    manifest = tuple(
        LegacyManifestEntry(relative, len(content), _sha(content))
        for relative, content in sorted(files.items())
    )

    db = tmp_path / "task_metadata.db"
    store = TaskStore(db)
    store.initialize()
    with store.transaction() as conn:
        for task_id, note_uuid, deleted_at in (
            ("t-a1", LIVE_NOTE, None),
            ("t-del", DELETED_NOTE, "2026-02-01T00:00:00+00:00"),
            ("t-missing", MISSING_NOTE, "2026-02-02T00:00:00+00:00"),
        ):
            conn.execute(
                """
                INSERT INTO task_metadata (
                    task_id, state, urgency, note_uuid, created_at, updated_at,
                    description, deleted_at, revision
                ) VALUES (?, 'inbox', 'medium', ?, '2026-01-01', '2026-01-01',
                          ?, ?, 1)
                """,
                (task_id, note_uuid, task_id, deleted_at),
            )
    conn = store.connect()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return source, db, manifest


def _inventory(tmp_path: Path, *, cohort_id: str = "cohort-fixture"):
    source, db, manifest = _fixture(tmp_path)
    inventory = LegacyTaskInventoryBuilder(
        cohort_id=cohort_id,
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()
    return source, db, manifest, inventory


def _manifest_csv(path: Path, manifest: tuple[LegacyManifestEntry, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("relative_path", "bytes", "sha256"))
        writer.writeheader()
        for entry in manifest:
            writer.writerow(
                {
                    "relative_path": entry.relative_path,
                    "bytes": entry.byte_length,
                    "sha256": entry.sha256,
                }
            )


def test_inventory_is_typed_complete_deterministic_and_path_scrubbed(tmp_path):
    source, db, manifest, inventory = _inventory(tmp_path)

    assert inventory.valid, inventory.errors
    assert inventory.counts["database_tasks"] == 3
    assert inventory.counts["identified_tasks"] == 1
    assert inventory.counts["idless_tasks"] == 1
    assert inventory.counts["task_note_live"] == 1
    assert inventory.counts["task_note_idless"] == 1
    assert inventory.counts["task_note_deleted"] == 1
    assert inventory.counts["recovered_task_document"] == 1
    assert inventory.counts["dangling_deleted_note"] == 1
    assert inventory.counts["diagnostic_excluded"] == 1
    assert inventory.counts["local_file_pdf"] == 1
    assert inventory.counts["local_file_sensitive"] == 1
    idless = next(line for line in inventory.task_lines if line.is_idless)
    assert idless.imported_task_id == deterministic_import_task_id(
        inventory.cohort_id,
        idless.relative_path,
        idless.line_number,
        idless.line_sha256,
    )
    rebuilt = LegacyTaskInventoryBuilder(
        cohort_id=inventory.cohort_id,
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()
    assert rebuilt.inventory_sha256 == inventory.inventory_sha256
    rendered = json.dumps(inventory.to_dict(include_items=True), ensure_ascii=False)
    assert str(source) not in rendered
    assert str(db) not in rendered


def test_inventory_fails_closed_for_unmanifested_or_changed_source(tmp_path):
    source, db, manifest = _fixture(tmp_path)
    _write(source / "notes" / "surprise.md", b"unexpected")
    inventory = LegacyTaskInventoryBuilder(
        cohort_id="cohort-extra",
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()
    assert not inventory.valid
    assert any("unmanifested source entry" in error for error in inventory.errors)
    with pytest.raises(LegacyInventoryError):
        inventory.require_valid()


def test_inventory_preserves_non_attachment_markdown_targets_without_blocking(
    tmp_path,
):
    source, db, manifest = _fixture(tmp_path)
    relative = f"notes/{LIVE_NOTE}.md"
    content = (
        "# References\n\n"
        "[design](C:\\External\\designs\\DECISIONS.md)\n"
        "[support](mailto:ops@example.test)\n"
        "[source](dispatch/router.py:172)\n"
        "[placeholder](url)\n"
    ).encode("utf-8")
    _write(source / relative, content)
    manifest = tuple(
        LegacyManifestEntry(relative, len(content), _sha(content))
        if entry.relative_path == relative
        else entry
        for entry in manifest
    )

    inventory = LegacyTaskInventoryBuilder(
        cohort_id="cohort-non-attachments",
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()

    assert inventory.valid, inventory.errors
    note = next(item for item in inventory.items if item.item_key == f"file:{relative}")
    classifications = {
        item["classification"]
        for item in note.metadata["preserved_non_manifest_references"]
    }
    assert classifications == {
        "external_local_path",
        "external_uri",
        "code_location",
        "relative_url_or_placeholder",
    }


def test_inventory_still_blocks_a_missing_relative_attachment(tmp_path):
    source, db, manifest = _fixture(tmp_path)
    relative = f"notes/{LIVE_NOTE}.md"
    content = b"# Missing attachment\n\n[report](assets/missing.pdf)\n"
    _write(source / relative, content)
    manifest = tuple(
        LegacyManifestEntry(relative, len(content), _sha(content))
        if entry.relative_path == relative
        else entry
        for entry in manifest
    )

    inventory = LegacyTaskInventoryBuilder(
        cohort_id="cohort-missing-attachment",
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()

    assert not inventory.valid
    assert any(
        "unresolved local reference" in error and "assets/missing.pdf" in error
        for error in inventory.errors
    )


def test_kernel_projection_parity_accepts_semantic_markdown_canonicalization_only():
    assert _kernel_projection_equivalent(
        b"## Summary\n\nUse **init** &amp; JOBS\\_DIR.\n",
        b"## Summary\nUse __init__ & JOBS_DIR.\n",
    )
    assert not _kernel_projection_equivalent(
        b"## Summary\nUse the later revision.\n",
        b"## Summary\nUse the current revision.\n",
    )
    assert not _kernel_projection_equivalent(
        b"alpha beta\n",
        b"beta alpha\n",
    )
    assert _kernel_projection_equivalent(
        b"Visit [https://example.test/a](https://example.test/a) or "
        b"[dev@example.test](mailto:dev@example.test).\n",
        b"Visit https://example.test/a or dev@example.test.\n",
    )
    assert not _kernel_projection_equivalent(
        b"Visit [the guide](https://example.test/wrong).\n",
        b"Visit [the guide](https://example.test/right).\n",
    )
    assert not _kernel_projection_equivalent(
        b"Use `a-b` and foo/bar.\n",
        b"Use `a/b` and foo.bar.\n",
    )
    assert _kernel_projection_equivalent(
        b"| Name             | State |\n| ---------------- | ----- |\n| Alpha            | Open  |\n",
        b"| Name | State |\n|---|---|\n| Alpha | Open |\n",
    )


@pytest.mark.parametrize(
    "source",
    (
        b"plain source without a newline",
        b"# Heading\n\n- > nested source line\n",
        b"# Heading\n\n- > nested source line\n\n",
        b"\xef\xbb\xbf# Heading\r\n\r\nBody\r\n",
        b"```\nraw <token>\n```\n",
    ),
)
def test_literal_markdown_envelope_extracts_exact_source_bytes(source):
    enveloped = _literal_markdown_envelope(source)

    assert _literal_projection_equivalent(enveloped, source)


def test_literal_projection_rejects_punctuation_only_payload_corruption():
    source = b"Use `a/b` and foo.bar.\n"
    enveloped = _literal_markdown_envelope(source)
    corrupted = enveloped.replace(b"a/b", b"a-b").replace(b"foo.bar", b"foo/bar")

    assert not _literal_projection_equivalent(corrupted, source)


def test_literal_projection_accepts_strict_legacy_trailing_newline_representation():
    source = b"line one\nline two\n\n"
    legacy_projection = b"```\nline one\nline two\n```\n\n"

    assert _literal_projection_equivalent(legacy_projection, source)
    assert not _literal_projection_equivalent(
        legacy_projection.replace(b"line one", b"line/one"),
        source,
    )


def test_literal_bootstrap_uses_envelope_fidelity_for_no_newline_source():
    source = b"[guide](C:\\docs\\guide.txt)"

    class Kernel:
        def __init__(self):
            self.operations = []

        def request(self, operation, *, request_id):
            self.operations.append((operation, request_id))
            if len(self.operations) == 1:
                return SimpleNamespace(snapshot=b"structured", projection=b"guide")
            return SimpleNamespace(
                snapshot=b"literal",
                projection=operation["sourceBase64"],
            )

    kernel = Kernel()
    outcome, strategy = LegacyTaskDocumentImporter._bootstrap_projection(
        SimpleNamespace(kernel=kernel),
        source,
        request_id="literal-no-newline",
    )

    assert strategy == "literal_markdown_fallback"
    assert kernel.operations[1][0]["newlineStyle"] == "lf"
    assert _literal_projection_equivalent(outcome.projection, source)


def test_local_file_link_identity_is_stable_and_document_scoped():
    attachment = InventoryItem(
        item_key="file:notes/assets/report.pdf",
        item_kind="source_file",
        classification="local_file_pdf",
        reason="fixture",
        relative_path="notes/assets/report.pdf",
        content_sha256="a" * 64,
        byte_length=42,
    )
    content = b"[report](assets/report.pdf)\n"

    first, first_rewrites = rewrite_local_references(
        content,
        note_path="notes/11111111-1111-4111-8111-111111111111.md",
        attachments=(attachment,),
        root_id="root_fixture",
        document_id="doc_one",
    )
    replay, replay_rewrites = rewrite_local_references(
        content,
        note_path="notes/11111111-1111-4111-8111-111111111111.md",
        attachments=(attachment,),
        root_id="root_fixture",
        document_id="doc_one",
    )
    second, second_rewrites = rewrite_local_references(
        content,
        note_path="notes/22222222-2222-4222-8222-222222222222.md",
        attachments=(attachment,),
        root_id="root_fixture",
        document_id="doc_two",
    )

    assert first == replay
    assert first_rewrites == replay_rewrites
    assert first_rewrites[0]["link_id"] != second_rewrites[0]["link_id"]
    assert first_rewrites[0]["relative_path"] == second_rewrites[0]["relative_path"]
    assert b"wb-local-file:lf_" in first and b"wb-local-file:lf_" in second


def test_inventory_accepts_a_restart_from_an_intermediate_native_schema(tmp_path):
    source, db, manifest = _fixture(tmp_path)
    intermediate = TASK_MIGRATIONS.target_version - 1
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "DELETE FROM _migration_history WHERE version>?", (intermediate,)
        )
        conn.execute(f"PRAGMA user_version={intermediate}")
        conn.commit()
    finally:
        conn.close()

    inventory = LegacyTaskInventoryBuilder(
        cohort_id="cohort-intermediate-schema",
        source_root=source,
        task_db_path=db,
        manifest=manifest,
    ).build()

    assert inventory.valid
    assert inventory.source_db_schema_version == intermediate


def test_cli_defaults_to_read_only_inventory(tmp_path, capsys):
    source, db, manifest = _fixture(tmp_path)
    manifest_path = tmp_path / "manifest.csv"
    _manifest_csv(manifest_path, manifest)
    before = db.read_bytes()

    result = main(
        [
            "--cohort-id",
            "cohort-cli",
            "--source-root",
            str(source),
            "--task-db",
            str(db),
            "--manifest",
            str(manifest_path),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert db.read_bytes() == before
    assert not (tmp_path / "sources").exists()
    assert not (tmp_path / "cowork").exists()


def test_shadow_cli_validates_backup_receipt_before_creating_stores(tmp_path):
    source, db, manifest = _fixture(tmp_path)
    manifest_path = tmp_path / "manifest.csv"
    _manifest_csv(manifest_path, manifest)
    receipts_path = tmp_path / "backup-receipts.json"
    receipts_path.write_text('[{"verified": false}]', encoding="utf-8")
    before = db.read_bytes()

    with pytest.raises(SystemExit, match="verified backup receipt"):
        main(
            [
                "--cohort-id",
                "cohort-cli-invalid-backup",
                "--source-root",
                str(source),
                "--task-db",
                str(db),
                "--manifest",
                str(manifest_path),
                "--apply-shadow",
                "--sources-root",
                str(tmp_path / "sources"),
                "--cowork-store-root",
                str(tmp_path / "cowork"),
                "--truth-registry",
                str(tmp_path / "truth-registry.db"),
                "--backup-receipts-json",
                str(receipts_path),
            ]
        )

    assert db.read_bytes() == before
    assert not (tmp_path / "sources").exists()
    assert not (tmp_path / "cowork").exists()
    assert not (tmp_path / "truth-registry.db").exists()
