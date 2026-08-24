"""Metadata-only local-file links for registered Co-work documents.

The linked bytes remain under a server-owned, local root.  Documents and HTTP
responses carry only opaque ``wb-local-file:`` identifiers and display
metadata; no endpoint accepts a filesystem path or returns file bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import unquote

from flask import Blueprint, jsonify, request

from work_buddy import paths
from work_buddy.cowork.api import cowork_mutation_context_sha256
from work_buddy.cowork.folder_api import (
    MAX_FOLDER_PATH_CHARS,
    PICKER_INTENT_HEADER,
    _contained_picker_selection,
    _has_local_picker_intent,
    _is_direct_loopback_request,
)
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.project_store import FolderLifecycleError
from work_buddy.dashboard import local_identity_api
from work_buddy.security.local_identity import LocalIdentityError
from work_buddy.truth import documents
from work_buddy.truth.registry import TruthStoreRegistry


LOCAL_FILE_URI_PREFIX = "wb-local-file:"
LOCAL_FILE_OPEN_INTENT = "cowork-local-file-open"
LOCAL_FILE_REVEAL_INTENT = "cowork-local-file-reveal"
LOCAL_FILE_POLICY_REVISION = 1
MAX_LINKED_FILE_BYTES = 256 * 1024 * 1024
MAX_DISPLAY_NAME_CHARS = 512
MAX_MEDIA_TYPE_CHARS = 200
MAX_SENSITIVITY_CHARS = 80

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ALLOWED_SUFFIX_ACTIONS = {".pdf": "open", ".ppk": "reveal"}
_ACTIVE_ROOT_STATUSES = frozenset({"active"})


class LocalFileLinkError(RuntimeError):
    """Typed local-file failure with a path-free HTTP representation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class LocalFileRootBinding:
    root_id: str
    root: Path
    policy_revision: int


@dataclass(frozen=True, slots=True)
class LocalFileLink:
    link_id: str
    task_id: str | None
    store_id: str
    document_id: str
    root_id: str
    relative_path: str
    display_name: str
    suffix: str
    media_type: str
    byte_length: int
    sha256: str
    sensitivity: str
    allowed_action: str
    policy_revision: int
    source_receipt_id: str
    created_at: str

    @property
    def href(self) -> str:
        return f"{LOCAL_FILE_URI_PREFIX}{self.link_id}"


@dataclass(frozen=True, slots=True)
class LocalFileStatus:
    link: LocalFileLink
    availability: str

    def public_dict(self, *, local_action_available: bool) -> dict[str, Any]:
        link = self.link
        return {
            "link_id": link.link_id,
            "href": link.href,
            "display_name": link.display_name,
            "suffix": link.suffix,
            "media_type": link.media_type,
            "byte_length": link.byte_length,
            "sensitivity": link.sensitivity,
            "allowed_action": link.allowed_action,
            "availability": self.availability,
            "local_action_available": (
                local_action_available and self.availability == "verified"
            ),
        }


class LocalFileOsActions(Protocol):
    """The only seam permitted to hand a verified path to the host OS."""

    def open_pdf(self, path: Path) -> None: ...

    def reveal(self, path: Path) -> None: ...


