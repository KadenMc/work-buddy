"""Tests for the installer build scripts (build_payload, vendor_uv). No network.

The scripts live in ``packaging/`` (not an importable package), so they are
loaded by path. Network downloads are never performed: only URL construction and
archive extraction (against synthetic archives) are exercised.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / "packaging" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_payload = _load("build_payload")
vendor_uv = _load("vendor_uv")
version = _load("version")


def test_version_reads_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    assert version.read_version(tmp_path) == "1.2.3"


def test_version_matches_real_pyproject():
    # The real repo's version must be readable (guards against a malformed bump).
    v = version.read_version(_REPO)
    assert v and v[0].isdigit()


def test_build_payload_includes_package_excludes_dev_trees(tmp_path):
    root = tmp_path / "repo"
    (root / "work_buddy").mkdir(parents=True)
    (root / "work_buddy" / "__init__.py").write_text("x")
    (root / "work_buddy" / "__pycache__").mkdir()
    (root / "work_buddy" / "__pycache__" / "x.pyc").write_text("junk")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("junk")
    (root / "pyproject.toml").write_text("[tool]")

    out = tmp_path / "payload"
    summary = build_payload.build_payload(root, out)

    assert (out / "work_buddy" / "__init__.py").exists()
    assert (out / "pyproject.toml").exists()
    assert not (out / "tests").exists()  # dev tree excluded by the allowlist
    assert not (out / "work_buddy" / "__pycache__").exists()  # bytecode pruned
    assert "work_buddy" in summary["copied"]


def test_build_payload_refuses_live_tree(tmp_path):
    root = tmp_path / "repo"
    (root / "work_buddy").mkdir(parents=True)
    (root / "work_buddy" / "__init__.py").write_text("x")
    (root / "knowledge" / "store.local").mkdir(parents=True)  # private, gitignored
    with pytest.raises(ValueError, match="live working tree"):
        build_payload.build_payload(root, tmp_path / "payload")


def test_build_payload_recreates_out(tmp_path):
    root = tmp_path / "repo"
    (root / "work_buddy").mkdir(parents=True)
    (root / "work_buddy" / "__init__.py").write_text("x")
    out = tmp_path / "payload"
    out.mkdir()
    (out / "stale.txt").write_text("should be wiped")
    build_payload.build_payload(root, out)
    assert not (out / "stale.txt").exists()


def test_windows_installer_passes_harness_to_bootstrap_and_provision():
    iss = (_REPO / "packaging" / "windows" / "work-buddy.iss").read_text(
        encoding="utf-8"
    )
    bootstrap = (_REPO / "packaging" / "windows" / "bootstrap.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Name: "harness_claudecode"' in iss
    assert (
        'Name: "harness_codexcli"; Description: "Set up for Codex"; '
        "Flags: exclusive"
    ) in iss
    assert (
        'Name: "harness_none"; Description: "Skip agent harness setup"; '
        "Flags: exclusive"
    ) in iss
    assert (
        'Name: "harness_claudecode"; Description: "Set up for Claude Code '
        '(recommended)"; Flags: exclusive checkedonce'
    ) in iss
    assert "WizardIsTaskSelected('harness_codexcli')" in iss
    assert "Result := 'codexcli'" in iss
    assert '-Harness ""{code:HarnessFlag}""' in iss
    assert "[string]$Harness" in bootstrap
    assert 'if ($Harness)      { $provArgs += @("--harness", $Harness) }' in bootstrap
    assert "requires rulesync via Node/npm" not in iss


def test_vendor_uv_url_construction():
    assert vendor_uv.release_url("windows", "0.5.29") == (
        "https://github.com/astral-sh/uv/releases/download/0.5.29/"
        "uv-x86_64-pc-windows-msvc.zip"
    )
    assert vendor_uv.release_url("linux").endswith("uv-x86_64-unknown-linux-gnu.tar.gz")
    assert vendor_uv.release_url("macos").endswith("uv-aarch64-apple-darwin.tar.gz")


def test_vendor_uv_extracts_zip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("uv.exe", b"BINARY")
    dest = vendor_uv._extract(buf.getvalue(), "windows", tmp_path)
    assert dest.name == "uv.exe"
    assert dest.read_bytes() == b"BINARY"


def test_vendor_uv_extracts_tar(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"BINARY"
        info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = vendor_uv._extract(buf.getvalue(), "linux", tmp_path)
    assert dest.name == "uv"
    assert dest.read_bytes() == b"BINARY"


def test_vendor_uv_checksum_accepts_match():
    data = b"BINARY"
    digest = hashlib.sha256(data).hexdigest()
    # uv publishes "<hexdigest>  <filename>"; the filename token is ignored.
    vendor_uv._verify_checksum(data, f"{digest}  uv-x86_64-pc-windows-msvc.zip")


def test_vendor_uv_checksum_rejects_mismatch():
    with pytest.raises(ValueError):
        vendor_uv._verify_checksum(b"BINARY", "deadbeef  uv.zip")
