"""Bounded, no-follow reads of mutable Co-work source files."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from work_buddy.cowork.file_importers import (
    DEFAULT_FILE_IMPORTERS,
    MARKDOWN_IMPORTER_ID,
    FileImporter,
    FileImporterRegistry,
)
from work_buddy.cowork.paths import CoworkPathError, resolve_document_source_path
from work_buddy.truth import documents
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.store import DocumentRecord, TruthStore


class SourceObservationError(InvariantViolation):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BoundedSourceRead:
    path: Path
    sha256: str
    byte_length: int
    data: bytes | None
    importer_id: str | None = None
    max_source_bytes: int | None = None


def _is_link_or_reparse(info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and (
        not left.st_ino or not right.st_ino or left.st_ino == right.st_ino
    )


def _too_large(
    source_label: str,
    *,
    maximum: int,
    observed: int,
    importer_id: str | None,
) -> SourceObservationError:
    details: dict[str, Any] = {
        "max_source_bytes": maximum,
        "source_byte_length": observed,
    }
    if importer_id is not None:
        details["importer_id"] = importer_id
    return SourceObservationError(
        "source_too_large",
        f"{source_label} exceeds the size limit.",
        status=413,
        details=details,
    )


def read_bounded_regular_file(
    path: Path,
    *,
    maximum: int,
    source_label: str,
    importer_id: str | None = None,
    retain_bytes: bool = True,
) -> BoundedSourceRead:
    """Read or hash one regular file through a bounded, no-follow descriptor."""

    try:
        path_before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise SourceObservationError(
            "source_not_found",
            f"{source_label} does not exist.",
            status=404,
        ) from exc
    except OSError as exc:
        raise SourceObservationError(
            "source_unavailable",
            f"{source_label} cannot be inspected.",
            status=409,
            retryable=True,
        ) from exc
    if _is_link_or_reparse(path_before) or not stat.S_ISREG(path_before.st_mode):
        raise SourceObservationError(
            "source_unavailable",
            f"{source_label} must be a regular file and cannot be a link.",
            status=409,
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SourceObservationError(
            "source_not_found",
            f"{source_label} does not exist.",
            status=404,
        ) from exc
    except OSError as exc:
        raise SourceObservationError(
            "source_unavailable",
            f"{source_label} cannot be opened safely.",
            status=409,
            retryable=True,
        ) from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SourceObservationError(
                "source_unavailable",
                f"{source_label} is not a regular file.",
                status=409,
            )
        if not _same_file(opened, path_before):
            raise SourceObservationError(
                "source_changed",
                f"{source_label} changed while it was being opened.",
                status=409,
                retryable=True,
            )
        if opened.st_size > maximum:
            raise _too_large(
                source_label,
                maximum=maximum,
                observed=opened.st_size,
                importer_id=importer_id,
            )

        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if retain_bytes else None
        byte_length = 0
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            byte_length += len(chunk)
            remaining -= len(chunk)
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        if byte_length > maximum:
            raise _too_large(
                source_label,
                maximum=maximum,
                observed=byte_length,
                importer_id=importer_id,
            )

        after = os.fstat(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise SourceObservationError(
                "source_changed",
                f"{source_label} changed while it was being read.",
                status=409,
                retryable=True,
            ) from exc
        if after.st_size > maximum:
            raise _too_large(
                source_label,
                maximum=maximum,
                observed=after.st_size,
                importer_id=importer_id,
            )
        if (
            _is_link_or_reparse(path_after)
            or not stat.S_ISREG(path_after.st_mode)
            or not _same_file(after, path_after)
            or after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None)
            != getattr(opened, "st_mtime_ns", None)
        ):
            raise SourceObservationError(
                "source_changed",
                f"{source_label} changed while it was being read.",
                status=409,
                retryable=True,
            )
        return BoundedSourceRead(
            path=path,
            sha256=digest.hexdigest(),
            byte_length=byte_length,
            data=None if chunks is None else b"".join(chunks),
            importer_id=importer_id,
            max_source_bytes=maximum,
        )
    finally:
        os.close(descriptor)


def _source_metadata(document: DocumentRecord) -> Mapping[str, Any]:
    try:
        metadata = json.loads(document.meta_json) if document.meta_json else {}
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    source = metadata.get("source")
    return source if isinstance(source, dict) else {}


def source_importer_for_document(
    document: DocumentRecord,
    *,
    registry: FileImporterRegistry = DEFAULT_FILE_IMPORTERS,
) -> FileImporter:
    """Resolve an external source's bound from its durable importer identity."""

    source = _source_metadata(document)
    detached = documents.source_is_detached(document)
    if detached and source.get("kind") not in {"file_import", "imported_markdown"}:
        raise SourceObservationError(
            "source_metadata_invalid",
            "The detached source metadata is not safe to interpret.",
            status=409,
        )
    persisted_id = source.get("importer_id") if detached else MARKDOWN_IMPORTER_ID
    if persisted_id is None:
        # Pre-registry detached Markdown imports have no importer_id. A bounded
        # Markdown fallback preserves their observability without guessing a
        # future/non-Markdown format.
        if (
            registry.resolve_binding(
                document.path,
                importer_id=MARKDOWN_IMPORTER_ID,
            )
            is None
        ):
            raise SourceObservationError(
                "source_importer_unavailable",
                "The historical source has no safe importer binding.",
                status=409,
            )
        importer_id = MARKDOWN_IMPORTER_ID
    elif isinstance(persisted_id, str):
        importer_id = persisted_id
    else:
        raise SourceObservationError(
            "source_importer_unavailable",
            "The source importer identity is invalid.",
            status=409,
        )
    importer = registry.importer_by_id(importer_id)
    if importer is None:
        raise SourceObservationError(
            "source_importer_unavailable",
            "The importer used for this source is not available.",
            status=409,
            details={"importer_id": importer_id},
        )
    if registry.resolve_binding(document.path, importer_id=importer_id) is None:
        raise SourceObservationError(
            "source_importer_mismatch",
            "The source path does not match its recorded importer.",
            status=409,
            details={"importer_id": importer_id},
        )
    recorded_media_type = source.get("media_type")
    if recorded_media_type is not None and recorded_media_type != importer.media_type:
        raise SourceObservationError(
            "source_importer_mismatch",
            "The source media type does not match its recorded importer.",
            status=409,
            details={"importer_id": importer_id},
        )
    return importer


