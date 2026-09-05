"""Canonical Folder layout, inspection, setup, and path-safety tests."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest
import yaml

from work_buddy.cowork import project_store
from work_buddy.cowork.project_store import (
    COMPONENT_GITIGNORE_LINES,
    DEFAULT_TOKEN_TTL_SECONDS,
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


class _PhantomDirectoryEntry:
    """An entry naming a subdirectory the filesystem does not have.

    Its metadata is borrowed from a real directory, so the scan accepts it and
    queues it, and the lookup only fails once the scan tries to read it.
    """

    def __init__(self, name: str, borrowed: os.stat_result) -> None:
        self.name = name
        self._borrowed = borrowed

    def stat(self, **_kwargs: object) -> os.stat_result:
        return self._borrowed

    def is_dir(self, **_kwargs: object) -> bool:
        return True

    def is_symlink(self) -> bool:
        return False


class _RaisingEntry:
    """A directory entry whose every metadata lookup raises one chosen error."""

    def __init__(self, name: str, error: OSError) -> None:
        self.name = name
        self._error = error

    def stat(self, **_kwargs: object) -> os.stat_result:
        raise self._error

    def is_dir(self, **_kwargs: object) -> bool:
        raise self._error

    def is_symlink(self) -> bool:
        raise self._error


class _FileEntryWithoutMetadata:
    """A file entry that answers the directory question and nothing else.

    Reading a file's metadata raises, so a scan that consults it fails loudly
    rather than paying for facts a file can never make relevant.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self, **_kwargs: object) -> bool:
        return False

    def stat(self, **_kwargs: object) -> os.stat_result:
        raise AssertionError(f"{self.name} metadata was read")

    def is_symlink(self) -> bool:
        raise AssertionError(f"{self.name} redirection was read")


def _instrument_scandir(
    monkeypatch: pytest.MonkeyPatch,
    *,
    directory: Path | None = None,
    rewrite: Callable[[list[object]], list[object]] | None = None,
) -> list[Path]:
    """Record every directory the scan lists, optionally rewriting one listing.

    ``rewrite`` sees the entries yielded for ``directory`` and returns what the
    caller should observe, which is how a vanished or unreadable entry is
    staged without depending on the platform's caching of ``scandir`` metadata.
    Every other listing is handed straight to the real ``os.scandir``.
    """

    real = os.scandir
    listed: list[Path] = []
    target = directory.resolve() if directory is not None else None

    class _Listing:
        def __init__(self, path: object) -> None:
            self._inner = real(path)

        def __enter__(self) -> list[object]:
            assert rewrite is not None
            return rewrite(list(self._inner.__enter__()))

        def __exit__(self, *exc_info: object) -> None:
            self._inner.__exit__(*exc_info)

    def scandir(path: object = ".", *args: object, **kwargs: object) -> object:
        if args or kwargs:
            return real(path, *args, **kwargs)
        here = Path(os.fspath(path))
        listed.append(here)
        if target is not None and rewrite is not None and here.resolve() == target:
            return _Listing(path)
        return real(path)

    monkeypatch.setattr(project_store.os, "scandir", scandir)
    return listed


def _count_listings(listed: list[Path], directory: Path) -> int:
    resolved = directory.resolve()
    return sum(1 for path in listed if path.resolve() == resolved)


def _surviving_cursors(manager: ProjectStoreManager) -> list[Path]:
    """Scan cursors still on disk, tolerating a directory never written to."""

    return sorted(manager.scan_dir.glob("*.json"))


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


