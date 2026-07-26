"""Document-level Co-work surface policy checks."""

from __future__ import annotations

from work_buddy.truth.store import DocumentRecord, TruthStore


def document_surface_allowed(
    store: TruthStore,
    document: DocumentRecord,
) -> bool:
    surface = store.profile.document_surface
    allowed = surface.allowed_document_classes
    return bool(surface.enabled) and (
        not allowed or document.document_class in allowed
    )


def document_surface_denial_reason(
    store: TruthStore,
    document: DocumentRecord,
) -> str | None:
    surface = store.profile.document_surface
    if not surface.enabled:
        return "document_surface_disabled"
    if (
        surface.allowed_document_classes
        and document.document_class not in surface.allowed_document_classes
    ):
        return "document_class_not_allowed"
    return None


__all__ = ["document_surface_allowed", "document_surface_denial_reason"]