class DefaultLocalFileOsActions:
    """Fixed-argv, shell-free host actions.

    ``.ppk`` never reaches :meth:`open_pdf`; its only admitted action is a
    containing-folder reveal/select operation.
    """

    @staticmethod
    def _spawn(argv: list[str]) -> None:
        try:
            subprocess.Popen(  # noqa: S603 - argv is fixed and shell is disabled
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            raise LocalFileLinkError(
                "local_file_action_failed",
                "The local file could not be opened by this computer.",
                status=503,
                retryable=True,
            ) from exc

    def open_pdf(self, path: Path) -> None:
        if path.suffix.casefold() != ".pdf":
            raise LocalFileLinkError(
                "unsupported_local_file",
                "Only verified PDF files may be opened.",
                status=422,
            )
        if sys.platform == "win32":
            self._spawn(["explorer.exe", str(path)])
        elif sys.platform == "darwin":
            self._spawn(["open", "--", str(path)])
        else:
            self._spawn(["xdg-open", str(path)])

    def reveal(self, path: Path) -> None:
        if path.suffix.casefold() != ".ppk":
            raise LocalFileLinkError(
                "unsupported_local_file",
                "Only credential-like linked files may be revealed.",
                status=422,
            )
        if sys.platform == "win32":
            self._spawn(["explorer.exe", "/select,", str(path)])
        elif sys.platform == "darwin":
            self._spawn(["open", "-R", "--", str(path)])
        else:
            # Linux has no portable select-file contract.  Opening the exact
            # containing directory reveals the location without opening the
            # credential-like file.
            self._spawn(["xdg-open", str(path.parent)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _validate_opaque_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise LocalFileLinkError(
            "invalid_local_file_link",
            f"A valid {label} is required.",
            status=400,
        )
    return value


def parse_local_file_href(value: Any) -> str | None:
    """Return the opaque ID only for the exact admitted URI shape."""

    if not isinstance(value, str) or not value.startswith(LOCAL_FILE_URI_PREFIX):
        return None
    link_id = value[len(LOCAL_FILE_URI_PREFIX) :]
    return link_id if _OPAQUE_ID.fullmatch(link_id) else None


def _validate_text(value: Any, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise LocalFileLinkError(
            "invalid_local_file_link",
            f"A valid {label} is required.",
            status=400,
        )
    return value


def normalize_local_relative_path(value: Any) -> str:
    """Normalize a migration-seeded path without accepting encoded aliases."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_FOLDER_PATH_CHARS
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked file reference is invalid.",
            status=422,
        )
    # Percent-decoding is never part of the catalog contract.  Reject one- or
    # two-stage encoded separators/traversal instead of trying to guess intent.
    if _PERCENT_ESCAPE.search(value) or unquote(value) != value or unquote(unquote(value)) != value:
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked file reference is invalid.",
            status=422,
        )
    if value.startswith(("/", "\\", "//", "\\\\?\\", "\\\\.\\")):
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked file reference is invalid.",
            status=422,
        )
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or windows.root:
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked file reference is invalid.",
            status=422,
        )
    normalized_slashes = value.replace("\\", "/")
    parts = normalized_slashes.split("/")
    if (
        any(not part or part in {".", ".."} for part in parts)
        or any(":" in part for part in parts)
        or any(part.casefold() == ".wbuddy" for part in parts)
    ):
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked file reference is invalid.",
            status=422,
        )
    return "/".join(parts)


def _canonical_root(value: str | Path) -> Path:
    try:
        raw = os.fspath(value)
        if (
            not isinstance(raw, str)
            or "\x00" in raw
            or len(raw) > MAX_FOLDER_PATH_CHARS
            or raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\"))
        ):
            raise ValueError("invalid root")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("root must be absolute")
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LocalFileLinkError(
            "local_file_root_unavailable",
            "The linked-file root is unavailable.",
            status=409,
            retryable=True,
        ) from exc
    if not root.is_dir():
        raise LocalFileLinkError(
            "local_file_root_unavailable",
            "The linked-file root is unavailable.",
            status=409,
            retryable=True,
        )
    return root


def _from_folder_error(exc: FolderLifecycleError) -> LocalFileLinkError:
    code = (
        "local_file_unavailable"
        if exc.code in {"local_file_unavailable", "local_file_outside_root"}
        else "invalid_local_file_path"
    )
    status = 409 if code == "local_file_unavailable" else 422
    return LocalFileLinkError(
        code,
        "The linked local file is unavailable.",
        status=status,
        retryable=code == "local_file_unavailable",
    )


def _resolve_contained_file(root: Path, relative_path: str) -> Path:
    normalized = normalize_local_relative_path(relative_path)
    try:
        resolved, canonical_relative = _contained_picker_selection(
            root,
            root.joinpath(*normalized.split("/")),
            outside_code="local_file_outside_root",
            unavailable_code="local_file_unavailable",
            invalid_code="invalid_local_file_path",
            managed_code="invalid_local_file_path",
            item_label="the linked local file",
        )
    except FolderLifecycleError as exc:
        raise _from_folder_error(exc) from exc
    if canonical_relative.replace("\\", "/").casefold() != normalized.casefold():
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked local file is unavailable.",
            status=422,
        )
    try:
        info = resolved.lstat()
    except OSError as exc:
        raise LocalFileLinkError(
            "local_file_unavailable",
            "The linked local file is unavailable.",
            status=409,
            retryable=True,
        ) from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
    ):
        raise LocalFileLinkError(
            "invalid_local_file_path",
            "The linked local file is unavailable.",
            status=422,
        )
    return resolved


def _verified_path(
    root: Path,
    link: LocalFileLink,
    *,
    expected_policy_revision: int,
) -> Path:
    if link.policy_revision != expected_policy_revision:
        raise LocalFileLinkError(
            "local_file_policy_changed",
            "The linked local file must be revalidated before it can be used.",
            status=409,
        )
    path = _resolve_contained_file(root, link.relative_path)
    expected_action = _ALLOWED_SUFFIX_ACTIONS.get(path.suffix.casefold())
    if expected_action is None or expected_action != link.allowed_action:
        raise LocalFileLinkError(
            "unsupported_local_file",
            "This linked file type is not supported.",
            status=422,
        )
    try:
        before = path.stat()
    except OSError as exc:
        raise LocalFileLinkError(
            "local_file_unavailable",
            "The linked local file is unavailable.",
            status=409,
            retryable=True,
        ) from exc
    if before.st_size != link.byte_length or before.st_size > MAX_LINKED_FILE_BYTES:
        raise LocalFileLinkError(
            "local_file_changed",
            "The linked local file changed and was not opened.",
            status=409,
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != link.byte_length:
                raise LocalFileLinkError(
                    "local_file_changed",
                    "The linked local file changed and was not opened.",
                    status=409,
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except LocalFileLinkError:
        raise
    except OSError as exc:
        raise LocalFileLinkError(
            "local_file_unavailable",
            "The linked local file is unavailable.",
            status=409,
            retryable=True,
        ) from exc
    if (
        digest.hexdigest() != link.sha256
        or opened.st_size != after.st_size
        or getattr(opened, "st_mtime_ns", None) != getattr(after, "st_mtime_ns", None)
        or getattr(opened, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        raise LocalFileLinkError(
            "local_file_changed",
            "The linked local file changed and was not opened.",
            status=409,
        )
    return path


class LocalFileLinkRegistry:
    """Persistent metadata catalog plus machine-local root bindings."""

    def __init__(
        self,
        catalog_path: str | Path,
        root_bindings_path: str | Path,
    ) -> None:
        self.catalog_path = Path(catalog_path).expanduser().resolve()
        self.root_bindings_path = Path(root_bindings_path).expanduser().resolve()

    @classmethod
    def default(cls) -> "LocalFileLinkRegistry":
        return cls(
            # Task metadata is the canonical portable catalog. Co-work imports
            # no task implementation; it shares only this versioned SQLite
            # contract. The absolute root remains in the separate runtime DB.
            paths.resolve("db/tasks"),
            paths.data_dir("runtime") / "cowork_local_file_roots.db",
        )

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _root_connection(self) -> sqlite3.Connection:
        conn = self._connect(self.root_bindings_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_file_roots (
                root_id TEXT PRIMARY KEY,
                absolute_path TEXT NOT NULL,
                policy_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return conn

    def _catalog_connection(self) -> sqlite3.Connection:
        conn = self._connect(self.catalog_path)
        # The neutral task migration owns these portable tables. Co-work does
        # not run task migrations or invent a parallel schema: it performs a
        # narrow compatibility check and fails closed until task startup has
        # initialized the canonical database.
        required = {
            "task_local_file_roots": {
                "root_id",
                "label",
                "manifest_sha256",
                "policy_revision",
                "status",
                "created_at",
                "updated_at",
            },
            "task_local_file_links": {
                "link_id",
                "task_id",
                "store_id",
                "document_id",
                "root_id",
                "relative_path",
                "display_name",
                "suffix",
                "media_type",
                "byte_length",
                "sha256",
                "sensitivity",
                "allowed_action",
                "policy_revision",
                "source_receipt_id",
                "created_at",
            },
        }
        try:
            for table, expected_columns in required.items():
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                columns = (
                    {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
                    if exists is not None
                    else set()
                )
                if not expected_columns.issubset(columns):
                    raise LocalFileLinkError(
                        "local_file_catalog_unavailable",
                        "The linked-file catalog is not initialized.",
                        status=503,
                        retryable=True,
                    )
        except Exception:
            conn.close()
            raise
        return conn

    def register_root(
        self,
        *,
        root_id: str,
        root: str | Path,
        policy_revision: int = LOCAL_FILE_POLICY_REVISION,
        label: str | None = None,
        manifest_sha256: str | None = None,
        status: str | None = None,
    ) -> LocalFileRootBinding:
        root_id = _validate_opaque_id(root_id, label="linked-file root ID")
        if not isinstance(policy_revision, int) or policy_revision < 1:
            raise LocalFileLinkError(
                "invalid_local_file_policy",
                "A valid linked-file policy revision is required.",
                status=400,
            )
        canonical = _canonical_root(root)
        now = _now()
        local_binding_exists = False
        with self._root_connection() as conn:
            existing = conn.execute(
                "SELECT absolute_path, policy_revision FROM local_file_roots WHERE root_id = ?",
                (root_id,),
            ).fetchone()
            if existing is not None:
                try:
                    same_path = os.path.samefile(
                        str(existing["absolute_path"]), canonical
                    )
                except OSError:
                    same_path = False
                if not same_path or int(existing["policy_revision"]) != policy_revision:
                    raise LocalFileLinkError(
                        "local_file_root_conflict",
                        "That linked-file root ID is already bound.",
                        status=409,
                    )
                local_binding_exists = True
        with self._catalog_connection() as conn:
            catalog_root = conn.execute(
                """
                SELECT label, manifest_sha256, policy_revision, status
                FROM task_local_file_roots WHERE root_id = ?
                """,
                (root_id,),
            ).fetchone()
            if catalog_root is None:
                if (
                    label is None
                    or manifest_sha256 is None
                    or not _SHA256.fullmatch(manifest_sha256)
                ):
                    raise LocalFileLinkError(
                        "invalid_local_file_root_metadata",
                        "The root manifest metadata must be registered first.",
                        status=400,
                    )
                label = _validate_text(
                    label, label="root label", maximum=MAX_DISPLAY_NAME_CHARS
                )
                root_status = _validate_text(
                    status or "active", label="root status", maximum=80
                )
                conn.execute(
                    """
                    INSERT INTO task_local_file_roots(
                        root_id, label, manifest_sha256, policy_revision,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        root_id,
                        label,
                        manifest_sha256,
                        policy_revision,
                        root_status,
                        now,
                        now,
                    ),
                )
            elif int(catalog_root["policy_revision"]) != policy_revision:
                raise LocalFileLinkError(
                    "local_file_root_conflict",
                    "That linked-file root has a different policy revision.",
                    status=409,
                )
            else:
                updated_label = (
                    str(catalog_root["label"])
                    if label is None
                    else _validate_text(
                        label, label="root label", maximum=MAX_DISPLAY_NAME_CHARS
                    )
                )
                if manifest_sha256 is None:
                    updated_manifest = str(catalog_root["manifest_sha256"])
                elif _SHA256.fullmatch(manifest_sha256):
                    updated_manifest = manifest_sha256
                else:
                    raise LocalFileLinkError(
                        "invalid_local_file_root_metadata",
                        "A valid root manifest hash is required.",
                        status=400,
                    )
                updated_status = (
                    str(catalog_root["status"])
                    if status is None
                    else _validate_text(status, label="root status", maximum=80)
                )
                conn.execute(
                    """
                    UPDATE task_local_file_roots
                    SET label = ?, manifest_sha256 = ?, status = ?, updated_at = ?
                    WHERE root_id = ?
                    """,
                    (
                        updated_label,
                        updated_manifest,
                        updated_status,
                        now,
                        root_id,
                    ),
                )
        if local_binding_exists:
            return LocalFileRootBinding(root_id, canonical, policy_revision)
        with self._root_connection() as conn:
            conn.execute(
                """
                INSERT INTO local_file_roots(
                    root_id, absolute_path, policy_revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (root_id, str(canonical), policy_revision, now, now),
            )
        return LocalFileRootBinding(root_id, canonical, policy_revision)

    def get_root(self, root_id: str) -> LocalFileRootBinding:
        root_id = _validate_opaque_id(root_id, label="linked-file root ID")
        with self._catalog_connection() as conn:
            catalog = conn.execute(
                """
                SELECT policy_revision, status
                FROM task_local_file_roots WHERE root_id = ?
                """,
                (root_id,),
            ).fetchone()
        if (
            catalog is None
            or str(catalog["status"]) not in _ACTIVE_ROOT_STATUSES
        ):
            raise LocalFileLinkError(
                "local_file_root_unavailable",
                "The linked-file root is unavailable.",
                status=409,
                retryable=True,
            )
        with self._root_connection() as conn:
            row = conn.execute(
                "SELECT absolute_path, policy_revision FROM local_file_roots WHERE root_id = ?",
                (root_id,),
            ).fetchone()
        if row is None:
            raise LocalFileLinkError(
                "local_file_root_unavailable",
                "The linked-file root is unavailable.",
                status=409,
                retryable=True,
            )
        if int(row["policy_revision"]) != int(catalog["policy_revision"]):
            raise LocalFileLinkError(
                "local_file_policy_changed",
                "The linked-file root must be revalidated before it can be used.",
                status=409,
            )
        return LocalFileRootBinding(
            root_id=root_id,
            root=_canonical_root(str(row["absolute_path"])),
            policy_revision=int(row["policy_revision"]),
        )

    @staticmethod
    def _row_to_link(row: sqlite3.Row) -> LocalFileLink:
        return LocalFileLink(**dict(row))

    def register_link(
        self,
        *,
        link_id: str,
        task_id: str | None,
        store_id: str,
        document_id: str,
        root_id: str,
        relative_path: str,
        display_name: str,
        suffix: str,
        media_type: str,
        byte_length: int,
        sha256: str,
        sensitivity: str,
        allowed_action: str,
        source_receipt_id: str,
        policy_revision: int = LOCAL_FILE_POLICY_REVISION,
        created_at: str | None = None,
    ) -> LocalFileLink:
        link_id = _validate_opaque_id(link_id, label="local-file link ID")
        store_id = _validate_opaque_id(store_id, label="store ID")
        document_id = _validate_opaque_id(document_id, label="document ID")
        root_id = _validate_opaque_id(root_id, label="linked-file root ID")
        relative_path = normalize_local_relative_path(relative_path)
        if not isinstance(policy_revision, int) or policy_revision < 1:
            raise LocalFileLinkError(
                "invalid_local_file_policy",
                "A valid linked-file policy revision is required.",
                status=400,
            )
        display_name = _validate_text(
            display_name, label="display name", maximum=MAX_DISPLAY_NAME_CHARS
        )
        media_type = _validate_text(
            media_type, label="media type", maximum=MAX_MEDIA_TYPE_CHARS
        )
        sensitivity = _validate_text(
            sensitivity, label="sensitivity", maximum=MAX_SENSITIVITY_CHARS
        )
        source_receipt_id = _validate_text(
            source_receipt_id, label="source receipt ID", maximum=200
        )
        normalized_suffix = str(suffix).casefold()
        expected_action = _ALLOWED_SUFFIX_ACTIONS.get(normalized_suffix)
        if (
            expected_action is None
            or allowed_action != expected_action
            or Path(relative_path).suffix.casefold() != normalized_suffix
        ):
            raise LocalFileLinkError(
                "unsupported_local_file",
                "This linked file type or action is not supported.",
                status=422,
            )
        if (
            not isinstance(byte_length, int)
            or isinstance(byte_length, bool)
            or byte_length < 0
            or byte_length > MAX_LINKED_FILE_BYTES
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise LocalFileLinkError(
                "invalid_local_file_integrity",
                "Valid linked-file size and hash metadata are required.",
                status=422,
            )
        root = self.get_root(root_id)
        link = LocalFileLink(
            link_id=link_id,
            task_id=task_id,
            store_id=store_id,
            document_id=document_id,
            root_id=root_id,
            relative_path=relative_path,
            display_name=display_name,
            suffix=normalized_suffix,
            media_type=media_type,
            byte_length=byte_length,
            sha256=sha256,
            sensitivity=sensitivity,
            allowed_action=allowed_action,
            policy_revision=policy_revision,
            source_receipt_id=source_receipt_id,
            created_at=created_at or _now(),
        )
        _verified_path(
            root.root,
            link,
            expected_policy_revision=root.policy_revision,
        )
        values = asdict(link)
        columns = tuple(values)
        with self._catalog_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM task_local_file_links WHERE link_id = ?",
                (link_id,),
            ).fetchone()
            if existing is not None:
                existing_link = self._row_to_link(existing)
                if created_at is None:
                    link = replace(link, created_at=existing_link.created_at)
                if existing_link != link:
                    raise LocalFileLinkError(
                        "local_file_link_conflict",
                        "That local-file link ID is already registered.",
                        status=409,
                    )
                return existing_link
            try:
                conn.execute(
                    f"INSERT INTO task_local_file_links ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            except sqlite3.IntegrityError as exc:
                raise LocalFileLinkError(
                    "local_file_link_conflict",
                    "That document file is already registered.",
                    status=409,
                ) from exc
        return link

    def list_document_links(
        self, *, store_id: str, document_id: str
    ) -> tuple[LocalFileLink, ...]:
        store_id = _validate_opaque_id(store_id, label="store ID")
        document_id = _validate_opaque_id(document_id, label="document ID")
        with self._catalog_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_local_file_links
                WHERE store_id = ? AND document_id = ?
                ORDER BY created_at, link_id
                """,
                (store_id, document_id),
            ).fetchall()
        return tuple(self._row_to_link(row) for row in rows)

    def get_document_link(
        self, *, store_id: str, document_id: str, link_id: str
    ) -> LocalFileLink:
        store_id = _validate_opaque_id(store_id, label="store ID")
        document_id = _validate_opaque_id(document_id, label="document ID")
        link_id = _validate_opaque_id(link_id, label="local-file link ID")
        with self._catalog_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM task_local_file_links
                WHERE store_id = ? AND document_id = ? AND link_id = ?
                """,
                (store_id, document_id, link_id),
            ).fetchone()
        if row is None:
            raise LocalFileLinkError(
                "local_file_link_not_found",
                "That linked local file is not registered for this document.",
                status=404,
            )
        return self._row_to_link(row)

    def inspect(self, link: LocalFileLink) -> LocalFileStatus:
        try:
            root = self.get_root(link.root_id)
            _verified_path(
                root.root,
                link,
                expected_policy_revision=root.policy_revision,
            )
        except LocalFileLinkError as exc:
            if exc.code == "local_file_changed":
                availability = "changed"
            elif exc.code == "local_file_policy_changed":
                availability = "policy_changed"
            else:
                availability = "unavailable"
            return LocalFileStatus(link, availability)
        return LocalFileStatus(link, "verified")

    def verified_path(self, link: LocalFileLink) -> Path:
        root = self.get_root(link.root_id)
        return _verified_path(
            root.root,
            link,
            expected_policy_revision=root.policy_revision,
        )


DocumentMembership = Callable[[str, str], bool]
HumanAuthority = Callable[[str, str, str, Mapping[str, Any]], None]


def _default_document_membership(store_id: str, document_id: str) -> bool:
    """Prove this is a live registered document, never a browser scratch."""

    try:
        registry = TruthStoreRegistry()
        row = registry.get_by_store_id(store_id, refresh=True)
        if row is None or not row.reachable or not row.document_surface_enabled:
            return False
        store = registry.open_store(store_id)
        document = documents.get_document(store, document_id)
        return (
            documents.current_lifecycle(store, document.id) == "active"
            and document_surface_allowed(store, document)
        )
    except Exception:
        return False


def _default_human_authority(
    operation: str,
    store_id: str,
    document_id: str,
    body: Mapping[str, Any],
) -> None:
    local_identity_api.require_human_authority_request(
        action=f"cowork.{operation}",
        subject=f"cowork-document:{store_id}:{document_id}",
        context_sha256=cowork_mutation_context_sha256(
            operation=operation,
            store_id=store_id,
            document_id=document_id,
            body=body,
        ),
    )


def _error(exc: LocalFileLinkError):
    response = jsonify(
        {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        }
    )
    response.status_code = exc.status
    response.headers["Cache-Control"] = "no-store"
    return response


def create_local_file_blueprint(
    *,
    registry_factory: Callable[[], LocalFileLinkRegistry] = LocalFileLinkRegistry.default,
    document_membership: DocumentMembership = _default_document_membership,
    human_authority: HumanAuthority = _default_human_authority,
    os_actions: LocalFileOsActions | None = None,
) -> Blueprint:
    """Build the metadata read and exact local activation routes."""

    actions = os_actions or DefaultLocalFileOsActions()
    blueprint = Blueprint(f"cowork_local_files_{uuid.uuid4().hex}", __name__)

    @blueprint.get("/api/truth/doc/<document_id>/local-files")
    def list_local_files(document_id: str):
        store_id = request.args.get("store_id", "")
        try:
            if not document_membership(store_id, document_id):
                raise LocalFileLinkError(
                    "local_file_document_unavailable",
                    "Linked local files are unavailable for this document.",
                    status=404,
                )
            registry = registry_factory()
            local = _is_direct_loopback_request()
            links = [
                registry.inspect(link).public_dict(local_action_available=local)
                for link in registry.list_document_links(
                    store_id=store_id,
                    document_id=document_id,
                )
            ]
            response = jsonify({"ok": True, "links": links})
            response.headers["Cache-Control"] = "no-store"
            return response
        except LocalFileLinkError as exc:
            return _error(exc)

    @blueprint.post(
        "/api/truth/doc/<document_id>/local-files/<link_id>/activate"
    )
    def activate_local_file(document_id: str, link_id: str):
        store_id = request.args.get("store_id", "")
        value = request.get_json(silent=True)
        if not isinstance(value, Mapping):
            return _error(
                LocalFileLinkError(
                    "invalid_local_file_action",
                    "The local-file action request is invalid.",
                    status=400,
                )
            )
        body = dict(value)
        requested_action = body.get("action")
        expected_intent = {
            "open": LOCAL_FILE_OPEN_INTENT,
            "reveal": LOCAL_FILE_REVEAL_INTENT,
        }.get(requested_action)
        try:
            if expected_intent is None or body.get("link_id") != link_id:
                raise LocalFileLinkError(
                    "invalid_local_file_action",
                    "The local-file action request is invalid.",
                    status=400,
                )
            if not _has_local_picker_intent(expected_intent):
                raise LocalFileLinkError(
                    "local_file_action_forbidden",
                    "Local file actions are available only from this computer.",
                    status=403,
                )
            if not document_membership(store_id, document_id):
                raise LocalFileLinkError(
                    "local_file_document_unavailable",
                    "Linked local files are unavailable for this document.",
                    status=404,
                )
            registry = registry_factory()
            link = registry.get_document_link(
                store_id=store_id,
                document_id=document_id,
                link_id=link_id,
            )
            if link.allowed_action != requested_action:
                raise LocalFileLinkError(
                    "local_file_action_forbidden",
                    "That action is not allowed for this linked file.",
                    status=403,
                )
            operation = f"local_file.{requested_action}"
            human_authority(operation, store_id, document_id, body)
            verified = registry.verified_path(link)
            if requested_action == "open":
                actions.open_pdf(verified)
            else:
                actions.reveal(verified)
            response = jsonify(
                {
                    "ok": True,
                    "link_id": link.link_id,
                    "action": requested_action,
                    "status": "opened" if requested_action == "open" else "revealed",
                }
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except LocalIdentityError as exc:
            return local_identity_api._error(exc)
        except LocalFileLinkError as exc:
            return _error(exc)

    return blueprint


cowork_local_file_blueprint = create_local_file_blueprint()


__all__ = [
    "DefaultLocalFileOsActions",
    "LOCAL_FILE_OPEN_INTENT",
    "LOCAL_FILE_POLICY_REVISION",
    "LOCAL_FILE_REVEAL_INTENT",
    "LOCAL_FILE_URI_PREFIX",
    "LocalFileLink",
    "LocalFileLinkError",
    "LocalFileLinkRegistry",
    "LocalFileOsActions",
    "LocalFileRootBinding",
    "LocalFileStatus",
    "cowork_local_file_blueprint",
    "create_local_file_blueprint",
    "normalize_local_relative_path",
    "parse_local_file_href",
]