def test_shipped_ignore_keeps_machine_local_sidecar_state_out_of_git(
    tmp_path: Path,
) -> None:
    """Ask git itself what the shipped template would stage.

    Asserting the lines are present says nothing about what they match. The
    previous denylist listed ``/store.db-*``, which does not match a
    ``store.pre-vN.db`` migration snapshot, so multi-megabyte snapshots were
    staged by ``git add .wbuddy``. This checks the outcome instead.
    """

    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    sidecar = tmp_path / ".wbuddy" / "cowork"
    (sidecar / "blobs").mkdir(parents=True)
    (sidecar / "runtime" / "locks").mkdir(parents=True)
    (sidecar / "export").mkdir()
    (sidecar / ".gitignore").write_text(
        "\n".join(COMPONENT_GITIGNORE_LINES) + "\n", encoding="utf-8"
    )
    (tmp_path / ".wbuddy" / "manifest.yaml").write_text(
        "format: wbuddy-folder/v1\n", encoding="utf-8"
    )
    machine_local = (
        "store.db",
        "store.db-wal",
        "store.db-shm",
        "store.pre-v4.db",
        "store.pre-v11.db",
        "document-causality.db",
        "document-causality.db-wal",
        "document-causality.db-shm",
        ".store.pre-v4.db.abcd1234.tmp",
        "blobs/" + "0" * 64,
        "runtime/locks/writer.lock",
    )
    committed = ("store.yaml", "export/claims.jsonl")
    for name in machine_local + committed:
        (sidecar / name).write_bytes(b"x")

    staged = {
        line.split(" ", 1)[1].strip("'\"")
        for line in subprocess.run(
            ["git", "add", "-An", ".wbuddy"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if line.startswith("add ")
    }

    assert staged == {
        ".wbuddy/manifest.yaml",
        ".wbuddy/cowork/.gitignore",
        ".wbuddy/cowork/store.yaml",
        ".wbuddy/cowork/export/claims.jsonl",
    }
    for name in machine_local:
        assert f".wbuddy/cowork/{name}" not in staged


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


def test_a_paged_descendant_scan_resumes_onto_a_nested_boundary(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    for index in range(5):
        (parent / f"dir-{index}").mkdir()
    nested_root = parent / "dir-4" / "nested"
    nested_root.mkdir()
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_per_page=1,
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


def test_a_folder_past_the_work_limit_refuses_terminally(tmp_path: Path) -> None:
    too_large = tmp_path / "too-large"
    too_large.mkdir()
    for index in range(4):
        (too_large / f"entry-{index}.txt").write_text(str(index), encoding="ascii")
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_per_page=1,
        # Room to open the folder and list two of its entries.
        scan_work_limit=project_store._SCAN_DIRECTORY_WEIGHT + 2,
    )

    capped = manager.inspect(too_large)

    assert capped.status == "unavailable"
    assert capped.reason_code == "folder_too_large_for_safe_setup"
    assert capped.continuation_token is None
    assert _surviving_cursors(manager) == []


@pytest.mark.parametrize("width", [1, 3])
def test_a_directory_and_an_entry_are_charged_the_measured_ratio(
    tmp_path: Path,
    width: int,
) -> None:
    weight = project_store._SCAN_DIRECTORY_WEIGHT
    shapes = {
        # Opening the folder, listing its entries, then opening each empty
        # child.
        "directories": weight * (width + 1) + width,
        # Opening the folder and listing its entries.
        "files": weight + width,
    }
    directories = tmp_path / "directories"
    directories.mkdir()
    for index in range(width):
        (directories / f"child-{index}").mkdir()
    files = tmp_path / "files"
    files.mkdir()
    for index in range(width):
        (files / f"entry-{index}.txt").write_text(str(index), encoding="ascii")

    for name, expected_work in shapes.items():
        folder = tmp_path / name
        afforded = ProjectStoreManager(
            data_root=tmp_path / f"{name}-afforded",
            scan_work_limit=expected_work,
        ).inspect(folder)
        assert afforded.status == "uninitialized", name

        starved = ProjectStoreManager(
            data_root=tmp_path / f"{name}-starved",
            scan_work_limit=expected_work - 1,
        ).inspect(folder)
        assert starved.status == "unavailable", name
        assert starved.reason_code == "folder_too_large_for_safe_setup", name


def test_a_tree_of_empty_directories_is_bounded_by_the_page_budget(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "hollow"
    folder.mkdir()
    for index in range(6):
        (folder / f"child-{index}").mkdir()
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_per_page=project_store._SCAN_DIRECTORY_WEIGHT,
    )

    result = manager.inspect(folder)
    pages = 1
    while result.status == "inspection_pending":
        result = manager.inspect(folder, continuation_token=result.continuation_token)
        pages += 1

    assert result.status == "uninitialized"
    # Directories carry weight, so a tree that lists almost no entries still
    # pages instead of running its whole walk inside one request.
    assert pages == 7


def test_the_work_limit_is_not_floored_to_the_page_budget(tmp_path: Path) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    for index in range(4):
        (folder / f"entry-{index}.txt").write_text(str(index), encoding="ascii")
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_per_page=10_000,
        scan_work_limit=1,
    )

    assert manager.scan_work_limit == 1

    result = manager.inspect(folder)

    assert result.status == "unavailable"
    assert result.reason_code == "folder_too_large_for_safe_setup"


def test_the_work_limit_stops_an_unpaged_scan(tmp_path: Path) -> None:
    folder = tmp_path / "deep"
    folder.mkdir()
    for index in range(4):
        (folder / f"child-{index}").mkdir()
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_limit=project_store._SCAN_DIRECTORY_WEIGHT * 2,
    )

    result = manager.inspect(folder, complete_scan=True)

    assert result.status == "unavailable"
    assert result.reason_code == "folder_too_large_for_safe_setup"
    assert result.continuation_token is None
    assert _surviving_cursors(manager) == []