def read_document_source(
    store: TruthStore,
    document: DocumentRecord,
    *,
    retain_bytes: bool = True,
    registry: FileImporterRegistry = DEFAULT_FILE_IMPORTERS,
) -> BoundedSourceRead:
    importer = source_importer_for_document(document, registry=registry)
    try:
        resolved = resolve_document_source_path(store, document)
    except CoworkPathError as exc:
        raise SourceObservationError(
            "source_unavailable",
            "The current source path is not safe to read.",
            status=409,
        ) from exc
    return read_bounded_regular_file(
        resolved.path,
        maximum=importer.max_source_bytes,
        source_label="The current source file",
        importer_id=importer.importer_id,
        retain_bytes=retain_bytes,
    )


def observe_document_source_sha256(
    store: TruthStore,
    document: DocumentRecord,
    *,
    registry: FileImporterRegistry = DEFAULT_FILE_IMPORTERS,
) -> str | None:
    """Best-effort bounded observation for routine metadata projections."""

    try:
        return read_document_source(
            store,
            document,
            retain_bytes=False,
            registry=registry,
        ).sha256
    except SourceObservationError:
        return None


__all__ = [
    "BoundedSourceRead",
    "SourceObservationError",
    "observe_document_source_sha256",
    "read_bounded_regular_file",
    "read_document_source",
    "source_importer_for_document",
]
