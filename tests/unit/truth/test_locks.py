"""Cross-process Folder/store lock topology."""

from __future__ import annotations

from pathlib import Path
from threading import Thread

import pytest

from work_buddy.truth.locks import (
    exact_folder_lock,
    folder_operation_locks,
    folder_path_key,
    hierarchy_lock,
    migration_store_lock,
    path_lock,
)


def test_external_folder_locks_never_touch_selected_folder(tmp_path: Path) -> None:
    folder = tmp_path / "selected"
    folder.mkdir()
    data_root = tmp_path / "machine"
    before = tuple(folder.iterdir())

    with folder_operation_locks(folder, data_root=data_root):
        assert tuple(folder.iterdir()) == before
        assert (data_root / "runtime" / "cowork-folder-locks" / "hierarchy.lock").is_file()
        exact = (
            data_root
            / "runtime"
            / "cowork-folder-locks"
            / "by-path"
            / f"{folder_path_key(folder)}.lock"
        )
        assert exact.is_file()

    assert tuple(folder.iterdir()) == before


def test_same_lock_times_out_while_live_holder_owns_it(tmp_path: Path) -> None:
    folder = tmp_path / "selected"
    folder.mkdir()
    data_root = tmp_path / "machine"

    with hierarchy_lock(data_root=data_root):
        with pytest.raises(TimeoutError):
            with hierarchy_lock(data_root=data_root, timeout=0.05):
                pass


def test_migration_store_lock_is_reentrant_in_one_operation(tmp_path: Path) -> None:
    folder = tmp_path / "selected"
    folder.mkdir()
    data_root = tmp_path / "machine"

    with migration_store_lock(folder, "store-1", data_root=data_root):
        with migration_store_lock(
            folder,
            "store-1",
            data_root=data_root,
            timeout=0.05,
        ):
            lock_files = tuple(
                (
                    data_root
                    / "runtime"
                    / "cowork-folder-locks"
                    / "by-store"
                ).glob("*.lock")
            )
            assert len(lock_files) == 1


def test_migration_store_lock_remains_exclusive_across_threads(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "selected"
    folder.mkdir()
    data_root = tmp_path / "machine"
    outcomes: list[BaseException | str] = []

    def contend() -> None:
        try:
            with migration_store_lock(
                folder,
                "store-1",
                data_root=data_root,
                timeout=0.05,
            ):
                outcomes.append("acquired")
        except BaseException as error:  # test captures the contender's thread result
            outcomes.append(error)

    with migration_store_lock(folder, "store-1", data_root=data_root):
        contender = Thread(target=contend)
        contender.start()
        contender.join(timeout=1)
        assert not contender.is_alive()

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], TimeoutError)


def test_exact_folder_keys_are_stable_and_distinct(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert folder_path_key(first) == folder_path_key(first / ".")
    assert folder_path_key(first) != folder_path_key(second)


def test_component_path_lock_lives_under_runtime(tmp_path: Path) -> None:
    sidecar = tmp_path / ".wbuddy" / "cowork"
    sidecar.mkdir(parents=True)
    with path_lock(sidecar, "docs/example.md"):
        lock_files = tuple((sidecar / "runtime" / "locks" / "paths").glob("*.lock"))
        assert len(lock_files) == 1
