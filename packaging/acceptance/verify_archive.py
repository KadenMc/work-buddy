"""Black-box structural verification for cross-platform release tarballs."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

ROOT = PurePosixPath("work-buddy")
COMMON_REQUIRED = {
    ROOT / "payload" / "pyproject.toml",
    ROOT / "payload" / "work_buddy" / "__init__.py",
    ROOT / "payload" / "dashboard-react" / "dist" / "index.html",
    ROOT / "payload" / "dashboard-react" / "dist" / "icons" / "app-1024.png",
    ROOT / "payload" / "vendor" / "uv",
}
PLATFORM_REQUIRED = {
    "linux": {
        ROOT / "install.sh",
        ROOT / "uninstall.sh",
    },
    "macos": {
        ROOT / "install.command",
        ROOT / "uninstall.command",
        ROOT / "app" / "Work Buddy.app" / "Contents" / "Info.plist",
        ROOT / "app" / "Work Buddy.app" / "Contents" / "MacOS" / "work-buddy-launcher",
        ROOT / "app" / "Work Buddy.app" / "Contents" / "Resources" / "work-buddy.icns",
    },
}
EXECUTABLES = {
    "linux": {
        ROOT / "install.sh",
        ROOT / "uninstall.sh",
        ROOT / "payload" / "vendor" / "uv",
    },
    "macos": {
        ROOT / "install.command",
        ROOT / "uninstall.command",
        ROOT / "payload" / "vendor" / "uv",
        ROOT / "app" / "Work Buddy.app" / "Contents" / "MacOS" / "work-buddy-launcher",
    },
}
FORBIDDEN_EXACT = {
    ROOT / "payload" / ".env",
    ROOT / "payload" / "config.local.yaml",
    ROOT / "payload" / "knowledge" / "store.local",
    ROOT / "payload" / ".claude" / "settings.local.json",
}
FORBIDDEN_PARTS = {".git", ".data", "__pycache__"}


class VerificationError(ValueError):
    """The release artifact violates a structural or privacy invariant."""


def _safe_member_name(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise VerificationError(f"unsafe archive path: {raw}")
    if path.parts[0] != ROOT.name:
        raise VerificationError(f"archive member is outside {ROOT}: {raw}")
    return path


def _member_bytes(tf: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = tf.extractfile(member)
    if stream is None:
        raise VerificationError(f"could not read archive member: {member.name}")
    return stream.read()


def verify_archive(archive: Path, platform: str, version: str) -> dict:
    """Verify one release tarball and return evidence suitable for CI logs."""
    if platform not in PLATFORM_REQUIRED:
        raise VerificationError(f"unsupported platform: {platform}")
    if not archive.is_file():
        raise VerificationError(f"artifact does not exist: {archive}")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    members: dict[PurePosixPath, tarfile.TarInfo] = {}
    with tarfile.open(archive, mode="r:gz") as tf:
        for member in tf.getmembers():
            path = _safe_member_name(member.name)
            if path in members:
                raise VerificationError(f"duplicate archive member: {path}")
            if member.issym() or member.islnk():
                raise VerificationError(f"links are not permitted in release artifacts: {path}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise VerificationError(f"special file is not permitted: {path}")
            if FORBIDDEN_PARTS.intersection(path.parts):
                raise VerificationError(f"development/private path shipped: {path}")
            if path.suffix in {".pyc", ".pyo"}:
                raise VerificationError(f"bytecode shipped: {path}")
            members[path] = member

        forbidden = sorted(str(path) for path in FORBIDDEN_EXACT if path in members)
        if forbidden:
            raise VerificationError("private files shipped: " + ", ".join(forbidden))

        required = COMMON_REQUIRED | PLATFORM_REQUIRED[platform]
        missing = sorted(str(path) for path in required if path not in members)
        if missing:
            raise VerificationError("required files missing: " + ", ".join(missing))

        non_executable = sorted(
            str(path)
            for path in EXECUTABLES[platform]
            if not (members[path].mode & 0o111)
        )
        if non_executable:
            raise VerificationError("required files are not executable: " + ", ".join(non_executable))

        project_data = tomllib.loads(
            _member_bytes(tf, members[ROOT / "payload" / "pyproject.toml"]).decode("utf-8")
        )
        embedded_version = str(project_data.get("project", {}).get("version", ""))
        if embedded_version != version:
            raise VerificationError(
                f"artifact version mismatch: expected {version}, embedded {embedded_version or '<missing>'}"
            )

        if platform == "macos":
            plist = _member_bytes(
                tf,
                members[ROOT / "app" / "Work Buddy.app" / "Contents" / "Info.plist"],
            ).decode("utf-8")
            if "__APP_VERSION__" in plist or f"<string>{version}</string>" not in plist:
                raise VerificationError("macOS app bundle version was not materialized")

    expected_name = f"work-buddy-{version}-{'linux-x86_64' if platform == 'linux' else 'macos-arm64'}.tar.gz"
    if archive.name != expected_name:
        raise VerificationError(
            f"artifact filename mismatch: expected {expected_name}, got {archive.name}"
        )

    return {
        "artifact": str(archive),
        "platform": platform,
        "version": version,
        "sha256": digest,
        "members": len(members),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORM_REQUIRED))
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence")
    args = parser.parse_args(argv)

    evidence = verify_archive(args.archive.resolve(), args.platform, args.version)
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    print(rendered)
    if args.evidence:
        Path(args.evidence).write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