def test_the_work_limit_counts_work_spent_on_earlier_pages(tmp_path: Path) -> None:
    """The threshold is reached by the whole scan, not by one page of it.

    The launcher polls a paged scan, so the resumed walk is the only path a
    person takes. Each continuation restores the running total from the
    cursor; a walk that started every page from zero would spend an unbounded
    amount of work in total while no single page ever reached the threshold,
    and an arbitrarily large folder would classify as a setup candidate.
    """

    folder = tmp_path / "paged-wide"
    folder.mkdir()
    for index in range(4):
        (folder / f"child-{index}").mkdir()
    weight = project_store._SCAN_DIRECTORY_WEIGHT
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        # One directory per page, so each page opens exactly one directory.
        scan_work_per_page=1,
        # Room to open the folder, list its four entries, and open two of the
        # four children. The third child crosses the threshold, and it is the
        # fourth page that opens it.
        scan_work_limit=weight * 3 + 4,
    )

    result = manager.inspect(folder)
    pages = 1
    while result.status == "inspection_pending":
        result = manager.inspect(folder, continuation_token=result.continuation_token)
        pages += 1

    assert result.status == "unavailable"
    assert result.reason_code == "folder_too_large_for_safe_setup"
    assert pages == 4
    assert result.continuation_token is None
    assert _surviving_cursors(manager) == []


def test_the_scan_weight_and_both_budgets_are_pinned() -> None:
    """The numbers the whole budget rests on are named, not incidental.

    The directory weight is a measured ratio between opening a directory and
    reading one entry out of an already open listing, so it describes the
    hardware rather than a caller's preference. Both budgets are counted in
    those units and derived from the weight, which is what makes a page of
    directories and a page of files take comparable wall time.
    """

    assert project_store._SCAN_DIRECTORY_WEIGHT == 40
    assert project_store.DEFAULT_SCAN_WORK_PER_PAGE == 20_000
    assert project_store.DEFAULT_SCAN_WORK_LIMIT == 750_000
    # A page affords five hundred directory opens.
    assert (
        project_store.DEFAULT_SCAN_WORK_PER_PAGE
        == project_store._SCAN_DIRECTORY_WEIGHT * 500
    )
    # The refusal threshold affords eighteen thousand seven hundred and fifty.
    assert (
        project_store.DEFAULT_SCAN_WORK_LIMIT
        == project_store._SCAN_DIRECTORY_WEIGHT * 18_750
    )


def test_scan_progress_counts_entries_rather_than_work(tmp_path: Path) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    for index in range(3):
        (folder / f"child-{index}").mkdir()
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_per_page=1,
    )

    first = manager.inspect(folder)

    assert first.status == "inspection_pending"
    # One directory was opened and three entries were listed, which costs far
    # more than three work units. Progress reports the items a person can
    # recognize in the folder they picked.
    assert first.progress == {"visited": 3, "visited_entries": 3}


