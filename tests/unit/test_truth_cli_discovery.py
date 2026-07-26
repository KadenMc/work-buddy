from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.cli.truth import discover_store
from work_buddy.truth.contracts import TruthError


def _profile(sidecar: Path) -> Path:
    sidecar.mkdir(parents=True)
    (sidecar / "store.yaml").write_text("store_id: fixture\n", encoding="utf-8")
    return sidecar


def test_discovers_canonical_cowork_data_from_nested_folder(tmp_path: Path) -> None:
    canonical = _profile(tmp_path / ".wbuddy" / "cowork")
    nested = tmp_path / "notes" / "drafts"
    nested.mkdir(parents=True)

    assert discover_store(cwd=nested) == canonical.resolve()
    assert discover_store(tmp_path) == canonical.resolve()
    assert discover_store(canonical) == canonical.resolve()


def test_discovery_rejects_redirected_managed_directory(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _profile(external / "cowork")
    folder = tmp_path / "folder"
    folder.mkdir()
    try:
        (folder / ".wbuddy").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(TruthError, match="redirected or unsupported"):
        discover_store(folder)
    with pytest.raises(TruthError, match="redirected or unsupported"):
        discover_store(folder / ".wbuddy" / "cowork")
