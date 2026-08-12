"""Crash-safe content-addressed storage for retained source representations."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from work_buddy.sources.errors import SourceIntegrityFailure
from work_buddy.sources.models import sha256_bytes


@dataclass(frozen=True, slots=True)
class BlobRecord:
    sha256: str
    relative_path: str
    byte_length: int


class BlobStore:
    """Store exact bytes by digest without using caller-controlled path parts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise SourceIntegrityFailure()
        path = (self.root / digest[:2] / digest).resolve()
        if self.root not in path.parents:
            raise SourceIntegrityFailure()
        return path

    def put(self, content: bytes) -> BlobRecord:
        digest = sha256_bytes(content)
        path = self.path_for(digest)
        relative = path.relative_to(self.root).as_posix()
        if path.exists():
            existing = path.read_bytes()
            if sha256_bytes(existing) != digest or existing != content:
                raise SourceIntegrityFailure()
            return BlobRecord(digest, relative, len(content))

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{digest}.",
            suffix=".staged",
        )
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._flush_directory(path.parent)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return BlobRecord(digest, relative, len(content))

    def read(self, digest: str, *, expected_length: int | None = None) -> bytes:
        path = self.path_for(digest)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SourceIntegrityFailure() from exc
        if sha256_bytes(content) != digest:
            raise SourceIntegrityFailure()
        if expected_length is not None and len(content) != expected_length:
            raise SourceIntegrityFailure()
        return content

    def delete(self, digest: str) -> None:
        path = self.path_for(digest)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def digests(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {
            item.name
            for item in self.root.glob("*/*")
            if item.is_file() and len(item.name) == 64
        }

    @staticmethod
    def _flush_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