def test_directory_changed_while_listed_is_rescanned_without_double_queueing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "racy"
    (folder / "child").mkdir(parents=True)
    (folder / "child" / "leaf.txt").write_text("throwaway\n", encoding="utf-8")
    disturbances: list[int] = []

    def disturb(entries: list[object]) -> list[object]:
        if not disturbances:
            disturbances.append(1)
            stamp = folder.stat().st_mtime + 60
            os.utime(folder, (stamp, stamp))
        return entries

    listed = _instrument_scandir(monkeypatch, directory=folder, rewrite=disturb)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(folder)

    assert result.status == "uninitialized"
    assert disturbances == [1]
    # The changed directory is read twice, and the child it yielded is queued
    # by the successful read alone.
    assert _count_listings(listed, folder) == 2
    assert _count_listings(listed, folder / "child") == 1


def test_relentlessly_changing_directory_exhausts_its_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "churning"
    folder.mkdir()
    for index in range(3):
        (folder / f"entry-{index}.txt").write_text("throwaway\n", encoding="utf-8")

    def disturb(entries: list[object]) -> list[object]:
        stamp = folder.stat().st_mtime + 60
        os.utime(folder, (stamp, stamp))
        return entries

    _instrument_scandir(monkeypatch, directory=folder, rewrite=disturb)
    manager = ProjectStoreManager(data_root=tmp_path / "machine", scan_work_per_page=1)
    allowance = project_store._SCAN_DIRECTORY_RETRY_LIMIT
    token: str | None = None
    pages = 0

    with pytest.raises(FolderLifecycleError) as failure:
        for _ in range(allowance + 3):
            pending = manager.inspect(folder, continuation_token=token)
            assert pending.status == "inspection_pending"
            pages += 1
            token = pending.continuation_token

    assert failure.value.code == "descendant_scan_incomplete"
    assert failure.value.retryable is True
    # Each retry lands on its own page, so the count is carried by the cursor.
    assert pages == allowance
    assert _surviving_cursors(manager) == []


def test_entry_that_vanishes_mid_listing_does_not_abort_the_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "vanishing"
    folder.mkdir()
    (folder / "gone.txt").write_text("throwaway\n", encoding="utf-8")
    (folder / "sibling").mkdir()

    def vanish(entries: list[object]) -> list[object]:
        return [
            _RaisingEntry(entry.name, FileNotFoundError(errno.ENOENT, "gone"))
            if entry.name == "gone.txt"
            else entry
            for entry in entries
        ]

    listed = _instrument_scandir(monkeypatch, directory=folder, rewrite=vanish)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(folder)

    assert result.status == "uninitialized"
    assert _count_listings(listed, folder / "sibling") == 1


def test_directory_that_vanishes_before_it_is_read_does_not_abort_the_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "shrinking"
    survivor = folder / "survivor"
    survivor.mkdir(parents=True)
    borrowed = survivor.stat()

    def add_phantom(entries: list[object]) -> list[object]:
        # A listing names a subdirectory that is gone by the time the scan pops
        # it off the queue. Deleting a real directory cannot stage this: a
        # platform may keep serving a delete-pending handle, so the race would
        # not reliably reach the queued-then-missing path.
        return [*entries, _PhantomDirectoryEntry("ghost", borrowed)]

    listed = _instrument_scandir(monkeypatch, directory=folder, rewrite=add_phantom)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(folder)

    assert result.status == "uninitialized"
    assert _count_listings(listed, folder / "ghost") == 0
    assert _count_listings(listed, survivor) == 1


def test_unreadable_entry_stops_the_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "guarded"
    folder.mkdir()
    (folder / "sealed").mkdir()

    def seal(entries: list[object]) -> list[object]:
        return [
            _RaisingEntry(entry.name, PermissionError(errno.EACCES, "denied"))
            if entry.name == "sealed"
            else entry
            for entry in entries
        ]

    _instrument_scandir(monkeypatch, directory=folder, rewrite=seal)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    with pytest.raises(FolderLifecycleError) as failure:
        manager.inspect(folder)

    assert failure.value.code == "descendant_scan_incomplete"
    assert _surviving_cursors(manager) == []


