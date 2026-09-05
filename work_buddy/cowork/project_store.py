"""Canonical Co-work folder layout, inspection, and setup.

Inspection helpers in this module are read-only with respect to the selected
folder. Resumable scan cursors, idempotency receipts, and locks live beneath
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
# Written into every managed sidecar. The sidecar mixes machine-local working
# state (SQLite stores plus their WAL/SHM sidecars, pre-migration snapshots,
# atomic-write temporaries, locks, runtime scratch, content blobs) with a small
# durable surface meant to travel with the folder. Naming the machine-local
# files individually rots: the list has to grow every time a component writes
# beside the store, and whatever it misses is committed silently. Ignore the
# directory and re-include the committed surface instead, so a new sidecar file
# fails closed rather than leaking into history.
COMPONENT_GITIGNORE_LINES = (
    "# Work Buddy Co-work component state.",
    "# Everything beside the store is machine-local and stays uncommitted.",
    "# Only the durable surface re-included below travels with the folder:",
    "# this file, the store identity in store.yaml, and the deterministic",
    "# export under export/. A new file here is ignored by default; add a",
    "# re-include line for it only when it is deterministic and shareable.",
    "/*",
    "!/.gitignore",
    "!/store.yaml",
    "!/export/",
)
DEFAULT_SCAN_WORK_PER_PAGE = 20_000
DEFAULT_SCAN_WORK_LIMIT = 750_000
DEFAULT_TOKEN_TTL_SECONDS = 15 * 60
# A directory that mutates while it is being listed is re-queued instead of
# failing the whole scan. This caps that: a directory under continuous churn
# stops the scan honestly rather than spinning against it.
_SCAN_DIRECTORY_RETRY_LIMIT = 3
# Opening a directory costs two metadata lookups plus a listing; reading one
# entry out of an open listing is served from the same enumeration. Measured
# against a large repository the first is roughly forty times the second, so
# the scan charges a directory forty units and an entry one. The weight prices
# opening a directory, not every syscall the walk makes.
_SCAN_DIRECTORY_WEIGHT = 40
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
# The child every Work Buddy component writes beneath. A directory listing
# that never names it cannot be a store root, which is what lets the scan
# decide a boundary from the listing it already has.
_COMPONENT_DIR_NAME = ".wbuddy"
_SKIP_DIRS = frozenset(
    {".git", _COMPONENT_DIR_NAME, "node_modules", ".venv", "vendor"}
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
            "folder_not_found", "The selected folder does not exist.", status=404
        ) from exc
    if not resolved.is_dir():
        raise FolderLifecycleError(
            "folder_not_found", "The selected path is not a folder.", status=404
        )
    return resolved


def _managed_path_error(root: Path, path: Path) -> FolderLifecycleError:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return FolderLifecycleError(
        "folder_layout_incomplete",
        "The folder contains redirected or unsupported Work Buddy data.",
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
            "The folder's Work Buddy data could not be inspected safely.",
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
                            "The folder's Work Buddy data could not be inspected safely.",
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
                "The folder's Work Buddy data could not be inspected safely.",
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
    symlink cannot turn a selected folder operation into an external write.
    """

    root_info = root.stat()
    _assert_manifest_path_safe(root)
    _assert_managed_tree_safe(
        root,
        root / ".wbuddy" / "cowork",
        root_device=root_info.st_dev,
    )


def _is_store_root(scan_root: Path, directory: str) -> bool:
    """Report whether ``directory`` holds a Co-work store of its own.

    Only a directory whose own listing named the component child reaches this
    probe, so the metadata lookups are spent on the few directories that can
    possibly answer yes. The component child is read with ``lstat`` first: a
    redirected component is refused rather than followed, so the probe cannot
    read a store that lives outside the scanned tree.
    """

    component = os.path.join(directory, _COMPONENT_DIR_NAME)
    info = _lstat_managed(scan_root, Path(component))
    if info is None or not stat.S_ISDIR(info.st_mode):
        return False
    return os.path.isfile(os.path.join(component, "cowork", "store.yaml"))


