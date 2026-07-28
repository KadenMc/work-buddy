"""Security regression tests for backup archive extraction."""

from __future__ import annotations

import io
import tarfile

import pytest

from work_buddy.backups.restore import RestoreFailed, _safe_extract_tar


def _archive(tmp_path, members):
    path = tmp_path / "snapshot.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, payload, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode()
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tf.addfile(info)
    return path


def test_safe_extract_accepts_regular_files(tmp_path):
    archive = _archive(tmp_path, [("db/tasks.db", "sqlite", "file")])
    destination = tmp_path / "staging"
    destination.mkdir()

    with tarfile.open(archive, "r:gz") as tf:
        _safe_extract_tar(tf, destination)

    assert (destination / "db" / "tasks.db").read_text() == "sqlite"


@pytest.mark.parametrize("name", ["../escaped.db", "/tmp/escaped.db"])
def test_safe_extract_rejects_paths_outside_destination(tmp_path, name):
    archive = _archive(tmp_path, [(name, "payload", "file")])
    destination = tmp_path / "staging"
    destination.mkdir()

    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(RestoreFailed, match="Unsafe backup member path"):
            _safe_extract_tar(tf, destination)

    assert not (tmp_path / "escaped.db").exists()


def test_safe_extract_rejects_links(tmp_path):
    archive = _archive(
        tmp_path,
        [("db/link", "../../outside", "symlink")],
    )
    destination = tmp_path / "staging"
    destination.mkdir()

    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(RestoreFailed, match="Unsupported backup member"):
            _safe_extract_tar(tf, destination)