def test_every_boundary_in_one_page_is_reported_together(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    stores = {}
    for name in ("alpha", "beta"):
        boundary = parent / name
        boundary.mkdir()
        inspected = manager.inspect(boundary)
        stores[name] = manager.initialize(
            boundary,
            registry=registry,
            inspection_fingerprint=inspected.fingerprint or "",
            idempotency_key=f"sibling-{name}",
        )

    result = manager.inspect(parent)

    assert result.status == "contains_nested_folder"
    assert result.progress == {"nested_count": 2}
    assert [boundary["folder_name"] for boundary in result.boundaries] == [
        "alpha",
        "beta",
    ]
    assert [boundary["store_id"] for boundary in result.boundaries] == [
        stores["alpha"].store_id,
        stores["beta"].store_id,
    ]
    assert len(result.to_dict()["boundaries"]) == 2


def test_a_boundary_interior_is_left_unwalked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    boundary = parent / "boundary"
    interior = boundary / "interior"
    (interior / "deeper").mkdir(parents=True)
    sibling = parent / "sibling"
    sibling.mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(boundary)
    manager.initialize(
        boundary,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="boundary-interior",
    )
    listed = _instrument_scandir(monkeypatch)

    result = manager.inspect(parent)

    assert result.status == "contains_nested_folder"
    assert _count_listings(listed, interior) == 0
    assert _count_listings(listed, interior / "deeper") == 0
    assert _count_listings(listed, sibling) == 1


def test_a_boundary_listing_is_read_and_its_children_are_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    boundary = parent / "boundary"
    boundary.mkdir(parents=True)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    inspected = manager.inspect(boundary)
    manager.initialize(
        boundary,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="boundary-children",
    )
    children = [boundary / f"child-{index}" for index in range(3)]
    for child in children:
        child.mkdir()
    listed = _instrument_scandir(monkeypatch)

    result = manager.inspect(parent)

    assert result.status == "contains_nested_folder"
    assert [entry["folder_name"] for entry in result.boundaries] == ["boundary"]
    # The boundary's own listing is what identifies it, and the children that
    # listing yields belong to its store, so none of them is queued.
    assert _count_listings(listed, boundary) == 1
    assert [_count_listings(listed, child) for child in children] == [0, 0, 0]


def test_a_component_child_without_a_store_is_not_a_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    bare = parent / "bare"
    (bare / ".wbuddy").mkdir(parents=True)
    (bare / "interior").mkdir()
    partial = parent / "partial"
    (partial / ".wbuddy" / "cowork").mkdir(parents=True)
    (partial / "interior").mkdir()
    listed = _instrument_scandir(monkeypatch)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(parent)

    assert result.status == "uninitialized"
    assert result.boundaries == ()
    assert _count_listings(listed, bare / "interior") == 1
    assert _count_listings(listed, partial / "interior") == 1


def test_a_directory_naming_no_component_child_is_never_probed_for_a_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    (parent / "plain" / "deeper").mkdir(parents=True)
    (parent / "notes.md").write_text("throwaway\n", encoding="utf-8")
    holder = parent / "holder"
    (holder / ".wbuddy").mkdir(parents=True)
    probed: list[str] = []
    real_probe = project_store._is_store_root

    def record(scan_root: Path, directory: str) -> bool:
        probed.append(Path(directory).name)
        return real_probe(scan_root, directory)

    monkeypatch.setattr(project_store, "_is_store_root", record)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(parent)

    assert result.status == "uninitialized"
    # A listing that never names the component child answers the boundary
    # question by itself, so only the one directory that names it is probed.
    assert probed == ["holder"]


def test_a_redirected_component_child_is_refused_without_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    descendant = parent / "descendant"
    descendant.mkdir(parents=True)
    target = tmp_path / "external"
    interior = target / "cowork"
    interior.mkdir(parents=True)
    (interior / "store.yaml").write_text("store_id: outside\n", encoding="utf-8")
    link = descendant / ".wbuddy"
    _make_directory_redirect(link, target)
    listed = _instrument_scandir(monkeypatch)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    try:
        with pytest.raises(FolderLifecycleError) as failure:
            manager.inspect(parent)
    finally:
        _remove_directory_redirect(link)

    assert failure.value.code == "folder_layout_incomplete"
    assert _count_listings(listed, target) == 0
    assert _count_listings(listed, interior) == 0


def test_a_file_entry_is_classified_without_reading_its_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "notes.md").write_text("throwaway\n", encoding="utf-8")
    (folder / "sibling").mkdir()

    def hide_metadata(entries: list[object]) -> list[object]:
        return [
            _FileEntryWithoutMetadata(entry.name)
            if entry.name == "notes.md"
            else entry
            for entry in entries
        ]

    listed = _instrument_scandir(monkeypatch, directory=folder, rewrite=hide_metadata)
    manager = ProjectStoreManager(data_root=tmp_path / "machine")

    result = manager.inspect(folder)

    assert result.status == "uninitialized"
    assert _count_listings(listed, folder / "sibling") == 1


