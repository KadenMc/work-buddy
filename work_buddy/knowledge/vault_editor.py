"""Compatibility entry point for personal-knowledge mutations.

The module name remains because the shipped ``knowledge_mint`` operation
imports it. Its implementation is database-only and has no vault fallback.
"""

from __future__ import annotations

from typing import Any

from work_buddy.knowledge.personal.service import (
    PersonalKnowledgeService,
    build_structured_body as _build_structured_body,
    slugify as _slugify,
)


def mint_personal_unit(
    *,
    name: str,
    category: str,
    content_body: str = "",
    severity: str = "",
    tags: str = "",
    evidence: str = "",
    definition: str = "",
    triggers: str = "",
    signals: str = "",
    default_response: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Mutate whichever authority is active at the migration seam."""

    from work_buddy.knowledge.personal.store import PersonalKnowledgeStore

    if PersonalKnowledgeStore.existing_authority() != "sqlite":
        from work_buddy.knowledge.personal.legacy import mint_legacy_personal_unit

        result = mint_legacy_personal_unit(
            name=name,
            category=category,
            content_body=content_body,
            severity=severity,
            tags=tags,
            evidence=evidence,
            definition=definition,
            triggers=triggers,
            signals=signals,
            default_response=default_response,
        )
        from work_buddy.knowledge.store import invalidate_vault

        invalidate_vault()
        return result

    result = PersonalKnowledgeService().mint(
        name=name,
        category=category,
        content_body=content_body,
        severity=severity,
        tags=tags,
        evidence=evidence,
        definition=definition,
        triggers=triggers,
        signals=signals,
        default_response=default_response,
        idempotency_key=idempotency_key,
    )
    from work_buddy.knowledge.store import invalidate_vault

    invalidate_vault()
    return result


def update_personal_unit(
    *,
    path: str,
    updates: dict[str, Any],
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """CAS/idempotent update surface for personal knowledge callers."""

    from work_buddy.knowledge.personal.store import (
        PersonalKnowledgeConflict,
        PersonalKnowledgeStore,
    )

    if PersonalKnowledgeStore.existing_authority() != "sqlite":
        raise PersonalKnowledgeConflict(
            "revisioned personal updates require the sealed SQLite authority"
        )

    return PersonalKnowledgeService().update(
        path,
        updates,
        expected_revision=expected_revision,
        actor="agent",
        idempotency_key=idempotency_key,
    )
