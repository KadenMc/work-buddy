"""Canonical Co-work Folder layout, inspection, and setup.

Inspection helpers in this module are read-only with respect to the selected
Folder. Resumable scan cursors, idempotency receipts, and locks live beneath
Work Buddy's machine data root.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from work_buddy.truth.contracts import StorePaths
from work_buddy.truth.identity import new_id, sha256_bytes
from work_buddy.truth.export import StoreIdentityCollision
from work_buddy.truth.locks import (
    exact_folder_lock,
    folder_operation_locks,
    migration_store_lock as store_write_lock,
)
from work_buddy.truth.profiles import validate_profile
from work_buddy.truth.registry import (
    RegisteredTruthStore,
    RegistryIdentityMismatch,
    TruthStoreRegistry,
)
from work_buddy.truth.store import TruthStore


MANIFEST_FORMAT = "wbuddy-folder/v1"
CANONICAL_LAYOUT = "wbuddy_cowork_v1"
COMPONENT_GITIGNORE_LINES = (
    "/store.db",
    "/store.db-*",
    "/runtime/",
    "/blobs/",
)
DEFAULT_SCAN_BUDGET = 2_000
DEFAULT_SCAN_HARD_LIMIT = 50_000
DEFAULT_TOKEN_TTL_SECONDS = 15 * 60
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_SKIP_DIRS = frozenset(
    {".git", ".wbuddy", "node_modules", ".venv", "vendor"}
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class FolderLifecycleError(RuntimeError):
    """Typed failure suitable for the lifecycle API error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class StoreIdentity:
    store_id: str
    profile: str
    title: str
    schema_version: int
    document_surface_enabled: bool
    allowed_document_classes: tuple[str, ...]
    feedback_capture: bool
    document_count: int


@dataclass(frozen=True, slots=True)
class ManifestSnapshot:
    path: Path
    exists: bool
    raw: bytes | None
    sha256: str | None
    data: Mapping[str, Any] | None
    has_cowork: bool


@dataclass(frozen=True, slots=True)
class ScanResult:
    state: str
    continuation_token: str | None = None
    visited_entries: int = 0
    nested_folders: tuple[str, ...] = ()
    retry_after_ms: int | None = None


@dataclass(frozen=True, slots=True)
class FolderInspection:
    status: str
    folder_path: Path
    folder_name: str
    layout: str | None = None
    store_id: str | None = None
    reason_code: str | None = None
    actions: tuple[str, ...] = ()
    owner_path: Path | None = None
    owner_store_id: str | None = None
    fingerprint: str | None = None
    continuation_token: str | None = None
    progress: Mapping[str, int] | None = None
    retry_after_ms: int | None = None
    boundaries: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["folder_path"] = str(self.folder_path)
        if self.owner_path is not None:
            value["owner_path"] = str(self.owner_path)
            value["owner"] = {
                "folder_name": self.owner_path.name,
                "folder_path": str(self.owner_path),
                "store_id": self.owner_store_id,
            }
        value["actions"] = list(self.actions)
        if self.boundaries:
            value["boundaries"] = [dict(boundary) for boundary in self.boundaries]
        else:
            value.pop("boundaries", None)
        return {key: item for key, item in value.items() if item is not None}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _canonical_folder(folder: str | Path) -> Path:
    try:
        resolved = Path(folder).expanduser().resolve(strict=True)
    except OSError as exc:
        raise FolderLifecycleError(
            "folder_not_found", "The selected Folder does not exist.", status=404
        ) from exc
    if not resolved.is_dir():
        raise FolderLifecycleError(
            "folder_not_found", "The selected path is not a Folder.", status=404
        )
    return resolved


def _managed_path_error(root: Path, path: Path) -> FolderLifecycleError:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return FolderLifecycleError(
        "folder_layout_incomplete",
        "The Folder contains redirected or unsupported Work Buddy data.",
        details={"managed_path": relative},
    )