def test_a_continuation_page_reuses_the_settled_ownership_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    for index in range(4):
        (folder / f"child-{index}").mkdir()
    manager = ProjectStoreManager(data_root=tmp_path / "machine", scan_work_per_page=1)

    first = manager.inspect(folder)
    assert first.status == "inspection_pending"

    owner_calls: list[Path] = []
    real_owner = ProjectStoreManager._nearest_owner

    def record(self: ProjectStoreManager, root: Path) -> object:
        owner_calls.append(root)
        return real_owner(self, root)

    monkeypatch.setattr(ProjectStoreManager, "_nearest_owner", record)
    result = manager.inspect(folder, continuation_token=first.continuation_token)
    while result.status == "inspection_pending":
        result = manager.inspect(folder, continuation_token=result.continuation_token)

    assert result.status == "uninitialized"
    # The page that minted the token proved no ancestor owns the folder, so a
    # continuation asks the ancestors nothing.
    assert owner_calls == []


def test_scan_cursors_outlive_their_token_no_longer_than_its_lifetime(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    for index in range(4):
        (folder / f"child-{index}").mkdir()
    machine = tmp_path / "machine"
    scan_dir = ProjectStoreManager(data_root=machine).scan_dir
    scan_dir.mkdir(parents=True, exist_ok=True)
    abandoned = scan_dir / f"{'a' * 32}.json"
    live = scan_dir / f"{'b' * 32}.json"
    for cursor in (abandoned, live):
        cursor.write_text("{}", encoding="utf-8")
    expired = time.time() - DEFAULT_TOKEN_TTL_SECONDS - 60
    os.utime(abandoned, (expired, expired))

    manager = ProjectStoreManager(data_root=machine, scan_work_per_page=1)
    assert abandoned.exists(), "constructing a manager stays free of filesystem work"

    result = manager.inspect(folder)

    assert result.status == "inspection_pending"
    assert not abandoned.exists()
    assert live.exists()


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


def test_setup_without_a_prior_observation_names_an_oversized_folder(
    tmp_path: Path,
) -> None:
    """A caller that showed the folder to nobody is refused on what it is.

    The walk inside the folder operation locks classifies the folder as too
    large to prove anything about, and the refusal carries that
    classification's own code and an action the caller can take, instead of
    reporting a change nobody made.
    """

    folder = tmp_path / "wide-folder"
    folder.mkdir()
    for index in range(4):
        (folder / f"item-{index}").write_text("x", encoding="utf-8")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        # Opening the folder fits the threshold; the entries it lists do not.
        scan_work_limit=project_store._SCAN_DIRECTORY_WEIGHT,
    )

    with pytest.raises(FolderLifecycleError) as failure:
        manager.initialize(
            folder,
            registry=registry,
            inspection_fingerprint=None,
            idempotency_key="oversized-unobserved",
        )

    assert failure.value.code == "folder_too_large_for_safe_setup"
    assert failure.value.retryable is False
    message = str(failure.value)
    assert "too many items" in message
    assert "narrower folder" in message
    assert not (folder / ".wbuddy").exists()
    assert registry.list_stores(refresh=False) == ()


