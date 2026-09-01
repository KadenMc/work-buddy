from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import work_buddy.backups.sensitive as sensitive
from work_buddy.backups.sensitive import (
    AuthorizedSourceExport,
    SensitiveBackupError,
    create_sensitive_checkpoint,
    create_sensitive_checkpoint_from_authorized_export,
    rehearse_sensitive_checkpoint_restore,
    verify_sensitive_checkpoint,
)
from work_buddy.mcp_server.ops import backups_ops
from work_buddy.sources import (
    ActorRef,
    ExportAuthorization,
    ImportAuthorization,
    SourceStore,
)


def _journal(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version=7")
        conn.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.execute("INSERT INTO entries VALUES('entry-1', 'private journal text')")
        conn.commit()
    finally:
        conn.close()


def _authorized_export(root: Path) -> AuthorizedSourceExport:
    path = root / "sources-authorized-export.jsonl"
    lines = [
        {
            "record_type": "manifest",
            "schema": "wb.sources-export/v1",
            "export_id": "export-1",
            "include_content": True,
            "item_count": 1,
        },
        {"record_type": "source_item", "bundle": {"private": "content"}},
    ]
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in lines
    ).encode()
    path.write_bytes(payload)
    return AuthorizedSourceExport(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        export_id="export-1",
        item_count=1,
        issued_copy_count=1,
    )


def test_checkpoint_uses_existing_guarded_export_without_copying_it(tmp_path: Path):
    root = tmp_path / "checkpoint"
    root.mkdir()
    receipt = _authorized_export(root)
    journal = tmp_path / "journal.db"
    _journal(journal)

    result = create_sensitive_checkpoint_from_authorized_export(
        root,
        journal_db=journal,
        source_export=receipt,
        idempotency_key="pre-cutover",
        created_at="2026-08-27T00:00:00+00:00",
    )
    replay = create_sensitive_checkpoint_from_authorized_export(
        root,
        journal_db=journal,
        source_export=receipt,
        idempotency_key="pre-cutover",
        created_at="ignored-on-replay",
    )

    assert replay == result
    assert verify_sensitive_checkpoint(root) == result
    assert not (root / "sources.jsonl").exists()
    assert (root / "sources-authorized-export.jsonl") == receipt.path
    manifest = json.loads((root / "SENSITIVE-MANIFEST.json").read_text())
    assert manifest["remoteEligible"] is False
    assert manifest["members"]["journal"]["userVersion"] == 7
    snapshot = sqlite3.connect(root / "journal_capture.db")
    try:
        assert snapshot.execute("SELECT body FROM entries").fetchone()[0] == (
            "private journal text"
        )
    finally:
        snapshot.close()


def test_checkpoint_rejects_tampered_or_external_source_export(tmp_path: Path):
    root = tmp_path / "checkpoint"
    root.mkdir()
    receipt = _authorized_export(root)
    journal = tmp_path / "journal.db"
    _journal(journal)
    receipt.path.write_text("tampered", encoding="utf-8")

    with pytest.raises(SensitiveBackupError, match="digest mismatch"):
        create_sensitive_checkpoint_from_authorized_export(
            root,
            journal_db=journal,
            source_export=receipt,
            idempotency_key="tampered",
        )

    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SensitiveBackupError, match="inside the checkpoint"):
        create_sensitive_checkpoint_from_authorized_export(
            root,
            journal_db=journal,
            source_export=AuthorizedSourceExport(
                path=outside,
                sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                export_id="export-2",
                item_count=0,
                issued_copy_count=0,
            ),
            idempotency_key="external",
        )


def test_checkpoint_can_issue_an_empty_sources_export_atomically(tmp_path: Path):
    journal = tmp_path / "journal.db"
    _journal(journal)
    store = SourceStore.create(tmp_path / "sources", authority_id="authority-test")
    principal = ActorRef(
        issuer_authority_id="issuer-test",
        subject="user-test",
        kind="human",
        tenant_scope_id="tenant-test",
    )
    authorization = ExportAuthorization(
        principal=principal,
        authorization_fingerprint="a" * 64,
    )

    result = create_sensitive_checkpoint(
        tmp_path / "checkpoint",
        journal_db=journal,
        source_store=store,
        source_authorization=authorization,
        idempotency_key="empty-store",
        created_at="2026-08-27T00:00:00+00:00",
    )

    assert result.source_item_count == 0
    assert verify_sensitive_checkpoint(result.path) == result


def test_sensitive_checkpoint_operator_is_local_and_prose_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "backups" / "snapshot"
    root.mkdir(parents=True)
    receipt = _authorized_export(root)
    journal = tmp_path / "journal.db"
    _journal(journal)

    import work_buddy.paths as paths

    monkeypatch.setattr(paths, "data_dir", lambda _name: tmp_path / "backups")
    monkeypatch.setattr(paths, "resolve", lambda _resource: journal)
    result = backups_ops.data_sensitive_checkpoint.__wrapped__(
        source_export_path=str(receipt.path),
        source_export_sha256=receipt.sha256,
        source_export_id=receipt.export_id,
        source_item_count=receipt.item_count,
        issued_copy_count=receipt.issued_copy_count,
        idempotency_key="operator-test",
    )

    assert result["remoteEligible"] is False
    assert result["sourceItemCount"] == 1
    assert "private journal text" not in json.dumps(result)
    assert "content" not in json.dumps(result)