def _managed_sidecar_root(sidecar: str | Path) -> tuple[Path, Path]:
    """Return lexical sidecar/root paths without resolving through a link."""

    lexical = Path(os.path.abspath(os.fspath(Path(sidecar).expanduser())))
    if lexical.name == "cowork" and lexical.parent.name == ".wbuddy":
        return lexical, lexical.parent.parent
    raise FolderLifecycleError(
        "folder_layout_incomplete",
        "The Co-work data path is outside a recognized folder layout.",
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
            "The Work Buddy folder manifest could not be read.",
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
            "The folder manifest changed after it was inspected.",
            retryable=True,
        )
    patched = _patched_manifest_bytes(snapshot)
    if snapshot.has_cowork:
        return snapshot, patched
    # Re-read immediately before publication. The surrounding exact-folder
    # lock serializes Work Buddy writers; this byte check catches outside edits.
    if snapshot.exists:
        current = snapshot.path.read_bytes()
        if current != snapshot.raw:
            raise FolderLifecycleError(
                "folder_changed", "The folder manifest changed during setup.", retryable=True
            )
        _atomic_write(snapshot.path, patched)
    else:
        try:
            _atomic_write(snapshot.path, patched, absent_only=True)
        except FileExistsError as exc:
            raise FolderLifecycleError(
                "folder_changed", "A folder manifest appeared during setup.", retryable=True
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
            "folder_layout_incomplete", "This folder's Co-work configuration is invalid."
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
            "folder_layout_incomplete", "This folder's Co-work data cannot be read."
        ) from exc
    if len(rows) != 1 or quick != "ok":
        raise FolderLifecycleError(
            "folder_layout_incomplete", "This folder's Co-work data failed integrity checks."
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


# The one setup refusal a caller can answer by asking again: reading the
# folder's Work Buddy data failed, so the classification settled nothing about
# what that data holds and the same folder can classify differently on the next
# attempt. Every other refusal describes a fact the walk did establish, which
# repeating the call cannot change. Membership decides both the ``retryable``
# flag and the HTTP status, so the two cannot drift apart.
_RETRYABLE_SETUP_REFUSALS = frozenset({"folder_unreachable"})


def _setup_refusal(
    current: FolderInspection,
    *,
    observed: bool,
) -> FolderLifecycleError:
    """Compose the refusal that stops setup on a folder that cannot take it.

    A caller that inspected the folder and then asked for setup holds a
    fingerprint over that observation, so its refusal reports the folder
    moving out from under what a human was shown. A caller with no prior
    observation has no such gap to honour, and its refusal carries the
    classification's own code and prose, naming what the folder is instead of
    implying a race that did not happen.

    The message is the whole contract for an agent caller, which reads the
    exception text and nothing else, so each one names both the folder's state
    and the action that answers it. A refusal that follows from failing to read
    the folder, rather than from what a read found, says exactly that and keeps
    the unreachable classification's retryable 503: an agent told the data is
    unread waits and asks again, where an agent told the data is broken repairs
    a folder that was never shown to be broken.
    """

    if observed:
        return FolderLifecycleError(
            "folder_changed",
            "The folder is no longer available for setup.",
            retryable=True,
        )
    status = current.status
    code = current.reason_code or status
    if status == "initialized":
        code = "folder_already_initialized"
        message = (
            "This folder is already set up for Co-work. Open it instead of "
            "setting it up again."
        )
    elif status == "inside_existing_folder":
        code = "inside_existing_folder"
        message = (
            "This folder sits inside a folder that is already set up for "
            "Co-work. Set up a folder outside that one instead."
        )
    elif code == "contains_nested_folder":
        message = (
            "This folder encloses a folder that is already set up for "
            "Co-work. Set up a folder that does not enclose it instead."
        )
    elif code == "folder_too_large_for_safe_setup":
        message = (
            "This folder holds too many items for Co-work to check safely. "
            "Set up a narrower folder inside it instead."
        )
    elif code == "folder_unreachable":
        # Reading the folder's Work Buddy data failed, so nothing is known
        # about what that data holds. Naming the folder unread rather than
        # incomplete is what keeps a caller from repairing a healthy store
        # that a backup or a scanner happened to be holding open.
        message = (
            "This folder's Work Buddy data is temporarily unavailable, so "
            "Co-work cannot tell what state the folder is in. Try setting it "
            "up again in a moment."
        )
    elif code == "identity_conflict":
        # The data was read and the records contradict each other, which is a
        # settled fact about the folder and a different repair from an
        # incomplete layout.
        message = (
            "This folder's Co-work records disagree about which store the "
            "folder holds. Repair that data, or set up a different folder."
        )
    elif status == "collision":
        message = (
            "This folder already holds Work Buddy data that does not form a "
            "complete Co-work folder. Repair that data, or set up a "
            "different folder."
        )
    else:
        message = "Co-work cannot set this folder up in the state it is in."
    retryable = code in _RETRYABLE_SETUP_REFUSALS
    return FolderLifecycleError(
        code,
        message,
        status=503 if retryable else 409,
        retryable=retryable,
    )


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
    """Coordinates read-only folder inspection and explicit publications."""

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        scan_work_per_page: int | None = None,
        scan_work_limit: int | None = None,
    ) -> None:
        if data_root is None:
            from work_buddy.paths import data_dir

            data_root = data_dir()
        self.data_root = Path(data_root).expanduser().resolve()
        # Both budgets are resolved here rather than in the signature, so a
        # caller that reassigns the module default reaches every manager it
        # builds afterwards. The two knobs are independently valid: a page
        # budget larger than the refusal threshold simply means one page
        # reaches it, and flooring the threshold to the page budget would
        # discard a deliberately small one.
        self.scan_work_per_page = max(
            1,
            int(
                DEFAULT_SCAN_WORK_PER_PAGE
                if scan_work_per_page is None
                else scan_work_per_page
            ),
        )
        self.scan_work_limit = max(
            1,
            int(
                DEFAULT_SCAN_WORK_LIMIT
                if scan_work_limit is None
                else scan_work_limit
            ),
        )
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

    def _sweep_scan_state(self) -> None:
        """Drop scan cursors no continuation token can still reach.

        A caller can abandon a paged scan at any point, leaving its pending
        list on disk. The cursor is reachable only through a signed token that
        expires after ``DEFAULT_TOKEN_TTL_SECONDS``, so a file older than that
        window is unreachable work. Every failure here is swallowed: an
        undeleted cursor is a small leak, never a reason to refuse a scan.
        """

        cutoff = time.time() - DEFAULT_TOKEN_TTL_SECONDS
        try:
            for entry in self.scan_dir.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return

    def _scan_descendants(
        self,
        root: Path,
        *,
        continuation_token: str | None = None,
        complete: bool = False,
    ) -> ScanResult:
        """Look for a Co-work store beneath ``root``, a budgeted page at a time.

        The paged walk is an advisory preview: it drives the launcher and mints
        an inspection token, and it trusts the pages it already walked instead
        of revalidating them on every continuation. The proof that gates a
        write is ``initialize``, which re-walks the whole tree in one unpaged
        pass while holding the folder operation locks and refuses anything that
        no longer classifies as ``uninitialized``. A preview that drifted
        between pages therefore cannot authorize an unsafe setup: the locked
        re-walk sees the store that appeared and stops the write.

        Every directory the walk reads is one a listing admitted: an entry is
        queued only after ``is_symlink`` and the reparse-point attribute clear
        it and its device matches the root's, and the root itself is validated
        by ``inspect`` before the walk starts. The walk therefore holds a
        redirect-free path to each directory it opens, and spends metadata
        lookups only where a listing names a component child.

        Both the page budget and the refusal threshold are counted in work
        units, where opening a directory costs ``_SCAN_DIRECTORY_WEIGHT`` and
        listing one entry costs 1, so a page of directories and a page of
        files take comparable wall time and neither shape escapes the
        threshold. The predicate reads only the shape of the tree, so the same
        tree refuses on every machine. A directory re-read after an mtime race
        is charged for each read, so the total is exact only for a quiescent
        tree.
        """

        root_info = root.stat()
        if continuation_token:
            state_path = self._scan_path(continuation_token)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FolderLifecycleError(
                    "descendant_scan_incomplete",
                    "The folder scan can no longer be resumed; inspect it again.",
                    retryable=True,
                ) from exc
            if state.get("root") != str(root):
                # A cursor holds the pending list of one folder. Replaying it
                # against another would report that folder's descendants.
                state_path.unlink(missing_ok=True)
                raise FolderLifecycleError(
                    "descendant_scan_incomplete",
                    "The folder scan belongs to another folder; inspect it again.",
                    retryable=True,
                )
            token = continuation_token
            pending = list(state.get("pending") or [])
            visited = int(state.get("visited") or 0)
            work = int(state.get("work") or 0)
            retries = {
                str(key): int(value)
                for key, value in (state.get("directory_retries") or {}).items()
            }
        else:
            token = uuid.uuid4().hex
            state_path = self._scan_path(token)
            self._sweep_scan_state()
            pending = ["."]
            visited = 0
            work = 0
            retries: dict[str, int] = {}

        page_work = 0
        nested: list[str] = []
        root_device = root_info.st_dev
        # The walk addresses directories as plain strings joined onto the
        # resolved root. ``Path`` belongs at the API boundary. Inside the loop
        # every path is built once and handed straight to an ``os`` call, and
        # relative paths are held POSIX-style, which is the form the cursor
        # stores and the caller reads.
        root_path = str(root)
        try:
            # Work is spent per directory and per entry but tested per
            # directory, so one page spends at most the budget plus one
            # directory's worth of work. Tightening that needs a cursor inside
            # a directory listing, and ``os.scandir`` exposes no stable resume
            # point, so the alternative is buffering whole listings to disk:
            # dearer than the overshoot.
            while pending and (complete or page_work < self.scan_work_per_page):
                relative = pending.pop()
                directory = (
                    root_path if relative == "." else os.path.join(root_path, relative)
                )
                try:
                    before_mtime = os.stat(directory).st_mtime_ns
                    listing = os.scandir(directory)
                except (FileNotFoundError, NotADirectoryError):
                    # The directory went away between being queued and being
                    # read, the same race an entry can lose. A path that no
                    # longer exists cannot hold a store, so dropping it leaves
                    # the proof intact.
                    retries.pop(relative, None)
                    continue
                children: list[str] = []
                names_component = False
                with listing as entries:
                    # Charged only once the metadata lookup and the listing
                    # have both succeeded, so a directory that went away
                    # between being queued and being read costs nothing
                    # rather than being billed for work the walk never did.
                    page_work += _SCAN_DIRECTORY_WEIGHT
                    work += _SCAN_DIRECTORY_WEIGHT
                    if work > self.scan_work_limit:
                        state_path.unlink(missing_ok=True)
                        return ScanResult("too_large", visited_entries=visited)
                    for entry in entries:
                        visited += 1
                        page_work += 1
                        work += 1
                        if work > self.scan_work_limit:
                            state_path.unlink(missing_ok=True)
                            return ScanResult("too_large", visited_entries=visited)
                        name = entry.name
                        if name in _SKIP_DIRS:
                            # A component child is the one skipped name the
                            # scan still learns something from: it decides
                            # whether this directory is worth probing at all.
                            if name == _COMPONENT_DIR_NAME:
                                names_component = True
                            continue
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                # A file holds no store and is never opened, so
                                # its redirection and device facts are never
                                # consulted and never worth reading.
                                continue
                            redirected = entry.is_symlink()
                            info = entry.stat(follow_symlinks=False)
                        except (FileNotFoundError, NotADirectoryError):
                            # The entry went away between the listing and the
                            # lookup. A path that no longer exists cannot hold
                            # a store, so skipping it leaves the proof intact.
                            continue
                        except OSError as exc:
                            # An entry that exists but cannot be read could
                            # hide a store, so skipping it would make the
                            # proof unsound.
                            raise FolderLifecycleError(
                                "descendant_scan_incomplete",
                                "A folder descendant could not be inspected.",
                                retryable=True,
                            ) from exc
                        attributes = getattr(info, "st_file_attributes", 0)
                        crosses_device = bool(
                            root_device and info.st_dev and info.st_dev != root_device
                        )
                        if redirected or attributes & _REPARSE_POINT or crosses_device:
                            continue
                        children.append(
                            name if relative == "." else f"{relative}/{name}"
                        )
                try:
                    settled = os.stat(directory).st_mtime_ns == before_mtime
                except (FileNotFoundError, NotADirectoryError):
                    # Deleted while it was being listed. Whatever it held is
                    # gone with it, so the children it yielded are dropped too.
                    retries.pop(relative, None)
                    continue
                if not settled:
                    # The listing raced a writer and may have missed an entry.
                    # Re-queue the directory and drop the children it yielded,
                    # so the re-read is the only thing that enqueues them.
                    attempts = retries.get(relative, 0) + 1
                    if attempts > _SCAN_DIRECTORY_RETRY_LIMIT:
                        raise FolderLifecycleError(
                            "descendant_scan_incomplete",
                            "A folder descendant kept changing while it was inspected.",
                            retryable=True,
                        )
                    retries[relative] = attempts
                    pending.append(relative)
                    continue
                retries.pop(relative, None)
                if (
                    names_component
                    and relative != "."
                    and _is_store_root(root, directory)
                ):
                    # A store root is a hard boundary: record it and leave its
                    # interior unwalked. The caller needs the boundary, and
                    # everything below it belongs to that store, so the
                    # children this listing yielded are dropped.
                    nested.append(relative)
                    continue
                pending.extend(children)
        except FolderLifecycleError:
            state_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            state_path.unlink(missing_ok=True)
            raise FolderLifecycleError(
                "descendant_scan_incomplete",
                "The folder descendant scan could not be completed.",
                retryable=True,
            ) from exc

        if nested:
            # One page can cross several boundaries, and the caller renders
            # them as a list. Report all of them together and stop: the folder
            # is already disqualified, so further pages buy nothing.
            state_path.unlink(missing_ok=True)
            return ScanResult(
                "nested",
                visited_entries=visited,
                nested_folders=tuple(sorted(nested)),
            )
        if pending:
            self._save_json(
                state_path,
                {
                    "root": str(root),
                    "pending": pending,
                    "visited": visited,
                    "work": work,
                    "directory_retries": retries,
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
        if continuation_token is None:
            # Ownership and exact classification gate the walk, so a
            # continuation token exists only where both already answered:
            # no ancestor owns the folder and the folder itself classifies as
            # ``uninitialized``. Every terminal page classifies again, and the
            # answer that gates a write comes from ``initialize``, which
            # re-walks the tree unpaged under the folder operation locks. A
            # continuation therefore carries no authority these two would have
            # to re-establish.
            owner = self._nearest_owner(root)
            if owner is not None:
                return FolderInspection(
                    "inside_existing_folder", root, root.name,
                    owner_path=owner.folder_path, owner_store_id=owner.store_id,
                    actions=("open_owner", "choose_another"),
                    fingerprint=_fingerprint(root),
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
        """Adopt one validated canonical folder into machine inventory.

        This action writes only the machine registry. It performs immutable
        profile/SQLite reads under the exact-folder and external store locks;
        it never opens the project database through the mutating TruthStore
        path and never creates files beneath the selected folder.
        """

        root = _canonical_folder(folder)
        with exact_folder_lock(root, data_root=self.data_root):
            current = self._classify_exact(root)
            if current.fingerprint != inspection_fingerprint:
                raise FolderLifecycleError(
                    "folder_changed",
                    "The folder changed after it was inspected.",
                    retryable=True,
                )
            if current.status != "initialized" or current.store_id is None:
                raise FolderLifecycleError(
                    "folder_changed",
                    "The folder is no longer an initialized canonical Co-work folder.",
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
                        "The initialized folder changed while it was opened.",
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
                        "This Co-work data is already associated with another folder.",
                    ) from exc
                except RegistryIdentityMismatch as exc:
                    raise FolderLifecycleError(
                        "identity_conflict",
                        "The registered folder path carries another store identity.",
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
        inspection_fingerprint: str | None,
        idempotency_key: str,
        profile: Mapping[str, Any] | None = None,
    ) -> TruthStore:
        """Set one folder up for Co-work under the folder operation locks.

        ``inspection_fingerprint`` carries a caller's earlier observation of
        the folder, and setup refuses a folder that moved since a human was
        shown it. A caller that reaches setup with no such gap to honour passes
        ``None`` and is refused on what the locked walk finds, which names the
        folder's actual state rather than reporting a change nobody made.

        Neither caller rests its safety on the fingerprint. The walk here runs
        inside the folder operation locks, classifies the folder from the
        filesystem, and refuses anything that is not ``uninitialized``; the
        managed-layout assertion then re-runs around every write beneath the
        folder.
        """

        root = _canonical_folder(folder)
        replay = self._receipt(idempotency_key, "initialize", root, registry)
        if replay is not None:
            return replay
        with folder_operation_locks(root, data_root=self.data_root):
            replay = self._receipt(idempotency_key, "initialize", root, registry)
            if replay is not None:
                return replay
            current = self.inspect(root, complete_scan=True)
            observed = inspection_fingerprint is not None
            if observed and current.fingerprint != inspection_fingerprint:
                raise FolderLifecycleError(
                    "folder_changed", "The folder changed after it was inspected.", retryable=True
                )
            if current.status != "uninitialized":
                raise _setup_refusal(current, observed=observed)
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
    "DEFAULT_SCAN_WORK_LIMIT",
    "DEFAULT_SCAN_WORK_PER_PAGE",
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