def test_setup_after_an_observation_reports_the_folder_changing(
    tmp_path: Path,
) -> None:
    """A caller holding a fingerprint is refused in the fingerprint's terms.

    The fingerprint says a human was shown this folder, so the refusal speaks
    to that observation rather than to the classification. The branch is
    chosen by the presence of the observation, not by the folder's status,
    which is why the same oversized folder answers both ways.
    """

    folder = tmp_path / "observed-wide-folder"
    folder.mkdir()
    for index in range(4):
        (folder / f"item-{index}").write_text("x", encoding="utf-8")
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    manager = ProjectStoreManager(
        data_root=tmp_path / "machine",
        scan_work_limit=project_store._SCAN_DIRECTORY_WEIGHT,
    )
    inspected = manager.inspect(folder, complete_scan=True)
    assert inspected.status == "unavailable"
    assert inspected.fingerprint is not None

    with pytest.raises(FolderLifecycleError) as failure:
        manager.initialize(
            folder,
            registry=registry,
            inspection_fingerprint=inspected.fingerprint,
            idempotency_key="oversized-observed",
        )

    assert failure.value.code == "folder_changed"
    assert "no longer available for setup" in str(failure.value)
    assert not (folder / ".wbuddy").exists()
    assert registry.list_stores(refresh=False) == ()


def test_setup_without_a_prior_observation_still_refuses_an_initialized_folder(
    tmp_path: Path,
) -> None:
    """Dropping the fingerprint drops no protection.

    Safety rests on the walk under the folder operation locks, which
    classifies the folder from the filesystem and refuses anything that is not
    an empty candidate. A caller passing no fingerprint is refused by that
    walk and leaves the folder byte-identical.
    """

    folder = tmp_path / "already-set-up"
    folder.mkdir()
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    inspected = manager.inspect(folder)
    seeded = manager.initialize(
        folder,
        registry=registry,
        inspection_fingerprint=inspected.fingerprint or "",
        idempotency_key="seed-initialized",
    )
    before = _tree_bytes(folder)

    with pytest.raises(FolderLifecycleError) as failure:
        manager.initialize(
            folder,
            registry=registry,
            inspection_fingerprint=None,
            idempotency_key="initialized-unobserved",
        )

    assert failure.value.code == "folder_already_initialized"
    assert failure.value.retryable is False
    assert "already set up" in str(failure.value)
    assert _tree_bytes(folder) == before
    assert [row.store_id for row in registry.list_stores(refresh=False)] == [
        seeded.store_id
    ]


def test_settled_setup_refusals_stay_out_of_the_transient_retry_vocabulary() -> None:
    """A refusal about a settled fact must not read as worth retrying.

    ``FolderLifecycleError`` is a ``RuntimeError``, and error classification
    falls back to reading a ``RuntimeError``'s message for transient wording.
    A refusal whose prose matched would be queued for retry against a folder
    whose answer cannot change without the user acting on it.
    """

    from work_buddy.errors import classify_error

    folder = Path("/scan-target")
    classifications = (
        project_store.FolderInspection("initialized", folder, folder.name),
        project_store.FolderInspection("inside_existing_folder", folder, folder.name),
        project_store.FolderInspection(
            "contains_nested_folder",
            folder,
            folder.name,
            reason_code="contains_nested_folder",
        ),
        project_store.FolderInspection(
            "unavailable",
            folder,
            folder.name,
            reason_code="folder_too_large_for_safe_setup",
        ),
        project_store.FolderInspection(
            "collision",
            folder,
            folder.name,
            reason_code="folder_layout_incomplete",
        ),
        project_store.FolderInspection(
            "collision",
            folder,
            folder.name,
            reason_code="identity_conflict",
        ),
    )

    for classification in classifications:
        assert (
            classification.reason_code
            not in project_store._RETRYABLE_SETUP_REFUSALS
        )
        refusal = project_store._setup_refusal(classification, observed=False)
        assert refusal.retryable is False, str(refusal)
        assert refusal.status == 409, str(refusal)
        assert classify_error(refusal) != "transient", str(refusal)


def test_an_unread_folder_is_refused_as_unread_rather_than_as_broken() -> None:
    """Failing to read the folder is a different answer from reading it.

    ``folder_unreachable`` says the Work Buddy data could not be read, so
    nothing is known about whether that data is complete. Answering it with the
    collision prose would tell an agent to repair a store that was only held
    open for a moment, and an agent reads the exception text and nothing else.
    """

    from work_buddy.errors import classify_error

    folder = Path("/scan-target")
    unreachable = project_store.FolderInspection(
        "collision",
        folder,
        folder.name,
        reason_code="folder_unreachable",
    )

    refusal = project_store._setup_refusal(unreachable, observed=False)

    assert refusal.code == "folder_unreachable"
    assert refusal.retryable is True
    assert refusal.status == 503
    message = str(refusal)
    assert "temporarily unavailable" in message
    assert "again in a moment" in message
    assert "Repair" not in message
    # The retry queue reads a ``RuntimeError``'s message, and this one names a
    # failure that clears on its own.
    assert classify_error(refusal) == "transient"