def test_coordinated_restore_rehearsal_restores_journal_and_operational_sources(
    tmp_path: Path,
):
    raw = b"private historical bytes"
    store = SourceStore.create(tmp_path / "live-sources", authority_id="authority-test")
    tenant = "tenant-test"
    journal_service = ActorRef(
        store.authority_id, "journal-service", "service", tenant
    )
    operator = ActorRef(store.authority_id, "operator-user", "human", tenant)
    item = store.capture_source(
        content=raw,
        source_role="human_input",
        tenant_scope_id=tenant,
        originating_surface="restore-test",
    )
    store.grant_access(
        source_ref=item.source_ref,
        principal=journal_service,
        purpose="journal.history_import",
        access_mode="content",
        authorization_fingerprint="1" * 64,
    )
    usage = store.reserve_usage(
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        principal=journal_service,
        purpose="journal.history_import",
        consumer_domain="journal",
        consumer_id="history-file-1",
        use_kind="journal_history_import",
        disclosure_kind="exact_readable_copy",
        redaction_policy="scrub",
        selector={"kind": "whole"},
    )
    store.acknowledge_usage(usage.usage_id)
    store.grant_access(
        source_ref=item.source_ref,
        principal=operator,
        purpose="export",
        access_mode="content",
        authorization_fingerprint="2" * 64,
    )

    journal = tmp_path / "journal.db"
    connection = sqlite3.connect(journal)
    try:
        connection.execute("PRAGMA user_version=7")
        connection.execute(
            "CREATE TABLE journal_import_files("
            "source_ref TEXT,representation_id TEXT,source_usage_id TEXT,"
            "source_usage_state TEXT,raw_sha256 TEXT,byte_length INTEGER)"
        )
        connection.execute(
            "INSERT INTO journal_import_files VALUES(?,?,?,?,?,?)",
            (
                item.source_ref.uri,
                item.primary_representation_id,
                usage.usage_id,
                "acknowledged",
                hashlib.sha256(raw).hexdigest(),
                len(raw),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    checkpoint = create_sensitive_checkpoint(
        tmp_path / "checkpoint",
        journal_db=journal,
        source_store=store,
        source_authorization=ExportAuthorization(operator, "2" * 64),
        idempotency_key="coordinated-restore",
        created_at="2026-08-27T00:00:00+00:00",
    )
    destination = tmp_path / "rehearsed"
    result = rehearse_sensitive_checkpoint_restore(
        checkpoint.path,
        destination,
        source_authorization=ImportAuthorization(
            ActorRef(store.authority_id, "restore-operator", "human", tenant),
            "3" * 64,
            restore_operational_state=True,
        ),
    )

    assert result.source_item_count == result.imported_source_count == 1
    assert result.journal_source_dependency_count == 1
    assert result.journal_source_dependency_gaps == 0
    assert result.journal_sha256 == checkpoint.journal_sha256
    assert result.to_dict()["containsProse"] is False
    assert "private historical bytes" not in json.dumps(result.to_dict())
    restored_sources = SourceStore.open(destination / "sources")
    with restored_sources.connect() as restored:
        restored_usage = restored.execute(
            "SELECT status FROM source_usage_intents WHERE usage_id=?",
            (usage.usage_id,),
        ).fetchone()
        assert restored_usage["status"] == "acknowledged"


def test_restore_rehearsal_requires_explicit_operational_state_authorization(
    tmp_path: Path,
):
    journal = tmp_path / "journal.db"
    _journal(journal)
    store = SourceStore.create(tmp_path / "sources", authority_id="authority-test")
    principal = ActorRef(store.authority_id, "operator-user", "human", "tenant-test")
    checkpoint = create_sensitive_checkpoint(
        tmp_path / "checkpoint",
        journal_db=journal,
        source_store=store,
        source_authorization=ExportAuthorization(principal, "4" * 64),
        idempotency_key="restore-auth-required",
    )
    destination = tmp_path / "rehearsed"

    with pytest.raises(SensitiveBackupError, match="operational Source-state"):
        rehearse_sensitive_checkpoint_restore(
            checkpoint.path,
            destination,
            source_authorization=ImportAuthorization(principal, "5" * 64),
        )
    assert not destination.exists()


def test_windows_private_acl_uses_current_user_only_and_hides_path_from_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "sensitive checkpoint"
    target.mkdir()
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sensitive.subprocess, "run", run)

    sensitive._restrict_windows_directory(target)

    assert captured["command"][:4] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
    ]
    assert str(target) not in " ".join(captured["command"])
    assert captured["env"]["WORK_BUDDY_SENSITIVE_DIRECTORY"] == str(
        target.resolve()
    )
    encoded = captured["command"][-1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "WindowsIdentity]::GetCurrent()" in script
    assert "Assert-UserOnlyAcl" in script


def test_windows_private_acl_fails_closed_when_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "sensitive"
    target.mkdir()
    monkeypatch.setattr(
        sensitive.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(SensitiveBackupError, match="verify"):
        sensitive._restrict_windows_directory(target)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_private_acl_applies_to_existing_sensitive_member(tmp_path: Path):
    target = tmp_path / "sensitive"
    target.mkdir()
    member = target / "sources.jsonl"
    member.write_text("private", encoding="utf-8")

    sensitive._restrict_windows_directory(target)

    assert member.read_text(encoding="utf-8") == "private"
