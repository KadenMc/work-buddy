"""Canonical Folder layout, inspection, setup, and path-safety tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from work_buddy.cowork.project_store import (
    COMPONENT_GITIGNORE_LINES,
    FolderLifecycleError,
    ProjectStoreManager,
    patch_cowork_manifest,
    read_manifest,
)
from work_buddy.truth.contracts import StorePaths
from work_buddy.truth.identity import new_id
from work_buddy.truth.registry import TruthStoreRegistry


def _profile(store_id: str | None = None) -> dict[str, object]:
    return {
        "store_id": store_id or new_id(),
        "profile": "cowork-test",
        "title": "Co-work Folder test",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["dashboard"],
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


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        result[key] = path.read_bytes() if path.is_file() else None
    return result


def _make_directory_redirect(link: Path, target: Path) -> None:
    """Create the platform's directory redirection primitive or skip."""

    if os.name == "nt":
        completed = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            pytest.skip(
                "Windows junction creation is unavailable: "
                f"{completed.stderr or completed.stdout}"
            )
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")


def _remove_directory_redirect(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


def test_manifest_patch_preserves_unowned_bytes_and_comments(tmp_path: Path) -> None:
    manifest = tmp_path / ".wbuddy" / "manifest.yaml"
    manifest.parent.mkdir()
    original = (
        b"# sibling-owned header\r\n"
        b"format: wbuddy-folder/v1\r\n"
        b"components:\r\n"
        b"  search:\r\n"
        b"    path: search  # keep this comment\r\n"
        b"# keep this boundary comment\r\n"
        b"future_field: untouched\r\n"
    )
    manifest.write_bytes(original)
    snapshot = read_manifest(tmp_path)

    before, published = patch_cowork_manifest(
        tmp_path,
        expected_sha256=snapshot.sha256,
    )

    assert before.raw == original
    assert manifest.read_bytes() == published
    for line in original.splitlines(keepends=True):
        assert line in published
    parsed = yaml.safe_load(published.decode("utf-8"))
    assert parsed["components"]["search"] == {"path": "search"}
    assert parsed["components"]["cowork"] == {"path": "cowork"}
    assert parsed["future_field"] == "untouched"


def test_new_manifest_publication_is_byte_exact(tmp_path: Path) -> None:
    snapshot = read_manifest(tmp_path)

    before, published = patch_cowork_manifest(
        tmp_path,
        expected_sha256=snapshot.sha256,
    )

    assert before.exists is False
    assert (tmp_path / ".wbuddy" / "manifest.yaml").read_bytes() == published


def test_inspection_of_ordinary_folder_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    folder = tmp_path / "ordinary"
    folder.mkdir()
    (folder / "notes.md").write_text("throwaway\n", encoding="utf-8")
    before = _tree_bytes(folder)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(folder)

    assert result.status == "uninitialized"
    assert result.folder_name == "ordinary"
    assert _tree_bytes(folder) == before
    assert not (folder / ".wbuddy").exists()


@pytest.mark.parametrize("managed_path", [".wbuddy", ".wbuddy/cowork"])
def test_inspection_rejects_redirected_managed_directories(
    tmp_path: Path,
    managed_path: str,
) -> None:
    folder = tmp_path / f"redirected-{managed_path.replace('/', '-')}"
    folder.mkdir()
    target = tmp_path / f"external-{managed_path.replace('/', '-')}"
    target.mkdir()
    sentinel = target / "must-not-change.txt"
    sentinel.write_bytes(b"outside-selected-folder")
    link = folder / managed_path
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_directory_redirect(link, target)
    try:
        result = ProjectStoreManager(data_root=tmp_path / "machine").inspect(folder)

        assert result.status == "collision"
        assert result.reason_code == "folder_layout_incomplete"
        assert sentinel.read_bytes() == b"outside-selected-folder"
        assert sorted(path.name for path in target.iterdir()) == [sentinel.name]
    finally:
        _remove_directory_redirect(link)


@pytest.mark.parametrize("managed_path", [".wbuddy", ".wbuddy/cowork"])
def test_setup_cannot_write_through_redirect_added_after_inspection(
    tmp_path: Path,
    managed_path: str,
) -> None:
    folder = tmp_path / f"setup-swap-{managed_path.replace('/', '-')}"
    folder.mkdir()
    target = tmp_path / f"external-setup-target-{managed_path.replace('/', '-')}"
    target.mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(folder)
    link = folder / managed_path
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_directory_redirect(link, target)
    try:
        with pytest.raises(FolderLifecycleError) as failure:
            manager.initialize(
                folder,
                registry=registry,
                inspection_fingerprint=inspected.fingerprint or "",
                idempotency_key="redirected-setup",
            )

        assert failure.value.code == "folder_changed"
        assert list(target.iterdir()) == []
        assert registry.list_stores(refresh=False) == ()
    finally:
        _remove_directory_redirect(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_posix_symlinked_store_file_is_rejected_without_following(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "symlinked-file"
    folder.mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(folder)
    manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="seed-symlinked-file",
    )
    database = folder / ".wbuddy" / "cowork" / "store.db"
    external = tmp_path / "external-store.db"
    shutil.copy2(database, external)
    database.unlink()
    database.symlink_to(external)

    result = manager.inspect(folder)

    assert result.status == "collision"
    assert result.reason_code == "folder_layout_incomplete"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_is_detected_as_a_reparse_point(tmp_path: Path) -> None:
    folder = tmp_path / "junction-folder"
    (folder / ".wbuddy").mkdir(parents=True)
    target = tmp_path / "junction-target"
    target.mkdir()
    link = folder / ".wbuddy" / "cowork"
    _make_directory_redirect(link, target)
    try:
        info = link.lstat()
        assert getattr(info, "st_file_attributes", 0) & 0x400

        result = ProjectStoreManager(data_root=tmp_path / "machine").inspect(folder)

        assert result.status == "collision"
        assert result.reason_code == "folder_layout_incomplete"
    finally:
        _remove_directory_redirect(link)


def test_explicit_setup_creates_only_canonical_integrated_store(tmp_path: Path) -> None:
    folder = tmp_path / "real-folder-name"
    folder.mkdir()
    root_ignore = folder / ".gitignore"
    root_ignore.write_bytes(b"# user-owned\n")
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(folder)

    store = manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="setup-once",
    )

    canonical = StorePaths.canonical(folder)
    assert store.paths.sidecar == canonical.sidecar
    assert canonical.config.is_file()
    assert canonical.db.is_file()
    assert canonical.blobs.is_dir()
    assert canonical.export_dir.is_dir()
    assert canonical.runtime.is_dir()
    assert root_ignore.read_bytes() == b"# user-owned\n"
    manifest = yaml.safe_load((folder / ".wbuddy" / "manifest.yaml").read_text())
    assert manifest == {
        "format": "wbuddy-folder/v1",
        "components": {"cowork": {"path": "cowork"}},
    }
    ignore = (canonical.sidecar / ".gitignore").read_text(encoding="utf-8")
    assert all(line in ignore.splitlines() for line in COMPONENT_GITIGNORE_LINES)
    assert store.profile.title == folder.name
    assert store.profile.document_surface.allowed_document_classes == ("co_authored",)
    row = registry.get_by_store_id(store.store_id, refresh=False)
    assert row is not None and row.layout == "wbuddy_cowork_v1"
    assert manager.inspect(folder).status == "initialized"

    replay = manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="setup-once",
    )
    assert replay.store_id == store.store_id