def test_contradictory_store_records_are_not_called_incomplete() -> None:
    """An identity conflict is its own repair, not an incomplete layout.

    The records were read; they disagree. Folding that into the incomplete
    layout prose would send a caller looking for missing data instead of
    contradictory data.
    """

    folder = Path("/scan-target")
    conflict = project_store.FolderInspection(
        "collision",
        folder,
        folder.name,
        reason_code="identity_conflict",
    )

    refusal = project_store._setup_refusal(conflict, observed=False)

    assert refusal.code == "identity_conflict"
    assert refusal.retryable is False
    assert refusal.status == 409
    message = str(refusal)
    assert "disagree about which store" in message
    assert "does not form a" not in message


def test_setup_refuses_an_unreadable_manifest_as_a_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path from a failed read to the caller's refusal stays transient.

    A healthy folder whose manifest is momentarily locked by another process
    classifies as ``folder_unreachable``, and setup is the caller that turns a
    classification into an exception. The refusal it hands back has to keep
    the retryable 503 the classification carried, because the very next
    attempt is expected to succeed.
    """

    folder = tmp_path / "locked-manifest"
    (folder / ".wbuddy").mkdir(parents=True)
    manifest = folder / ".wbuddy" / "manifest.yaml"
    manifest.write_text(
        "format: wbuddy-folder/v1\ncomponents:\n  cowork: {path: cowork}\n",
        encoding="utf-8",
    )
    registry = TruthStoreRegistry(tmp_path / "registry.db")
    manager = ProjectStoreManager(data_root=tmp_path / "machine")
    real_read_bytes = Path.read_bytes

    def refuse_the_manifest(self: Path) -> bytes:
        if self.name == "manifest.yaml":
            raise OSError(errno.EACCES, "manifest held open by another process")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refuse_the_manifest)

    with pytest.raises(FolderLifecycleError) as failure:
        manager.initialize(
            folder,
            registry=registry,
            inspection_fingerprint=None,
            idempotency_key="unreadable-manifest",
        )

    assert failure.value.code == "folder_unreachable"
    assert failure.value.retryable is True
    assert failure.value.status == 503
    assert "temporarily unavailable" in str(failure.value)
    assert not (folder / ".wbuddy" / "cowork").exists()
    assert registry.list_stores(refresh=False) == ()


def test_every_retryable_setup_refusal_is_a_code_setup_can_reach() -> None:
    """The retryable set has to name codes that reach ``_setup_refusal``.

    Setup composes its refusal from a ``FolderInspection``, so a code reaches
    the lookup only if a classification carries it as a ``reason_code``. A code
    that exists only as a raised exception makes the lookup unconditionally
    false and leaves the retryable branch dead, so the set is pinned against
    the reason codes a classification can actually carry.
    """

    classifiable_reason_codes = frozenset(
        {
            # Redirected or malformed Work Buddy data under the folder.
            "folder_layout_incomplete",
            # The data could not be read at all.
            "folder_unreachable",
            # The data was read and contradicts itself.
            "identity_conflict",
            # The descendant walk stopped at the work threshold.
            "folder_too_large_for_safe_setup",
            # The walk found a store root beneath the folder.
            "contains_nested_folder",
        }
    )
    retryable = project_store._RETRYABLE_SETUP_REFUSALS

    assert retryable
    assert retryable <= classifiable_reason_codes

    folder = Path("/scan-target")
    for code in retryable:
        refusal = project_store._setup_refusal(
            project_store.FolderInspection(
                "collision",
                folder,
                folder.name,
                reason_code=code,
            ),
            observed=False,
        )
        assert refusal.code == code
        assert refusal.retryable is True
        assert refusal.status == 503
