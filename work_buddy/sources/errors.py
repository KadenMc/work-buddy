"""Content-free failures exposed by the Sources kernel.

Sources handles material that may be private or legally sensitive.  Exceptions
therefore carry a stable code and a deliberately generic public message; raw
content, native coordinates, filesystem paths, and request payloads never
belong in an exception string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(eq=False)
class SourceError(Exception):
    """Base class for typed Sources failures safe to log or return over HTTP."""

    code: ClassVar[str] = "source_error"
    http_status: ClassVar[int] = 400
    retryable: ClassVar[bool] = False
    public_message: ClassVar[str] = "The source operation could not be completed."

    def __str__(self) -> str:
        return self.public_message

    def to_public_dict(self) -> dict[str, object]:
        return {
            "error": self.code,
            "message": self.public_message,
            "retryable": self.retryable,
        }


class InvalidSourceRequest(SourceError):
    code = "invalid_source_request"
    public_message = "The source request is invalid."


class InvalidSourceReference(InvalidSourceRequest):
    code = "invalid_source_reference"
    public_message = "The source reference is invalid."


class SourceInvariantViolation(SourceError):
    code = "source_invariant_violation"
    http_status = 409
    public_message = "The source operation would violate a storage invariant."


class SourceNotFound(SourceError):
    code = "source_not_found"
    http_status = 404
    public_message = "The requested source is unavailable."


class SourceAuthorityMismatch(SourceError):
    code = "source_authority_mismatch"
    http_status = 409
    public_message = "The source authority does not match this operation."


class SourceIdempotencyConflict(SourceError):
    code = "source_idempotency_conflict"
    http_status = 409
    public_message = "That mutation identifier was already used for different input."


class SourceAccessDenied(SourceError):
    code = "source_access_denied"
    http_status = 403
    public_message = "Access to this source is not authorized for that purpose."


class SourceRedacted(SourceError):
    code = "source_redacted"
    http_status = 410
    public_message = "The requested source is no longer readable."


class SourceIntegrityFailure(SourceError):
    code = "source_integrity_failure"
    http_status = 409
    public_message = "The retained source failed its integrity check."


class SourceContentTooLarge(SourceError):
    code = "source_content_too_large"
    http_status = 413
    public_message = "The source exceeds the permitted content boundary."


class SourceProviderNotFound(SourceError):
    code = "source_provider_not_found"
    http_status = 404
    public_message = "The requested source provider is unavailable."


class SourceProviderConflict(SourceError):
    code = "source_provider_conflict"
    http_status = 409
    public_message = "The source provider registration conflicts with an existing one."


class SourceOriginMismatch(SourceError):
    code = "source_origin_mismatch"
    http_status = 409
    public_message = "The native source identity or revision did not match."


class SourceUsageConflict(SourceError):
    code = "source_usage_conflict"
    http_status = 409
    public_message = "The source usage reservation conflicts with existing state."


class SourceLeaseConflict(SourceError):
    code = "source_lease_conflict"
    http_status = 409
    public_message = "The source effect lease is no longer held by this worker."


class SourceExportDenied(SourceError):
    code = "source_export_denied"
    http_status = 403
    public_message = "This source export is not authorized."


class SourceImportInvalid(SourceError):
    code = "source_import_invalid"
    public_message = "The source import could not be validated."


class SourceImportCollision(SourceError):
    code = "source_import_collision"
    http_status = 409
    public_message = "An imported source identity conflicts with retained content."


class SourceSchemaTooNew(SourceError):
    code = "source_schema_too_new"
    http_status = 409
    public_message = "This source store requires a newer Work Buddy version."


def public_error(exc: Exception) -> tuple[dict[str, object], int]:
    """Return a content-free HTTP body/status pair for a failure."""

    if isinstance(exc, SourceError):
        return exc.to_public_dict(), exc.http_status
    generic = SourceError()
    return generic.to_public_dict(), 500