def test_setup_preserves_unrelated_manifest_component(tmp_path: Path) -> None:
    folder = tmp_path / "with-component"
    manifest = folder / ".wbuddy" / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    original = (
        b"format: wbuddy-folder/v1\ncomponents:\n"
        b"  search:\n    path: search # owned elsewhere\n"
    )
    manifest.write_bytes(original)
    sibling = folder / ".wbuddy" / "search" / "state.bin"
    sibling.parent.mkdir()
    sibling.write_bytes(b"sibling bytes")
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(folder)
    assert inspected.status == "uninitialized"

    manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="component-setup",
    )

    assert sibling.read_bytes() == b"sibling bytes"
    published = manifest.read_bytes()
    assert b"path: search # owned elsewhere\n" in published
    assert yaml.safe_load(published)["components"]["cowork"] == {"path": "cowork"}


def test_descendant_scan_is_resumable_and_hard_limit_is_terminal(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    for index in range(5):
        (parent / f"dir-{index}").mkdir()
    nested_root = parent / "dir-4" / "nested"
    nested_root.mkdir()
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_budget=1,
        scan_hard_limit=100,
    )
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    nested_inspection = manager.inspect(nested_root)
    nested_store = manager.initialize(
        nested_root,
        registry=registry,
        inspection_fingerprint=nested_inspection.fingerprint or "",
        idempotency_key="nested-boundary",
    )

    result = manager.inspect(parent)
    while result.status == "inspection_pending":
        result = manager.inspect(
            parent,
            continuation_token=result.continuation_token,
        )
    assert result.status == "contains_nested_folder"
    expected_boundary = {
        "folder_name": nested_root.name,
        "folder_path": str(nested_root.resolve()),
        "store_id": nested_store.store_id,
    }
    assert result.boundaries == (expected_boundary,)
    assert result.to_dict()["boundaries"] == [expected_boundary]

    too_large = tmp_path / "too-large"
    too_large.mkdir()
    for index in range(4):
        (too_large / f"entry-{index}.txt").write_text(str(index), encoding="ascii")
    capped = ProjectStoreManager(
        data_root=tmp_path / "other-machine",
        scan_budget=1,
        scan_hard_limit=2,
    ).inspect(too_large)
    assert capped.status == "unavailable"
    assert capped.reason_code == "folder_too_large_for_safe_setup"
    assert capped.continuation_token is None


def test_manifest_compare_and_swap_rejects_changed_bytes(tmp_path: Path) -> None:
    manifest = tmp_path / ".wbuddy" / "manifest.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        "format: wbuddy-folder/v1\ncomponents: {}\n",
        encoding="utf-8",
    )
    snapshot = read_manifest(tmp_path)
    manifest.write_text(
        "format: wbuddy-folder/v1\ncomponents:\n  other: {path: other}\n",
        encoding="utf-8",
    )
    with pytest.raises(FolderLifecycleError, match="changed") as failure:
        patch_cowork_manifest(tmp_path, expected_sha256=snapshot.sha256)
    assert failure.value.code == "folder_changed"
