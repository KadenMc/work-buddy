"""Bounded vault-file Sources provider used for imports and divergence capture."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from work_buddy.cowork.source_observation import (
    SourceObservationError,
    read_bounded_regular_file,
)
from work_buddy.sources import (
    ActorRef,
    AttributionAssertion,
    NativeCapture,
    NativeObservation,
    OriginRef,
)
from work_buddy.sources.models import canonical_sha256, utc_now


FILE_IMPORT_PROVIDER_ID = "work-buddy-file-import"
FILE_IMPORT_PROVIDER_VERSION = "1"
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024


class WorkBuddyFileImportProvider:
    """Capture exact UTF-8/opaque file bytes without following links.

    `native_item_id` is always a normalized POSIX path relative to one
    registered root. Absolute paths, traversal, intermediate links/reparse
    points, and non-regular final entries fail closed.
    """

    provider_id = FILE_IMPORT_PROVIDER_ID
    version = FILE_IMPORT_PROVIDER_VERSION
    stable_occurrence_identity = True

    def __init__(
        self,
        root: str | Path,
        *,
        tenant_scope_id: str,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ValueError("file import root must be a real directory")
        if not tenant_scope_id:
            raise ValueError("tenant_scope_id is required")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.tenant_scope_id = tenant_scope_id
        self.max_bytes = max_bytes
        self.container_id = hashlib.sha256(
            os.path.normcase(str(self.root)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _relative(value: str) -> PurePosixPath:
        relative = PurePosixPath(value)
        if (
            not value
            or relative.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise SourceObservationError(
                "source_unavailable",
                "The file origin is outside its registered root.",
                status=409,
            )
        return relative

    def _path(self, origin_ref: OriginRef) -> Path:
        relative = self._relative(origin_ref.native_item_id)
        if origin_ref.container_id not in {None, self.container_id}:
            raise SourceObservationError(
                "source_unavailable",
                "The file origin belongs to another registered root.",
                status=409,
            )
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                info = os.stat(current, follow_symlinks=False)
            except OSError as exc:
                raise SourceObservationError(
                    "source_unavailable",
                    "The file origin cannot be inspected.",
                    status=409,
                    retryable=True,
                ) from exc
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse
            ):
                raise SourceObservationError(
                    "source_unavailable",
                    "The file origin cannot contain links.",
                    status=409,
                )
        try:
            current.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - guarded component-wise
            raise SourceObservationError(
                "source_unavailable",
                "The file origin is outside its registered root.",
                status=409,
            ) from exc
        return current

    def canonicalize_origin(self, origin_ref: OriginRef) -> OriginRef:
        if origin_ref.provider_id != self.provider_id:
            raise SourceObservationError(
                "source_unavailable", "The file provider does not match.", status=409
            )
        if origin_ref.part is not None or origin_ref.coordinates:
            raise SourceObservationError(
                "source_selector_unsupported",
                "This file provider captures the complete file only.",
                status=409,
            )
        relative = self._relative(origin_ref.native_item_id)
        self._path(origin_ref)
        return OriginRef(
            provider_id=self.provider_id,
            container_id=self.container_id,
            native_item_id=relative.as_posix(),
            revision=origin_ref.revision,
            part=None,
            coordinates={},
        )

    def authorize(
        self,
        origin_ref: OriginRef,
        principal: ActorRef,
        purpose: str,
    ) -> bool:
        return (
            principal.tenant_scope_id == self.tenant_scope_id
            and origin_ref.container_id == self.container_id
            and purpose in {
                "file_import",
                "document_projection_divergence",
                "document_source_recheck",
            }
        )

    def capture(self, origin_ref: OriginRef, purpose: str) -> NativeCapture:
        canonical = self.canonicalize_origin(origin_ref)
        observed = read_bounded_regular_file(
            self._path(canonical),
            maximum=self.max_bytes,
            source_label="The registered file",
            importer_id=self.provider_id,
            retain_bytes=True,
        )
        assert observed.data is not None
        is_markdown = canonical.native_item_id.lower().endswith(".md")
        if is_markdown:
            try:
                observed.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SourceObservationError(
                    "source_encoding_invalid",
                    "The Markdown file is not valid UTF-8.",
                    status=415,
                ) from exc
        if canonical.revision is not None and canonical.revision != observed.sha256:
            raise SourceObservationError(
                "source_changed",
                "The registered file changed before capture.",
                status=409,
                retryable=True,
            )
        authorization = canonical_sha256(
            {
                "provider": self.provider_id,
                "container": self.container_id,
                "path": canonical.native_item_id,
                "purpose": purpose,
                "digest": observed.sha256,
            }
        )
        return NativeCapture(
            exact_content=observed.data,
            media_type=(
                "text/markdown" if is_markdown else "application/octet-stream"
            ),
            representation_kind="raw_bytes",
            encoding=("utf-8" if is_markdown else None),
            source_role="imported_file",
            fidelity="exact_bytes",
            native_revision=observed.sha256,
            occurred_at=None,
            observed_at=utc_now(),
            authorization_fingerprint=authorization,
            attributions=(
                AttributionAssertion(
                    role="author",
                    actor=None,
                    state="unknown",
                    basis="file_origin",
                    assurance="unknown",
                ),
            ),
        )

    def observe(self, origin_ref: OriginRef) -> NativeObservation:
        canonical = self.canonicalize_origin(origin_ref)
        try:
            observed = read_bounded_regular_file(
                self._path(canonical),
                maximum=self.max_bytes,
                source_label="The registered file",
                importer_id=self.provider_id,
                retain_bytes=False,
            )
        except SourceObservationError as exc:
            return NativeObservation(
                kind="origin_recheck",
                status="unavailable",
                observed_at=utc_now(),
                error_code=exc.code,
            )
        return NativeObservation(
            kind="origin_recheck",
            status=(
                "unchanged"
                if canonical.revision in {None, observed.sha256}
                else "changed"
            ),
            observed_at=utc_now(),
            native_revision=observed.sha256,
            native_content_sha256=observed.sha256,
        )


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "FILE_IMPORT_PROVIDER_ID",
    "FILE_IMPORT_PROVIDER_VERSION",
    "WorkBuddyFileImportProvider",
]
