"""Resolve and install the pinned rulesync projection tool."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from work_buddy import paths
from work_buddy.harness.model import HarnessConfig


_RELEASE_ROOT = "https://github.com/dyoshikawa/rulesync/releases/download"


def rulesync_command(
    config: HarnessConfig,
    *,
    install: bool = False,
) -> list[str]:
    """Return a subprocess argv prefix for rulesync.

    A configured command wins. Otherwise prefer an exact-version executable on
    PATH, then work-buddy's checksum-verified managed binary. Installer flows
    pass ``install=True`` to download that binary when absent. ``npx`` remains a
    development fallback and is never required by the native installer.
    """

    if config.rulesync_command:
        return [config.rulesync_command]
    rulesync = shutil.which("rulesync")
    if rulesync and _reports_version([rulesync], config.rulesync_version):
        return [rulesync]

    managed = managed_rulesync_path(config.rulesync_version)
    if managed.is_file() and _reports_version([str(managed)], config.rulesync_version):
        return [str(managed)]

    if install:
        return [str(install_rulesync(config.rulesync_version))]

    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", f"rulesync@{config.rulesync_version}"]
    return ["rulesync"]


def managed_rulesync_path(version: str) -> Path:
    name = "rulesync.exe" if os.name == "nt" else "rulesync"
    return paths.data_dir("tools") / "rulesync" / version / name


def install_rulesync(version: str) -> Path:
    """Download and checksum-verify the platform release binary."""

    target = managed_rulesync_path(version)
    if target.is_file() and _reports_version([str(target)], version):
        return target

    asset = _release_asset_name()
    release = f"v{version}"
    sums_url = f"{_RELEASE_ROOT}/{release}/SHA256SUMS"
    asset_url = f"{_RELEASE_ROOT}/{release}/{asset}"

    try:
        with urlopen(sums_url, timeout=30) as response:
            sums = response.read().decode("utf-8")
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"could not download rulesync checksums: {exc}") from exc

    expected = _checksum_for_asset(sums, asset)
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.with_suffix(target.suffix + ".download")
    digest = hashlib.sha256()
    try:
        with urlopen(asset_url, timeout=120) as response, download.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                fh.write(chunk)
    except (HTTPError, URLError, OSError) as exc:
        download.unlink(missing_ok=True)
        raise RuntimeError(f"could not download rulesync {version}: {exc}") from exc

    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        download.unlink(missing_ok=True)
        raise RuntimeError(
            f"rulesync checksum mismatch for {asset}: expected {expected}, got {actual}"
        )

    download.replace(target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    if not _reports_version([str(target)], version):
        target.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded rulesync binary did not report version {version}")
    return target


def rulesync_status(config: HarnessConfig) -> dict[str, str | bool]:
    command = rulesync_command(config, install=False)
    available = command[0] != "rulesync" or shutil.which("rulesync") is not None
    version = _read_version(command) if available else ""
    return {
        "available": available,
        "command": " ".join(command),
        "version": version,
        "expected_version": config.rulesync_version,
        "version_ok": version == config.rulesync_version,
        "managed_path": str(managed_rulesync_path(config.rulesync_version)),
    }


def _release_asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if system == "windows":
        if arch != "x64":
            raise RuntimeError("rulesync does not publish a Windows ARM64 binary")
        return "rulesync-windows-x64.exe"
    if system == "darwin":
        return f"rulesync-darwin-{arch}"
    if system == "linux":
        return f"rulesync-linux-{arch}"
    raise RuntimeError(f"unsupported rulesync platform: {system}/{machine}")


def _checksum_for_asset(sums: str, asset: str) -> str:
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            return parts[0]
    raise RuntimeError(f"rulesync release checksum is missing {asset}")


def _reports_version(command: list[str], expected: str) -> bool:
    return _read_version(command) == expected


def _read_version(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            [*command, "--version"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    output = (proc.stdout or proc.stderr).strip()
    return output.removeprefix("v").splitlines()[0].strip()
