"""Typed registry for files Co-work can import into managed documents.

The native picker is intentionally format-agnostic.  This registry is the
single admission boundary that maps a selected file suffix to the importer
contract the caller must use.  Adding another format therefore extends this
registry and its importer implementation without changing the picker route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MARKDOWN_IMPORTER_ID = "markdown/v1"
MARKDOWN_MEDIA_TYPE = "text/markdown"
MARKDOWN_SUFFIXES = (".md", ".markdown")
MARKDOWN_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_IMPORTER_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}/v[1-9][0-9]{0,8}$")
_SOURCE_FORMAT_RE = re.compile(r"^[a-z][a-z0-9._-]{0,39}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._+-]{0,15}$")


@dataclass(frozen=True, slots=True)
class FileImporter:
    """One versioned file-to-Co-work conversion contract."""

    importer_id: str
    suffixes: tuple[str, ...]
    media_type: str
    max_source_bytes: int
    display_name: str | None = None
    source_format: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.importer_id, str)
            or not _IMPORTER_ID_RE.fullmatch(self.importer_id)
        ):
            raise ValueError(
                "importer_id must be a lowercase versioned identifier such as markdown/v1"
            )
        if (
            not isinstance(self.media_type, str)
            or not _MEDIA_TYPE_RE.fullmatch(self.media_type)
        ):
            raise ValueError("media_type must be a valid bounded media type")
        if (
            isinstance(self.max_source_bytes, bool)
            or not isinstance(self.max_source_bytes, int)
            or self.max_source_bytes <= 0
        ):
            raise ValueError("max_source_bytes must be a positive integer")
        if not isinstance(self.suffixes, tuple) or any(
            not isinstance(suffix, str) for suffix in self.suffixes
        ):
            raise ValueError("importer suffixes must be a tuple of strings")
        normalized = tuple(suffix.casefold() for suffix in self.suffixes)
        if (
            not normalized
            or len(set(normalized)) != len(normalized)
            or any(not _SUFFIX_RE.fullmatch(suffix) for suffix in normalized)
        ):
            raise ValueError(
                "importer suffixes must be unique bounded ASCII dotted extensions"
            )
        display_name = self.display_name
        if display_name is None:
            display_name = self.importer_id.partition("/")[0].replace("-", " ").title()
        if (
            not isinstance(display_name, str)
            or display_name != display_name.strip()
            or not display_name
            or len(display_name) > 80
            or any(ord(character) < 32 for character in display_name)
        ):
            raise ValueError("display_name must contain 1-80 printable characters")
        source_format = self.source_format
        if source_format is None:
            source_format = self.importer_id.partition("/")[0]
        if (
            not isinstance(source_format, str)
            or not _SOURCE_FORMAT_RE.fullmatch(source_format)
        ):
            raise ValueError("source_format must be a bounded lowercase identifier")
        object.__setattr__(self, "suffixes", normalized)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "source_format", source_format)

    def descriptor(self) -> dict[str, object]:
        """Return the immutable public source-format descriptor."""

        return {
            "importer_id": self.importer_id,
            "display_name": self.display_name,
            "source_format": self.source_format,
            "media_type": self.media_type,
            "suffixes": list(self.suffixes),
            "max_source_bytes": self.max_source_bytes,
        }


@dataclass(frozen=True, slots=True)
class FilePickerSpec:
    """Validated presentation metadata for a native supported-file picker."""

    display_name: str
    suffixes: tuple[str, ...]

    @property
    def patterns(self) -> tuple[str, ...]:
        return tuple(f"*{suffix}" for suffix in self.suffixes)

    @property
    def extension_names(self) -> tuple[str, ...]:
        return tuple(suffix[1:] for suffix in self.suffixes)


@dataclass(frozen=True, slots=True)
class FileImportSelection:
    """A safely admitted file plus the importer selected for it."""

    path: str
    importer_id: str
    media_type: str
    source_sha256: str
    display_name: str
    source_format: str
    suffixes: tuple[str, ...]
    max_source_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "importer_id": self.importer_id,
            "media_type": self.media_type,
            "source_sha256": self.source_sha256,
            "importer": {
                "importer_id": self.importer_id,
                "display_name": self.display_name,
                "source_format": self.source_format,
                "media_type": self.media_type,
                "suffixes": list(self.suffixes),
                "max_source_bytes": self.max_source_bytes,
            },
        }


class FileImporterRegistry:
    """Resolve supported file suffixes to stable, versioned importers."""

    def __init__(self, importers: tuple[FileImporter, ...]) -> None:
        if not importers:
            raise ValueError("at least one file importer is required")
        by_id: dict[str, FileImporter] = {}
        by_suffix: dict[str, FileImporter] = {}
        for importer in importers:
            if importer.importer_id in by_id:
                raise ValueError(f"duplicate importer id: {importer.importer_id}")
            by_id[importer.importer_id] = importer
            for suffix in importer.suffixes:
                if suffix in by_suffix:
                    raise ValueError(f"duplicate importer suffix: {suffix}")
                by_suffix[suffix] = importer
        self._by_id = by_id
        self._by_suffix = by_suffix

    @property
    def importers(self) -> tuple[FileImporter, ...]:
        return tuple(self._by_id.values())

    @property
    def supported_suffixes(self) -> tuple[str, ...]:
        return tuple(self._by_suffix)

    @property
    def maximum_source_bytes(self) -> int:
        """Largest bounded source accepted by any registered importer."""

        return max(importer.max_source_bytes for importer in self._by_id.values())

    def importer_by_id(self, importer_id: str) -> FileImporter | None:
        return self._by_id.get(importer_id)

    def importer_for_path(self, path: str | Path) -> FileImporter | None:
        return self._by_suffix.get(Path(path).suffix.casefold())

    def resolve_binding(
        self,
        path: str | Path,
        *,
        importer_id: str,
    ) -> FileImporter | None:
        """Resolve only when the frozen importer ID owns the selected suffix."""

        importer = self.importer_by_id(importer_id)
        if importer is None:
            return None
        return (
            importer
            if Path(path).suffix.casefold() in importer.suffixes
            else None
        )

    def picker_spec(self) -> FilePickerSpec:
        return FilePickerSpec(
            display_name="Supported files",
            suffixes=self.supported_suffixes,
        )

    def selection_for_path(
        self,
        path: str | Path,
        *,
        relative_path: str,
        source_sha256: str,
    ) -> FileImportSelection | None:
        importer = self.importer_for_path(path)
        if importer is None:
            return None
        return FileImportSelection(
            path=relative_path,
            importer_id=importer.importer_id,
            media_type=importer.media_type,
            source_sha256=source_sha256,
            display_name=str(importer.display_name),
            source_format=str(importer.source_format),
            suffixes=importer.suffixes,
            max_source_bytes=importer.max_source_bytes,
        )


MARKDOWN_FILE_IMPORTER = FileImporter(
    importer_id=MARKDOWN_IMPORTER_ID,
    suffixes=MARKDOWN_SUFFIXES,
    media_type=MARKDOWN_MEDIA_TYPE,
    max_source_bytes=MARKDOWN_MAX_SOURCE_BYTES,
    display_name="Markdown",
    source_format="markdown",
)
DEFAULT_FILE_IMPORTERS = FileImporterRegistry((MARKDOWN_FILE_IMPORTER,))


__all__ = [
    "DEFAULT_FILE_IMPORTERS",
    "FileImportSelection",
    "FileImporter",
    "FileImporterRegistry",
    "FilePickerSpec",
    "MARKDOWN_FILE_IMPORTER",
    "MARKDOWN_IMPORTER_ID",
    "MARKDOWN_MAX_SOURCE_BYTES",
    "MARKDOWN_MEDIA_TYPE",
    "MARKDOWN_SUFFIXES",
]