def _lstat_managed(root: Path, path: Path) -> os.stat_result | None:
    """Read metadata without following a managed path redirection."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FolderLifecycleError(
            "folder_unreachable",
            "The Folder's Work Buddy data could not be inspected safely.",
            status=503,
            retryable=True,
        ) from exc
    if stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise _managed_path_error(root, path)
    return info


def _assert_managed_file_safe(root: Path, path: Path) -> None:
    info = _lstat_managed(root, path)
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise _managed_path_error(root, path)


def _assert_managed_tree_safe(
    root: Path,
    path: Path,
    *,
    root_device: int,
) -> None:
    """Reject redirects and non-plain entries without traversing through them."""

    info = _lstat_managed(root, path)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode) or (
        root_device and info.st_dev and info.st_dev != root_device
    ):
        raise _managed_path_error(root, path)

    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    try:
                        child_info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # A concurrently removed entry cannot redirect an
                        # operation. The caller revalidates again immediately
                        # before every publication.
                        continue
                    except OSError as exc:
                        raise FolderLifecycleError(
                            "folder_unreachable",
                            "The Folder's Work Buddy data could not be inspected safely.",
                            status=503,
                            retryable=True,
                        ) from exc
                    if entry.is_symlink() or stat.S_ISLNK(child_info.st_mode) or bool(
                        getattr(child_info, "st_file_attributes", 0)
                        & _REPARSE_POINT
                    ):
                        raise _managed_path_error(root, child)
                    if (
                        root_device
                        and child_info.st_dev
                        and child_info.st_dev != root_device
                    ):
                        raise _managed_path_error(root, child)
                    if stat.S_ISDIR(child_info.st_mode):
                        pending.append(child)
                    elif not stat.S_ISREG(child_info.st_mode):
                        raise _managed_path_error(root, child)
        except FolderLifecycleError:
            raise
        except OSError as exc:
            raise FolderLifecycleError(
                "folder_unreachable",
                "The Folder's Work Buddy data could not be inspected safely.",
                status=503,
                retryable=True,
            ) from exc


def _assert_manifest_path_safe(root: Path) -> None:
    root_info = root.stat()
    wbuddy = root / ".wbuddy"
    wbuddy_info = _lstat_managed(root, wbuddy)
    if wbuddy_info is None:
        return
    if not stat.S_ISDIR(wbuddy_info.st_mode) or (
        root_info.st_dev
        and wbuddy_info.st_dev
        and wbuddy_info.st_dev != root_info.st_dev
    ):
        raise _managed_path_error(root, wbuddy)
    _assert_managed_file_safe(root, wbuddy / "manifest.yaml")


def _assert_managed_layout_safe(root: Path) -> None:
    """Validate every path Co-work may read, move, create beneath, or delete.

    ``Path.resolve`` and ordinary ``exists``/``is_file`` calls follow links.
    This check deliberately uses ``lstat``/``scandir(..., follow_symlinks=False)``
    first, including Windows' reparse-point attribute, so a junction or
    symlink cannot turn a selected Folder operation into an external write.
    """

    root_info = root.stat()
    _assert_manifest_path_safe(root)
    _assert_managed_tree_safe(
        root,
        root / ".wbuddy" / "cowork",
        root_device=root_info.st_dev,
    )


def _managed_sidecar_root(sidecar: str | Path) -> tuple[Path, Path]:
    """Return lexical sidecar/root paths without resolving through a link."""

    lexical = Path(os.path.abspath(os.fspath(Path(sidecar).expanduser())))
    if lexical.name == "cowork" and lexical.parent.name == ".wbuddy":
        return lexical, lexical.parent.parent
    raise FolderLifecycleError(
        "folder_layout_incomplete",
        "The Co-work data path is outside a recognized Folder layout.",
    )


def _yaml_mapping(raw: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FolderLifecycleError(
            "folder_layout_incomplete", f"{label} is not valid UTF-8 YAML."
        ) from exc
    if not isinstance(value, Mapping):
        raise FolderLifecycleError(
            "folder_layout_incomplete", f"{label} must contain a YAML mapping."
        )
    return value


def read_manifest(folder: str | Path) -> ManifestSnapshot:
    root = Path(folder).expanduser().resolve()
    _assert_manifest_path_safe(root)
    path = root / ".wbuddy" / "manifest.yaml"
    if not path.exists():
        return ManifestSnapshot(path, False, None, None, None, False)
    if not path.is_file():
        raise FolderLifecycleError(
            "folder_layout_incomplete", ".wbuddy/manifest.yaml is not a file."
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FolderLifecycleError(
            "folder_unreachable",
            "The Work Buddy Folder manifest could not be read.",
            status=503,
            retryable=True,
        ) from exc
    data = _yaml_mapping(raw, label=".wbuddy/manifest.yaml")
    if data.get("format") != MANIFEST_FORMAT:
        raise FolderLifecycleError(
            "folder_layout_incomplete",
            f".wbuddy/manifest.yaml must declare format: {MANIFEST_FORMAT}.",
        )
    components = data.get("components")
    if not isinstance(components, Mapping):
        raise FolderLifecycleError(
            "folder_layout_incomplete", "The manifest components value must be a mapping."
        )
    has_cowork = "cowork" in components
    cowork = components.get("cowork")
    if has_cowork:
        if not isinstance(cowork, Mapping) or cowork.get("path") != "cowork":
            raise FolderLifecycleError(
                "folder_layout_incomplete",
                "The manifest cowork component must point to the cowork child.",
            )
    return ManifestSnapshot(
        path=path,
        exists=True,
        raw=raw,
        sha256=sha256_bytes(raw),
        data=data,
        has_cowork=has_cowork,
    )


def _patched_manifest_bytes(snapshot: ManifestSnapshot) -> bytes:
    if not snapshot.exists:
        return (
            f"format: {MANIFEST_FORMAT}\ncomponents:\n"
            "  cowork:\n    path: cowork\n"
        ).encode("utf-8")
    assert snapshot.raw is not None and snapshot.data is not None
    if snapshot.has_cowork:
        return snapshot.raw

    bom = snapshot.raw.startswith(b"\xef\xbb\xbf")
    text = snapshot.raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    key_pattern = re.compile(r"^(?:components|'components'|\"components\")\s*:")
    matches = [index for index, line in enumerate(lines) if key_pattern.match(line)]
    if len(matches) != 1:
        raise FolderLifecycleError(
            "manifest_patch_unsupported",
            "The existing manifest uses a components form that cannot be patched losslessly.",
        )
    index = matches[0]
    line = lines[index]
    prefix, _, remainder = line.partition(":")
    if prefix[:1].isspace():
        raise FolderLifecycleError(
            "manifest_patch_unsupported", "The manifest components key must be top-level."
        )
    content = remainder.rstrip("\r\n")

    if "{" in content:
        close = content.rfind("}")
        open_index = content.find("{")
        if close < open_index:
            raise FolderLifecycleError(
                "manifest_patch_unsupported", "The flow-style components map is ambiguous."
            )
        inner = content[open_index + 1 : close]
        insertion = "cowork: {path: cowork}" if not inner.strip() else ", cowork: {path: cowork}"
        updated_content = content[:close] + insertion + content[close:]
        suffix = line[len(line.rstrip("\r\n")) :]
        lines[index] = f"{prefix}:{updated_content}{suffix}"
    else:
        if content.strip() and not content.lstrip().startswith("#"):
            raise FolderLifecycleError(
                "manifest_patch_unsupported",
                "The manifest components key uses unsupported YAML metadata.",
            )
        end = index + 1
        child_indent = 2
        child_indent_found = False
        while end < len(lines):
            candidate = lines[end]
            stripped = candidate.strip()
            indent = len(candidate) - len(candidate.lstrip(" "))
            if stripped and not stripped.startswith("#") and indent == 0:
                break
            if (
                stripped
                and not stripped.startswith("#")
                and indent > 0
                and not child_indent_found
            ):
                child_indent = indent
                child_indent_found = True
            end += 1
        if lines and not lines[end - 1].endswith(("\n", "\r")):
            lines[end - 1] = lines[end - 1] + newline
        indent_text = " " * child_indent
        insertion = (
            f"{indent_text}cowork:{newline}"
            f"{indent_text}  path: cowork{newline}"
        )
        lines.insert(end, insertion)

    rendered = "".join(lines)
    patched = (("\ufeff" if bom else "") + rendered).encode("utf-8")
    parsed = _yaml_mapping(patched, label="patched .wbuddy/manifest.yaml")
    expected = copy.deepcopy(dict(snapshot.data))
    expected_components = copy.deepcopy(dict(expected["components"]))
    expected_components["cowork"] = {"path": "cowork"}
    expected["components"] = expected_components
    if parsed != expected:
        raise FolderLifecycleError(
            "manifest_patch_unsupported",
            "The lossless manifest patch changed data outside the cowork component.",
        )
    return patched


def _atomic_write(path: Path, content: bytes, *, absent_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if absent_only:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(str(path), flags)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def patch_cowork_manifest(
    folder: str | Path,
    *,
    expected_sha256: str | None,
) -> tuple[ManifestSnapshot, bytes]:
    """Add only the cowork component, with a locked byte precondition."""

    snapshot = read_manifest(folder)
    if snapshot.sha256 != expected_sha256:
        raise FolderLifecycleError(
            "folder_changed",
            "The Folder manifest changed after it was inspected.",
            retryable=True,
        )
    patched = _patched_manifest_bytes(snapshot)
    if snapshot.has_cowork:
        return snapshot, patched
    # Re-read immediately before publication. The surrounding exact-Folder
    # lock serializes Work Buddy writers; this byte check catches outside edits.
    if snapshot.exists:
        current = snapshot.path.read_bytes()
        if current != snapshot.raw:
            raise FolderLifecycleError(
                "folder_changed", "The Folder manifest changed during setup.", retryable=True
            )
        _atomic_write(snapshot.path, patched)
    else:
        try:
            _atomic_write(snapshot.path, patched, absent_only=True)
        except FileExistsError as exc:
            raise FolderLifecycleError(
                "folder_changed", "A Folder manifest appeared during setup.", retryable=True
            ) from exc
    return snapshot, patched


def _restore_manifest(snapshot: ManifestSnapshot, published: bytes) -> None:
    _assert_manifest_path_safe(snapshot.path.parent.parent)
    if not snapshot.path.exists() or snapshot.path.read_bytes() != published:
        return
    if snapshot.exists:
        assert snapshot.raw is not None
        _atomic_write(snapshot.path, snapshot.raw)
    else:
        snapshot.path.unlink()


def write_component_gitignore(sidecar: str | Path) -> tuple[bytes | None, bytes]:
    sidecar_path, root = _managed_sidecar_root(sidecar)
    _assert_managed_layout_safe(root)
    path = sidecar_path / ".gitignore"
    original = path.read_bytes() if path.is_file() else None
    newline = b"\r\n" if original and b"\r\n" in original else b"\n"
    rendered = original or b""
    existing = {
        line.strip().decode("utf-8", errors="ignore") for line in rendered.splitlines()
    }
    additions = [line for line in COMPONENT_GITIGNORE_LINES if line not in existing]
    if additions:
        if rendered and not rendered.endswith((b"\n", b"\r")):
            rendered += newline
        rendered += newline.join(item.encode("utf-8") for item in additions) + newline
        _atomic_write(path, rendered, absent_only=original is None)
    return original, rendered


def _read_store_identity(sidecar: Path) -> StoreIdentity:
    sidecar_path, root = _managed_sidecar_root(sidecar)
    _assert_managed_layout_safe(root)
    paths = StorePaths.from_sidecar(sidecar_path)
    if not paths.config.is_file() or not paths.db.is_file():
        raise FolderLifecycleError(
            "folder_layout_incomplete", "Co-work data is missing required files."
        )
    try:
        raw_profile = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
        profile = validate_profile(raw_profile)
    except Exception as exc:
        raise FolderLifecycleError(
            "folder_layout_incomplete", "This Folder's Co-work configuration is invalid."
        ) from exc
    try:
        # ``immutable=1`` prevents SQLite from creating or updating WAL/SHM
        # sidecars during a browse/inspect request. A mutating open is
        # deliberately deferred until the explicit open action.
        uri = paths.db.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT store_id, profile, schema_version, title FROM store_info"
            ).fetchall()
            has_documents = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
            ).fetchone()
            document_count = (
                int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                if has_documents is not None
                else 0
            )
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise FolderLifecycleError(
            "folder_layout_incomplete", "This Folder's Co-work data cannot be read."
        ) from exc
    if len(rows) != 1 or quick != "ok":
        raise FolderLifecycleError(
            "folder_layout_incomplete", "This Folder's Co-work data failed integrity checks."
        )
    row = rows[0]
    if (
        row["store_id"] != profile.store_id
        or row["profile"] != profile.profile
        or row["title"] != profile.title
    ):
        raise FolderLifecycleError(
            "identity_conflict", "store.yaml and store.db disagree about store identity."
        )
    return StoreIdentity(
        store_id=profile.store_id,
        profile=profile.profile,
        title=profile.title,
        schema_version=int(row["schema_version"]),
        document_surface_enabled=profile.document_surface.enabled,
        allowed_document_classes=tuple(
            profile.document_surface.allowed_document_classes
        ),
        feedback_capture=profile.document_surface.feedback_capture,
        document_count=document_count,
    )


def _fingerprint(root: Path) -> str:
    facts: list[tuple[str, bool, int | None, int | None]] = []
    paths = (
        root,
        root / ".wbuddy" / "manifest.yaml",
        root / ".wbuddy" / "cowork" / "store.yaml",
        root / ".wbuddy" / "cowork" / "store.db",
    )
    for path in paths:
        try:
            info = path.stat()
            facts.append((str(path.relative_to(root)) if path != root else ".", True, info.st_mtime_ns, info.st_size))
        except OSError:
            facts.append((str(path.relative_to(root)) if path != root else ".", False, None, None))
    return hashlib.sha256(
        json.dumps(facts, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _folder_from_registry_path(path: Path) -> Path:
    if path.name == "cowork" and path.parent.name == ".wbuddy":
        return path.parent.parent
    return path.parent


def folder_summary(row: RegisteredTruthStore, *, read_only: bool) -> dict[str, Any]:
    folder = _folder_from_registry_path(row.path)
    enabled = row.document_surface_enabled
    mutable = row.reachable and enabled and not read_only and row.layout == CANONICAL_LAYOUT
    return {
        "store_id": row.store_id,
        "folder_name": folder.name,
        "folder_path": str(folder),
        "layout": row.layout,
        "reachable": row.reachable,
        "eligibility": "eligible" if row.reachable and enabled else "ineligible",
        "ineligible_reason": (
            None
            if row.reachable and enabled
            else row.last_error or ("document_surface_disabled" if row.reachable else "folder_unreachable")
        ),
        "document_surface": {
            "enabled": enabled,
            "allowed_document_classes": list(row.allowed_document_classes),
            "feedback_capture": row.feedback_capture,
        },
        "permissions": {
            "read": row.reachable and enabled,
            "create": mutable,
            "import": mutable,
            "materialize": mutable,
            "retire": mutable,
        },
        "document_count": row.document_count,
    }


class ProjectStoreManager:
    """Coordinates read-only Folder inspection and explicit publications."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        scan_budget: int = DEFAULT_SCAN_BUDGET,
        scan_hard_limit: int = DEFAULT_SCAN_HARD_LIMIT,
    ) -> None:
        if data_root is None:
            from work_buddy.paths import data_dir

            data_root = data_dir()
        self.data_root = Path(data_root).expanduser().resolve()
        self.scan_budget = max(1, int(scan_budget))
        self.scan_hard_limit = max(self.scan_budget, int(scan_hard_limit))
        self.scan_dir = self.data_root / "runtime" / "cowork-folder-scans"
        self.receipt_dir = self.data_root / "runtime" / "cowork-folder-receipts"

    def _scan_path(self, token: str) -> Path:
        if not _TOKEN.fullmatch(token):
            raise FolderLifecycleError("invalid_request", "Invalid scan continuation token.", status=400)
        return self.scan_dir / f"{token}.json"

    def _save_json(self, path: Path, data: Mapping[str, Any]) -> None:
        _atomic_write(
            path,
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            absent_only=not path.exists(),
        )

    def _scan_descendants(
        self,
        root: Path,
        *,
        continuation_token: str | None = None,
        complete: bool = False,
    ) -> ScanResult:
        root_info = root.stat()
        if continuation_token:
            state_path = self._scan_path(continuation_token)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FolderLifecycleError(
                    "descendant_scan_incomplete",
                    "The Folder scan can no longer be resumed; inspect it again.",
                    retryable=True,
                ) from exc
            if state.get("root") != str(root) or state.get("root_mtime_ns") != root_info.st_mtime_ns:
                state_path.unlink(missing_ok=True)
                raise FolderLifecycleError(
                    "descendant_scan_incomplete",
                    "The Folder changed while descendants were inspected.",
                    retryable=True,
                )
            token = continuation_token
            pending = list(state.get("pending") or [])
            visited = int(state.get("visited") or 0)
            fingerprints = {
                str(key): int(value)
                for key, value in (state.get("directory_fingerprints") or {}).items()
            }
            for relative, expected_mtime in fingerprints.items():
                directory = root if relative == "." else root / relative
                try:
                    current_mtime = directory.stat().st_mtime_ns
                except OSError as exc:
                    state_path.unlink(missing_ok=True)
                    raise FolderLifecycleError(
                        "descendant_scan_incomplete",
                        "A visited Folder descendant changed during inspection.",
                        retryable=True,
                    ) from exc
                if current_mtime != expected_mtime:
                    state_path.unlink(missing_ok=True)
                    raise FolderLifecycleError(
                        "descendant_scan_incomplete",
                        "A visited Folder descendant changed during inspection.",
                        retryable=True,
                    )
        else:
            token = uuid.uuid4().hex
            state_path = self._scan_path(token)
            pending = ["."]
            visited = 0
            fingerprints: dict[str, int] = {}

        budget_used = 0
        nested: list[str] = []
        root_device = root_info.st_dev
        try:
            while pending and (complete or budget_used < self.scan_budget):
                relative = pending.pop()
                directory = root if relative == "." else root / relative
                if relative != ".":
                    _assert_managed_layout_safe(directory)
                    if (
                        directory / ".wbuddy" / "cowork" / "store.yaml"
                    ).is_file():
                        nested.append(relative.replace(os.sep, "/"))
                        state_path.unlink(missing_ok=True)
                        return ScanResult(
                            "nested",
                            visited_entries=visited,
                            nested_folders=tuple(sorted(nested)),
                        )
                before_mtime = directory.stat().st_mtime_ns
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        budget_used += 1
                        if visited > self.scan_hard_limit:
                            state_path.unlink(missing_ok=True)
                            return ScanResult("too_large", visited_entries=visited)
                        if entry.name in _SKIP_DIRS:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise FolderLifecycleError(
                                "descendant_scan_incomplete",
                                "A Folder descendant could not be inspected.",
                                retryable=True,
                            ) from exc
                        attributes = getattr(info, "st_file_attributes", 0)
                        reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                        crosses_device = bool(
                            root_device and info.st_dev and info.st_dev != root_device
                        )
                        if entry.is_symlink() or attributes & reparse or crosses_device:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            child = entry.name if relative == "." else str(Path(relative) / entry.name)
                            pending.append(child)
                after_mtime = directory.stat().st_mtime_ns
                if after_mtime != before_mtime:
                    state_path.unlink(missing_ok=True)
                    raise FolderLifecycleError(
                        "descendant_scan_incomplete",
                        "A Folder descendant changed while it was inspected.",
                        retryable=True,
                    )
                fingerprints[relative] = after_mtime
                if nested:
                    state_path.unlink(missing_ok=True)
                    return ScanResult(
                        "nested", visited_entries=visited, nested_folders=tuple(sorted(nested))
                    )
        except FolderLifecycleError:
            state_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            state_path.unlink(missing_ok=True)
            raise FolderLifecycleError(
                "descendant_scan_incomplete",
                "The Folder descendant scan could not be completed.",
                retryable=True,
            ) from exc

        if pending:
            self._save_json(
                state_path,
                {
                    "root": str(root),
                    "root_mtime_ns": root_info.st_mtime_ns,
                    "pending": pending,
                    "visited": visited,
                    "directory_fingerprints": fingerprints,
                    "updated_at": time.time(),
                },
            )
            return ScanResult(
                "pending",
                continuation_token=token,
                visited_entries=visited,
                retry_after_ms=50,
            )
        state_path.unlink(missing_ok=True)
        return ScanResult("complete", visited_entries=visited)

    def _classify_exact(self, root: Path) -> FolderInspection:
        try:
            _assert_managed_layout_safe(root)
        except FolderLifecycleError as exc:
            return FolderInspection(
                "collision",
                root,
                root.name,
                reason_code=exc.code,
                actions=("repair", "choose_another"),
            )
        fingerprint = _fingerprint(root)
        canonical = StorePaths.canonical(root)
        wbuddy = root / ".wbuddy"

        try:
            manifest = read_manifest(root)
        except FolderLifecycleError as exc:
            return FolderInspection(
                "collision", root, root.name, reason_code=exc.code,
                actions=("repair", "choose_another"), fingerprint=fingerprint,
            )

        canonical_identity: StoreIdentity | None = None
        if canonical.sidecar.exists():
            try:
                canonical_identity = _read_store_identity(canonical.sidecar)
            except FolderLifecycleError as exc:
                return FolderInspection(
                    "collision", root, root.name, reason_code=exc.code,
                    actions=("repair", "choose_another"), fingerprint=fingerprint,
                )
            if not manifest.exists or not manifest.has_cowork:
                return FolderInspection(
                    "collision", root, root.name,
                    reason_code="folder_layout_incomplete",
                    actions=("repair", "choose_another"), fingerprint=fingerprint,
                )
        elif manifest.has_cowork:
            return FolderInspection(
                "collision", root, root.name, reason_code="folder_layout_incomplete",
                actions=("repair", "choose_another"), fingerprint=fingerprint,
            )

        if canonical_identity:
            return FolderInspection(
                "initialized", root, root.name, layout=CANONICAL_LAYOUT,
                store_id=canonical_identity.store_id, actions=("open",),
                fingerprint=fingerprint,
            )
        if wbuddy.exists() and not manifest.exists and any(wbuddy.iterdir()):
            return FolderInspection(
                "collision", root, root.name, reason_code="folder_layout_incomplete",
                actions=("repair", "choose_another"), fingerprint=fingerprint,
            )
        return FolderInspection(
            "uninitialized", root, root.name, actions=("initialize", "choose_another"),
            fingerprint=fingerprint,
        )

    def _nearest_owner(self, root: Path) -> FolderInspection | None:
        for ancestor in root.parents:
            try:
                classified = self._classify_exact(ancestor)
            except (OSError, FolderLifecycleError):
                continue
            if classified.status == "initialized":
                return classified
        return None

    def inspect(
        self,
        folder: str | Path,
        *,
        continuation_token: str | None = None,
        complete_scan: bool = False,
    ) -> FolderInspection:
        root = _canonical_folder(folder)
        try:
            _assert_managed_layout_safe(root)
        except FolderLifecycleError as exc:
            return FolderInspection(
                "collision",
                root,
                root.name,
                reason_code=exc.code,
                actions=("repair", "choose_another"),
            )
        owner = self._nearest_owner(root)
        if owner is not None:
            return FolderInspection(
                "inside_existing_folder", root, root.name,
                owner_path=owner.folder_path, owner_store_id=owner.store_id,
                actions=("open_owner", "choose_another"), fingerprint=_fingerprint(root),
            )
        exact = self._classify_exact(root)
        if exact.status != "uninitialized":
            return exact
        scan = self._scan_descendants(
            root,
            continuation_token=continuation_token,
            complete=complete_scan,
        )
        if scan.state == "pending":
            return FolderInspection(
                "inspection_pending", root, root.name,
                continuation_token=scan.continuation_token,
                progress={
                    "visited": scan.visited_entries,
                    "visited_entries": scan.visited_entries,
                },
                retry_after_ms=scan.retry_after_ms,
            )
        if scan.state == "too_large":
            return FolderInspection(
                "unavailable", root, root.name,
                reason_code="folder_too_large_for_safe_setup",
                actions=("choose_narrower_folder",), fingerprint=_fingerprint(root),
            )
        if scan.state == "nested":
            boundaries: list[Mapping[str, Any]] = []
            for relative in scan.nested_folders:
                boundary_root = root.joinpath(*relative.split("/"))
                classified = self._classify_exact(boundary_root)
                boundaries.append(
                    {
                        "folder_name": boundary_root.name,
                        "folder_path": str(boundary_root),
                        "store_id": (
                            classified.store_id
                            if classified.status == "initialized"
                            else None
                        ),
                    }
                )
            return FolderInspection(
                "contains_nested_folder", root, root.name,
                reason_code="contains_nested_folder", actions=("choose_another",),
                fingerprint=_fingerprint(root),
                progress={"nested_count": len(scan.nested_folders)},
                boundaries=tuple(boundaries),
            )
        return self._classify_exact(root)

    def _default_profile(self, root: Path) -> dict[str, Any]:
        return {
            "store_id": new_id(),
            "profile": "cowork-default-v1",
            "title": root.name,
            "allowed_claim_kinds": ["fact", "preference", "decision", "commitment"],
            "required_fields": {},
            "gate": {
                "rejected_content": "retain",
                "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
                "block_materialize_on_flags": False,
            },
            "projection": "resident",
            "export_committed": True,
            "document_surface": {
                "enabled": True,
                "allowed_document_classes": ["co_authored"],
                "feedback_capture": True,
            },
        }

    def open_initialized(
        self,
        folder: str | Path,
        *,
        registry: TruthStoreRegistry,
        inspection_fingerprint: str,
    ) -> RegisteredTruthStore:
        """Adopt one validated canonical Folder into machine inventory.

        This action writes only the machine registry. It performs immutable
        profile/SQLite reads under the exact-Folder and external store locks;
        it never opens the project database through the mutating TruthStore
        path and never creates files beneath the selected Folder.
        """

        root = _canonical_folder(folder)
        with exact_folder_lock(root, data_root=self.data_root):
            current = self._classify_exact(root)
            if current.fingerprint != inspection_fingerprint:
                raise FolderLifecycleError(
                    "folder_changed",
                    "The Folder changed after it was inspected.",
                    retryable=True,
                )
            if current.status != "initialized" or current.store_id is None:
                raise FolderLifecycleError(
                    "folder_changed",
                    "The Folder is no longer an initialized canonical Co-work Folder.",
                    retryable=True,
                )
            canonical = StorePaths.canonical(root)
            with store_write_lock(
                root,
                current.store_id,
                data_root=self.data_root,
            ):
                revalidated = self._classify_exact(root)
                if (
                    revalidated.status != "initialized"
                    or revalidated.store_id != current.store_id
                ):
                    raise FolderLifecycleError(
                        "folder_changed",
                        "The initialized Folder changed while it was opened.",
                        retryable=True,
                    )
                identity = _read_store_identity(canonical.sidecar)
                try:
                    return registry.register_projection(
                        canonical.sidecar,
                        store_id=identity.store_id,
                        profile=identity.profile,
                        title=identity.title,
                        document_surface_enabled=identity.document_surface_enabled,
                        allowed_document_classes=identity.allowed_document_classes,
                        feedback_capture=identity.feedback_capture,
                        document_count=identity.document_count,
                    )
                except StoreIdentityCollision as exc:
                    raise FolderLifecycleError(
                        "folder_store_collision",
                        "This Co-work data is already associated with another Folder.",
                    ) from exc
                except RegistryIdentityMismatch as exc:
                    raise FolderLifecycleError(
                        "identity_conflict",
                        "The registered Folder path carries another store identity.",
                    ) from exc

    def _receipt_path(self, idempotency_key: str) -> Path:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
            raise FolderLifecycleError(
                "invalid_request", "A bounded idempotency_key is required.", status=400
            )
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self.receipt_dir / f"{digest}.json"

    def _receipt(
        self,
        key: str,
        operation: str,
        root: Path,
        registry: TruthStoreRegistry,
    ) -> TruthStore | None:
        path = self._receipt_path(key)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("operation") != operation or data.get("folder") != str(root):
            raise FolderLifecycleError(
                "idempotency_conflict", "This idempotency key belongs to another request."
            )
        return registry.open_store(str(data["store_id"]))

    def _write_receipt(self, key: str, operation: str, root: Path, store: TruthStore) -> None:
        path = self._receipt_path(key)
        data = {
            "operation": operation,
            "folder": str(root),
            "store_id": store.store_id,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != data and (
                existing.get("operation") != operation
                or existing.get("folder") != str(root)
                or existing.get("store_id") != store.store_id
            ):
                raise FolderLifecycleError(
                    "idempotency_conflict", "This idempotency key has a different result."
                )
            return
        self._save_json(path, data)

    def initialize(
        self,
        folder: str | Path,
        *,
        registry: TruthStoreRegistry,
        inspection_fingerprint: str,
        idempotency_key: str,
        profile: Mapping[str, Any] | None = None,
    ) -> TruthStore:
        root = _canonical_folder(folder)
        replay = self._receipt(idempotency_key, "initialize", root, registry)
        if replay is not None:
            return replay
        with folder_operation_locks(root, data_root=self.data_root):
            replay = self._receipt(idempotency_key, "initialize", root, registry)
            if replay is not None:
                return replay
            current = self.inspect(root, complete_scan=True)
            if current.fingerprint != inspection_fingerprint:
                raise FolderLifecycleError(
                    "folder_changed", "The Folder changed after it was inspected.", retryable=True
                )
            if current.status != "uninitialized":
                raise FolderLifecycleError(
                    "folder_changed", "The Folder is no longer available for setup.", retryable=True
                )
            _assert_managed_layout_safe(root)
            manifest = read_manifest(root)
            wbuddy = root / ".wbuddy"
            wbuddy_preexisted = wbuddy.exists()
            wbuddy.mkdir(exist_ok=True)
            paths = StorePaths.canonical(root)
            marker = paths.sidecar / f".setup-{uuid.uuid4().hex}.pending"
            published_manifest: bytes | None = None
            receipt_written = False
            try:
                paths.sidecar.mkdir(exist_ok=False)
                marker.touch(exist_ok=False)
                _assert_managed_layout_safe(root)
                store = TruthStore.create(
                    paths.sidecar,
                    self._default_profile(root) if profile is None else profile,
                )
                _assert_managed_layout_safe(root)
                write_component_gitignore(paths.sidecar)
                _, published_manifest = patch_cowork_manifest(
                    root, expected_sha256=manifest.sha256
                )
                registry.register(store)
                self._write_receipt(idempotency_key, "initialize", root, store)
                receipt_written = True
                marker.unlink(missing_ok=True)
                return store
            except Exception:
                # If an outside writer replaced any managed path, abandon
                # rollback rather than following it during cleanup.
                _assert_managed_layout_safe(root)
                if receipt_written:
                    self._receipt_path(idempotency_key).unlink(missing_ok=True)
                try:
                    registry.unregister(paths.sidecar)
                except Exception:
                    pass
                if marker.is_file():
                    shutil.rmtree(paths.sidecar)
                if published_manifest is not None:
                    _restore_manifest(manifest, published_manifest)
                if not wbuddy_preexisted:
                    try:
                        wbuddy.rmdir()
                    except OSError:
                        pass
                raise


__all__ = [
    "CANONICAL_LAYOUT",
    "COMPONENT_GITIGNORE_LINES",
    "DEFAULT_SCAN_BUDGET",
    "DEFAULT_SCAN_HARD_LIMIT",
    "FolderInspection",
    "FolderLifecycleError",
    "MANIFEST_FORMAT",
    "ManifestSnapshot",
    "ProjectStoreManager",
    "ScanResult",
    "StoreIdentity",
    "folder_summary",
    "patch_cowork_manifest",
    "read_manifest",
    "write_component_gitignore",
]
