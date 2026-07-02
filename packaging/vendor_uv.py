"""Fetch a pinned uv binary to bundle into a native installer.

Downloads the uv release archive for a target OS and extracts the ``uv`` (or
``uv.exe``) binary into ``--out``. Runs in CI (which has network); the installer
bundles the result so the user's install needs no separate uv-acquisition step
before the bootstrap can run.

The pinned version MUST be a real uv release; keep it in sync with the
``astral-sh/setup-uv`` version the release CI uses. Bump deliberately.

Usage:  python packaging/vendor_uv.py --target windows --out dist/payload/vendor
"""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
import urllib.request
import zipfile
from pathlib import Path

UV_VERSION = "0.11.26"

# target -> (release triple, archive extension, binary name inside the archive)
TARGETS = {
    "windows": ("x86_64-pc-windows-msvc", "zip", "uv.exe"),
    "linux": ("x86_64-unknown-linux-gnu", "tar.gz", "uv"),
    "linux-arm64": ("aarch64-unknown-linux-gnu", "tar.gz", "uv"),
    "macos": ("aarch64-apple-darwin", "tar.gz", "uv"),
    "macos-x86": ("x86_64-apple-darwin", "tar.gz", "uv"),
}


def release_url(target: str, version: str = UV_VERSION) -> str:
    triple, ext, _ = TARGETS[target]
    return (
        f"https://github.com/astral-sh/uv/releases/download/{version}/"
        f"uv-{triple}.{ext}"
    )


def _extract(data: bytes, target: str, out: Path) -> Path:
    _triple, ext, binary = TARGETS[target]
    out.mkdir(parents=True, exist_ok=True)
    dest = out / binary
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(binary))
            with zf.open(member) as src, open(dest, "wb") as fh:
                fh.write(src.read())
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/" + binary) or m.name == binary)
            src = tf.extractfile(member)
            assert src is not None
            with open(dest, "wb") as fh:
                fh.write(src.read())
    dest.chmod(0o755)
    return dest


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (pinned github url)
        return resp.read()


def _verify_checksum(data: bytes, sha256_line: str) -> None:
    """Raise if ``data``'s SHA-256 does not match the published checksum line.

    uv's ``.sha256`` files are ``<hexdigest>  <filename>``; take the first token.
    """
    expected = sha256_line.split()[0].strip().lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"uv checksum mismatch: expected {expected}, got {actual}")


def vendor_uv(target: str, out: Path, version: str = UV_VERSION, verify: bool = True) -> Path:
    url = release_url(target, version)
    data = _download(url)
    if verify:
        _verify_checksum(data, _download(url + ".sha256").decode("utf-8"))
    return _extract(data, target, out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vendor a pinned uv binary.")
    ap.add_argument("--target", required=True, choices=sorted(TARGETS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default=UV_VERSION)
    args = ap.parse_args(argv)
    dest = vendor_uv(args.target, Path(args.out), args.version)
    print(f"vendored uv {args.version} ({args.target}) -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
