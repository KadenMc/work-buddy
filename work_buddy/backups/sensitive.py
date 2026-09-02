"""Authorized local checkpoints for Journal and Sources content.

The rolling GitHub backup deliberately excludes these resources.  This module
creates a separate, local-only directory whose Sources member is produced by
the content-aware export contract and whose Journal member uses SQLite's hot
backup API.  It is never uploaded by :mod:`work_buddy.backups.remote`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.sources import (
    ExportAuthorization,
    ImportAuthorization,
    SourceRef,
    SourceStore,
    export_sources,
    import_sources,
)


SENSITIVE_MANIFEST = "SENSITIVE-MANIFEST.json"
SOURCES_MEMBER = "sources.jsonl"
JOURNAL_MEMBER = "journal_capture.db"
SENSITIVE_SCHEMA = "wb.sensitive-backup/v1"


class SensitiveBackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SensitiveCheckpoint:
    path: Path
    checkpoint_id: str
    manifest_sha256: str
    journal_sha256: str
    sources_sha256: str
    source_item_count: int
    source_export_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SENSITIVE_SCHEMA,
            "checkpointId": self.checkpoint_id,
            "path": str(self.path),
            "manifestSha256": self.manifest_sha256,
            "journalSha256": self.journal_sha256,
            "sourcesSha256": self.sources_sha256,
            "sourceItemCount": self.source_item_count,
            "sourceExportId": self.source_export_id,
            "remoteEligible": False,
        }


@dataclass(frozen=True, slots=True)
class SensitiveRestoreRehearsal:
    """Content-free receipt for an isolated coordinated restore rehearsal."""

    path: Path
    checkpoint_id: str
    journal_sha256: str
    sources_sha256: str
    journal_user_version: int
    source_item_count: int
    imported_source_count: int
    journal_source_dependency_count: int
    journal_source_dependency_gaps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "wb.sensitive-restore-rehearsal/v1",
            "path": str(self.path),
            "checkpointId": self.checkpoint_id,
            "journalSha256": self.journal_sha256,
            "sourcesSha256": self.sources_sha256,
            "journalUserVersion": self.journal_user_version,
            "sourceItemCount": self.source_item_count,
            "importedSourceCount": self.imported_source_count,
            "journalSourceDependencyCount": self.journal_source_dependency_count,
            "journalSourceDependencyGaps": self.journal_source_dependency_gaps,
            "containsProse": False,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedSourceExport:
    """Receipt for a Sources archive produced by the guarded operator."""

    path: Path
    sha256: str
    export_id: str
    item_count: int
    issued_copy_count: int

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise SensitiveBackupError("Sources export digest is invalid")
        if not self.export_id or self.item_count < 0 or self.issued_copy_count < 0:
            raise SensitiveBackupError("Sources export receipt is invalid")


def create_sensitive_checkpoint(
    destination: str | Path,
    *,
    journal_db: str | Path,
    source_store: SourceStore,
    source_authorization: ExportAuthorization,
    idempotency_key: str,
    created_at: str | None = None,
) -> SensitiveCheckpoint:
    """Create or replay one exact authorized checkpoint directory."""

    root = Path(destination).expanduser().resolve()
    manifest_path = root / SENSITIVE_MANIFEST
    request = {
        "schema": SENSITIVE_SCHEMA,
        "destination": str(root),
        "journal_db": str(Path(journal_db).expanduser().resolve()),
        "source_authorization_fingerprint": source_authorization.authorization_fingerprint,
        "include_source_content": source_authorization.include_content,
        "idempotency_key": idempotency_key,
    }
    request_sha256 = _sha256_json(request)
    checkpoint_id = hashlib.sha256(
        f"sensitive-checkpoint\0{idempotency_key}\0{request_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    if manifest_path.is_file():
        checkpoint = verify_sensitive_checkpoint(root)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("requestSha256") != request_sha256:
            raise SensitiveBackupError("sensitive checkpoint destination was reused")
        return checkpoint
    if root.exists() and any(root.iterdir()):
        raise SensitiveBackupError("sensitive checkpoint destination is not empty")
    root.mkdir(parents=True, exist_ok=True)
    _restrict_directory(root)

    journal_source = Path(journal_db).expanduser().resolve()
    if not journal_source.is_file():
        raise SensitiveBackupError("journal database is unavailable")
    journal_target = root / JOURNAL_MEMBER
    _hot_backup(journal_source, journal_target)
    _verify_sqlite(journal_target)

    sources_target = root / SOURCES_MEMBER
    source_result = export_sources(
        source_store,
        sources_target,
        authorization=source_authorization,
        idempotency_key=f"sensitive-checkpoint:{idempotency_key}",
    )
    journal_sha = _sha256_file(journal_target)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "schema": SENSITIVE_SCHEMA,
        "checkpointId": checkpoint_id,
        "createdAt": timestamp,
        "requestSha256": request_sha256,
        "remoteEligible": False,
        "privacy": "authorized_sensitive_export",
        "members": {
            "journal": {
                "path": JOURNAL_MEMBER,
                "sha256": journal_sha,
                "userVersion": _user_version(journal_target),
            },
            "sources": {
                "path": SOURCES_MEMBER,
                "sha256": source_result.sha256,
                "exportId": source_result.export_id,
                "itemCount": source_result.item_count,
                "issuedCopyCount": len(source_result.usage_ids),
            },
        },
    }
    manifest_bytes = (_canonical_json(payload) + "\n").encode("utf-8")
    atomic_write_bytes(manifest_path, manifest_bytes)
    return SensitiveCheckpoint(
        path=root,
        checkpoint_id=checkpoint_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        journal_sha256=journal_sha,
        sources_sha256=source_result.sha256,
        source_item_count=source_result.item_count,
        source_export_id=source_result.export_id,
    )


def create_sensitive_checkpoint_from_authorized_export(
    destination: str | Path,
    *,
    journal_db: str | Path,
    source_export: AuthorizedSourceExport,
    idempotency_key: str,
    created_at: str | None = None,
) -> SensitiveCheckpoint:
    """Add a Journal hot snapshot beside an existing guarded Sources export.

    The Sources archive must already be a direct child of ``destination``.
    It is neither copied nor renamed: doing either would create an issued
    offline copy outside the Sources export ledger.
    """

    root = Path(destination).expanduser().resolve()
    source_path = source_export.path.expanduser().resolve()
    if source_path.parent != root:
        raise SensitiveBackupError(
            "authorized Sources export must already be inside the checkpoint"
        )
    _verify_authorized_source_export(source_export)
    manifest_path = root / SENSITIVE_MANIFEST
    journal_source = Path(journal_db).expanduser().resolve()
    request = {
        "schema": SENSITIVE_SCHEMA,
        "destination": str(root),
        "journal_db": str(journal_source),
        "source_export_id": source_export.export_id,
        "source_export_sha256": source_export.sha256,
        "source_member": source_path.name,
        "idempotency_key": idempotency_key,
    }
    request_sha256 = _sha256_json(request)
    checkpoint_id = hashlib.sha256(
        f"sensitive-checkpoint\0{idempotency_key}\0{request_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    if manifest_path.is_file():
        checkpoint = verify_sensitive_checkpoint(root)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("requestSha256") != request_sha256:
            raise SensitiveBackupError("sensitive checkpoint destination was reused")
        return checkpoint
    if not journal_source.is_file():
        raise SensitiveBackupError("journal database is unavailable")
    root.mkdir(parents=True, exist_ok=True)
    _restrict_directory(root)
    journal_target = root / JOURNAL_MEMBER
    if journal_target.exists():
        raise SensitiveBackupError("unsealed Journal checkpoint member already exists")

    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{JOURNAL_MEMBER}.", suffix=".tmp", dir=root
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        _hot_backup(journal_source, temporary)
        _verify_sqlite(temporary)
        os.replace(temporary, journal_target)
    finally:
        temporary.unlink(missing_ok=True)

    journal_sha = _sha256_file(journal_target)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "schema": SENSITIVE_SCHEMA,
        "checkpointId": checkpoint_id,
        "createdAt": timestamp,
        "requestSha256": request_sha256,
        "remoteEligible": False,
        "privacy": "authorized_sensitive_export",
        "members": {
            "journal": {
                "path": JOURNAL_MEMBER,
                "sha256": journal_sha,
                "userVersion": _user_version(journal_target),
            },
            "sources": {
                "path": source_path.name,
                "sha256": source_export.sha256,
                "exportId": source_export.export_id,
                "itemCount": source_export.item_count,
                "issuedCopyCount": source_export.issued_copy_count,
            },
        },
    }
    manifest_bytes = (_canonical_json(payload) + "\n").encode("utf-8")
    atomic_write_bytes(manifest_path, manifest_bytes)
    return SensitiveCheckpoint(
        path=root,
        checkpoint_id=checkpoint_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        journal_sha256=journal_sha,
        sources_sha256=source_export.sha256,
        source_item_count=source_export.item_count,
        source_export_id=source_export.export_id,
    )


def verify_sensitive_checkpoint(destination: str | Path) -> SensitiveCheckpoint:
    """Verify member digests and Journal integrity without exposing content."""

    root = Path(destination).expanduser().resolve()
    manifest_path = root / SENSITIVE_MANIFEST
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise SensitiveBackupError("sensitive checkpoint manifest is unreadable") from exc
    if payload.get("schema") != SENSITIVE_SCHEMA or payload.get("remoteEligible") is not False:
        raise SensitiveBackupError("sensitive checkpoint manifest is unsupported")
    members = payload.get("members")
    if not isinstance(members, dict):
        raise SensitiveBackupError("sensitive checkpoint members are missing")
    journal = _member(root, members, "journal", required_name=JOURNAL_MEMBER)
    sources = _member(root, members, "sources")
    _verify_sqlite(journal)
    journal_row = members["journal"]
    if (
        not isinstance(journal_row.get("userVersion"), int)
        or _user_version(journal) != journal_row["userVersion"]
    ):
        raise SensitiveBackupError("Journal checkpoint schema version mismatch")
    source_row = members["sources"]
    if (
        not isinstance(source_row.get("itemCount"), int)
        or not isinstance(source_row.get("issuedCopyCount"), int)
    ):
        raise SensitiveBackupError("Sources item count is invalid")
    source_receipt = AuthorizedSourceExport(
        path=sources,
        sha256=str(source_row["sha256"]),
        export_id=str(source_row["exportId"]),
        item_count=int(source_row["itemCount"]),
        issued_copy_count=int(source_row["issuedCopyCount"]),
    )
    _verify_authorized_source_export(source_receipt)
    return SensitiveCheckpoint(
        path=root,
        checkpoint_id=str(payload["checkpointId"]),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        journal_sha256=str(members["journal"]["sha256"]),
        sources_sha256=str(source_row["sha256"]),
        source_item_count=int(source_row["itemCount"]),
        source_export_id=source_receipt.export_id,
    )


def rehearse_sensitive_checkpoint_restore(
    checkpoint: str | Path,
    destination: str | Path,
    *,
    source_authorization: ImportAuthorization,
) -> SensitiveRestoreRehearsal:
    """Restore Journal and Sources together under a fresh, isolated root.

    This is intentionally not an in-place restore operator.  The destination
    must not exist, all work is assembled under a sibling temporary directory,
    and the final directory is published only after member, operational-state,
    and Journal-to-Source dependency validation succeeds.
    """

    if not source_authorization.restore_operational_state:
        raise SensitiveBackupError(
            "restore rehearsal requires operational Source-state authorization"
        )
    verified = verify_sensitive_checkpoint(checkpoint)
    checkpoint_root = verified.path
    manifest = json.loads((checkpoint_root / SENSITIVE_MANIFEST).read_bytes())
    journal_member = _member(
        checkpoint_root,
        manifest["members"],
        "journal",
        required_name=JOURNAL_MEMBER,
    )
    sources_member = _member(checkpoint_root, manifest["members"], "sources")
    try:
        with sources_member.open("r", encoding="utf-8") as handle:
            source_manifest = json.loads(handle.readline())
        source_authority_id = str(source_manifest["exporting_authority_id"])
        SourceRef(source_authority_id, "restore-rehearsal-probe")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise SensitiveBackupError("Sources restore archive is invalid") from exc

    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise SensitiveBackupError("restore rehearsal destination must not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
    )
    try:
        restored_journal = staging / JOURNAL_MEMBER
        atomic_write_bytes(restored_journal, journal_member.read_bytes())
        _verify_sqlite(restored_journal)
        restored_sources = SourceStore.create(
            staging / "sources", authority_id=source_authority_id
        )
        imported = import_sources(
            restored_sources,
            sources_member,
            authorization=source_authorization,
        )
        if (
            imported.item_count != verified.source_item_count
            or imported.quarantined_count != 0
            or imported.remapped_count != 0
            or any(
                mapping.authority_id != original.split(":", 1)[0]
                or mapping.item_id != original.split(":", 1)[1]
                for original, mapping in imported.mappings.items()
            )
        ):
            raise SensitiveBackupError("Sources restore rehearsal changed identity")
        dependency_count, dependency_gaps = _journal_source_dependency_parity(
            restored_journal, restored_sources
        )
        if dependency_gaps:
            raise SensitiveBackupError(
                "Journal restore has unavailable Source dependencies"
            )
        os.replace(staging, target)
        staging = target
        return SensitiveRestoreRehearsal(
            path=target,
            checkpoint_id=verified.checkpoint_id,
            journal_sha256=_sha256_file(target / JOURNAL_MEMBER),
            sources_sha256=verified.sources_sha256,
            journal_user_version=_user_version(target / JOURNAL_MEMBER),
            source_item_count=verified.source_item_count,
            imported_source_count=imported.item_count,
            journal_source_dependency_count=dependency_count,
            journal_source_dependency_gaps=dependency_gaps,
        )
    except Exception:
        if staging != target:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _journal_source_dependency_parity(
    journal_path: Path, source_store: SourceStore
) -> tuple[int, int]:
    journal = sqlite3.connect(f"file:{journal_path.as_posix()}?mode=ro", uri=True)
    journal.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in journal.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        dependencies: list[dict[str, Any]] = []
        if "journal_import_files" in tables:
            columns = {
                str(row[1])
                for row in journal.execute("PRAGMA table_info(journal_import_files)")
            }
            usage_state = (
                "source_usage_state" if "source_usage_state" in columns else "NULL"
            )
            usage_id = "source_usage_id" if "source_usage_id" in columns else "NULL"
            for row in journal.execute(
                "SELECT source_ref,representation_id,"
                f"{usage_id} AS source_usage_id,{usage_state} AS dependency_state,"
                "raw_sha256 AS expected_sha256,byte_length AS expected_byte_length "
                "FROM journal_import_files WHERE source_ref IS NOT NULL"
            ):
                dependencies.append(dict(row))
        generic_tables = []
        if "journal_item_revision_source_dependencies" in tables:
            generic_tables.append(
                ("journal_item_revision_source_dependencies", "content_sha256")
            )
        elif "journal_native_source_dependencies" in tables:
            generic_tables.append(
                ("journal_native_source_dependencies", "content_sha256")
            )
        generic_tables.extend(
            (
                ("journal_field_source_dependencies", "value_sha256"),
                ("journal_prompt_input_source_dependencies", "input_sha256"),
                ("journal_prompt_result_source_dependencies", "result_sha256"),
            )
        )
        for table, digest_column in generic_tables:
            if table not in tables:
                continue
            for row in journal.execute(
                "SELECT source_ref,representation_id,source_usage_id,"
                f"state AS dependency_state,{digest_column} AS expected_sha256,"
                f"NULL AS expected_byte_length FROM {table} "
                "WHERE state NOT IN ('released','redaction_committed','aborted')"
            ):
                dependencies.append(dict(row))
    finally:
        journal.close()

    gaps = 0
    source_connection = source_store.connect()
    try:
        for dependency in dependencies:
            try:
                if dependency["dependency_state"] != "acknowledged":
                    gaps += 1
                    continue
                source_ref = SourceRef.parse(str(dependency["source_ref"]))
                representation = source_store._representation_row(
                    source_connection,
                    source_ref,
                    str(dependency["representation_id"]),
                )
                content = source_store._read_representation_row(representation)
                expected_sha = dependency["expected_sha256"]
                expected_length = dependency["expected_byte_length"]
                if expected_sha is not None and hashlib.sha256(content).hexdigest() != str(
                    expected_sha
                ):
                    gaps += 1
                    continue
                if expected_length is not None and len(content) != int(expected_length):
                    gaps += 1
                    continue
                usage = source_connection.execute(
                    "SELECT authority_id,source_item_id,representation_id,status "
                    "FROM source_usage_intents WHERE usage_id=?",
                    (dependency["source_usage_id"],),
                ).fetchone()
                if (
                    usage is None
                    or usage["authority_id"] != source_ref.authority_id
                    or usage["source_item_id"] != source_ref.item_id
                    or usage["representation_id"]
                    != str(dependency["representation_id"])
                    or usage["status"] != "acknowledged"
                ):
                    gaps += 1
            except Exception:
                gaps += 1
    finally:
        source_connection.close()
    return len(dependencies), gaps


def verify_sensitive_journal_source_dependencies(
    journal_db: str | Path,
    source_store: SourceStore,
) -> dict[str, int | str]:
    """Return content-free parity for every active Journal Source dependency."""

    count, gaps = _journal_source_dependency_parity(
        Path(journal_db).expanduser().resolve(),
        source_store,
    )
    return {
        "schema": "wb.sensitive-journal-source-dependency-parity/v1",
        "count": count,
        "gaps": gaps,
    }


def _member(
    root: Path,
    members: dict[str, Any],
    key: str,
    *,
    required_name: str | None = None,
) -> Path:
    row = members.get(key)
    if not isinstance(row, dict) or not isinstance(row.get("path"), str):
        raise SensitiveBackupError(f"{key} checkpoint member is invalid")
    member_name = str(row["path"])
    if (
        not member_name
        or Path(member_name).name != member_name
        or (required_name is not None and member_name != required_name)
    ):
        raise SensitiveBackupError(f"{key} checkpoint member is invalid")
    target = root / member_name
    if not target.is_file() or _sha256_file(target) != row.get("sha256"):
        raise SensitiveBackupError(f"{key} checkpoint digest mismatch")
    return target


def _verify_authorized_source_export(receipt: AuthorizedSourceExport) -> None:
    path = receipt.path.expanduser().resolve()
    if not path.is_file() or _sha256_file(path) != receipt.sha256:
        raise SensitiveBackupError("authorized Sources export digest mismatch")
    try:
        with path.open("r", encoding="utf-8") as handle:
            header = json.loads(handle.readline())
            record_count = sum(1 for _ in handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SensitiveBackupError("authorized Sources export is unreadable") from exc
    if (
        header.get("record_type") != "manifest"
        or header.get("export_id") != receipt.export_id
        or header.get("include_content") is not True
        or header.get("item_count") != receipt.item_count
        or record_count != receipt.item_count
        or receipt.issued_copy_count != receipt.item_count
    ):
        raise SensitiveBackupError("authorized Sources export receipt mismatch")


def _hot_backup(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _verify_sqlite(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = [tuple(row) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
    finally:
        conn.close()
    if integrity != [("ok",)] or foreign_keys:
        raise SensitiveBackupError("journal checkpoint failed SQLite integrity checks")


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _restrict_directory(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_directory(path)
        return
    path.chmod(0o700)


def _restrict_windows_directory(path: Path) -> None:
    """Replace inherited ACLs with one inheritable current-user allow rule.

    Sensitive checkpoints contain readable Journal and Sources material.  A
    best-effort chmod is not an ACL operation on Windows, so failure here must
    stop the checkpoint before another private member is written.
    """

    script = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('WORK_BUDDY_SENSITIVE_DIRECTORY')
if ([String]::IsNullOrWhiteSpace($target) -or -not [IO.Directory]::Exists($target)) {
    throw 'sensitive directory is unavailable'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User

function Set-UserOnlyDirectoryAcl([string] $item) {
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void] $security.AddAccessRule($rule)
    [IO.Directory]::SetAccessControl($item, $security)
}

function Set-UserOnlyFileAcl([string] $item) {
    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void] $security.AddAccessRule($rule)
    [IO.File]::SetAccessControl($item, $security)
}

function Assert-UserOnlyAcl([string] $item) {
    if ([IO.Directory]::Exists($item)) {
        $acl = [IO.Directory]::GetAccessControl($item)
    } else {
        $acl = [IO.File]::GetAccessControl($item)
    }
    $rules = @($acl.Access)
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 1) {
        throw 'sensitive ACL is not private'
    }
    $ruleSid = $rules[0].IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]
    )
    if (
        $rules[0].IsInherited -or
        $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $ruleSid.Value -ne $sid.Value -or
        (($rules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) `
            -ne [Security.AccessControl.FileSystemRights]::FullControl)
    ) {
        throw 'sensitive ACL contains an unexpected principal'
    }
}

Set-UserOnlyDirectoryAcl $target
$children = @(Get-ChildItem -LiteralPath $target -Force)
foreach ($child in $children) {
    if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'sensitive directory contains a reparse point'
    }
    if ($child.PSIsContainer) {
        Set-UserOnlyDirectoryAcl $child.FullName
    } else {
        Set-UserOnlyFileAcl $child.FullName
    }
    Assert-UserOnlyAcl $child.FullName
}
Assert-UserOnlyAcl $target
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = os.environ.copy()
    environment["WORK_BUDDY_SENSITIVE_DIRECTORY"] = str(path.resolve())
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SensitiveBackupError(
            "Windows could not apply the private checkpoint ACL"
        ) from exc
    if completed.returncode != 0:
        raise SensitiveBackupError(
            "Windows could not verify the private checkpoint ACL"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "AuthorizedSourceExport",
    "SensitiveBackupError",
    "SensitiveCheckpoint",
    "SensitiveRestoreRehearsal",
    "create_sensitive_checkpoint",
    "create_sensitive_checkpoint_from_authorized_export",
    "rehearse_sensitive_checkpoint_restore",
    "verify_sensitive_checkpoint",
    "verify_sensitive_journal_source_dependencies",
]
