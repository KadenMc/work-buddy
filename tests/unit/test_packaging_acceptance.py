"""Structural verifier tests use synthetic release tarballs and no network."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = _REPO / "packaging" / "acceptance" / "verify_archive.py"
_SPEC = importlib.util.spec_from_file_location("verify_archive", _MODULE_PATH)
verify_archive = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(verify_archive)


def _add(tf: tarfile.TarFile, name: str, data: bytes = b"x", mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tf.addfile(info, io.BytesIO(data))


def _artifact(tmp_path: Path, platform: str, version: str = "1.2.3") -> Path:
    suffix = "linux-x86_64" if platform == "linux" else "macos-arm64"
    path = tmp_path / f"work-buddy-{version}-{suffix}.tar.gz"
    common = {
        "work-buddy/payload/pyproject.toml": (
            f'[project]\nname = "work-buddy"\nversion = "{version}"\n'.encode(), 0o644
        ),
        "work-buddy/payload/work_buddy/__init__.py": (b"", 0o644),
        "work-buddy/payload/dashboard-react/dist/index.html": (b"<html/>", 0o644),
        "work-buddy/payload/dashboard-react/dist/icons/app-1024.png": (b"PNG", 0o644),
        "work-buddy/payload/vendor/uv": (b"UV", 0o755),
    }
    platform_files = {
        "linux": {
            "work-buddy/install.sh": (b"#!/bin/sh\n", 0o755),
            "work-buddy/uninstall.sh": (b"#!/bin/sh\n", 0o755),
        },
        "macos": {
            "work-buddy/install.command": (b"#!/bin/sh\n", 0o755),
            "work-buddy/uninstall.command": (b"#!/bin/sh\n", 0o755),
            "work-buddy/app/Work Buddy.app/Contents/Info.plist": (
                f"<plist><string>{version}</string></plist>".encode(), 0o644
            ),
            "work-buddy/app/Work Buddy.app/Contents/MacOS/work-buddy-launcher": (
                b"#!/bin/sh\n", 0o755
            ),
            "work-buddy/app/Work Buddy.app/Contents/Resources/work-buddy.icns": (
                b"ICNS", 0o644
            ),
        },
    }
    with tarfile.open(path, "w:gz") as tf:
        for name, (data, mode) in (common | platform_files[platform]).items():
            _add(tf, name, data, mode)
    return path


def _add_to_archive(artifact: Path, name: str, data: bytes = b"x") -> None:
    replacement = artifact.with_name("replacement.tar.gz")
    with tarfile.open(artifact, "r:gz") as source, tarfile.open(replacement, "w:gz") as dest:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isfile() else None
            dest.addfile(member, stream)
        _add(dest, name, data)
    replacement.replace(artifact)


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_verify_archive_accepts_complete_artifact(tmp_path, platform):
    artifact = _artifact(tmp_path, platform)
    evidence = verify_archive.verify_archive(artifact, platform, "1.2.3")
    assert evidence["platform"] == platform
    assert len(evidence["sha256"]) == 64


def test_verify_archive_rejects_traversal(tmp_path):
    artifact = _artifact(tmp_path, "linux")
    _add_to_archive(artifact, "work-buddy/../escape")
    with pytest.raises(verify_archive.VerificationError, match="unsafe archive path"):
        verify_archive.verify_archive(artifact, "linux", "1.2.3")


def test_verify_archive_rejects_private_file(tmp_path):
    artifact = _artifact(tmp_path, "linux")
    _add_to_archive(artifact, "work-buddy/payload/.env", b"SECRET=yes")
    with pytest.raises(verify_archive.VerificationError, match="private files shipped"):
        verify_archive.verify_archive(artifact, "linux", "1.2.3")


def test_verify_archive_rejects_version_mismatch(tmp_path):
    artifact = _artifact(tmp_path, "macos")
    with pytest.raises(verify_archive.VerificationError, match="version mismatch"):
        verify_archive.verify_archive(artifact, "macos", "9.9.9")
