from __future__ import annotations

import pytest

from work_buddy.hindsight_projection.authorization import (
    grant_projection_authorization,
    require_active_projection_authorization,
    revoke_projection_authorization,
)
from work_buddy.hindsight_projection.contracts import ProjectionValidationError


def _grant(projection_store):
    return grant_projection_authorization(
        projection_store,
        store_id="store-0001",
        policy_id="default",
        recipient="hindsight-local-service",
        provider_id="anthropic",
        model_id="configured-hindsight-model",
        eligible_claim_kinds=("fact", "preference"),
        projection_method="semantic_summary",
        granted_by_ref="consent-session:test",
        basis="high_consent_capability",
        granted_at="2026-08-09T12:00:00.000Z",
        expires_at="2026-08-10T12:00:00.000Z",
        authorization_ref="hpa-test-0001",
    )


def test_projection_authorization_is_exact_expiring_and_revocable(projection_store) -> None:
    granted = _grant(projection_store)
    assert require_active_projection_authorization(
        projection_store,
        authorization_ref=granted.authorization_ref,
        store_id="store-0001",
        policy_id="default",
        recipient="hindsight-local-service",
        provider_id="anthropic",
        model_id="configured-hindsight-model",
        eligible_claim_kinds=("preference", "fact"),
        projection_method="semantic_summary",
        at="2026-08-09T13:00:00.000Z",
    ) == granted
    with pytest.raises(ProjectionValidationError, match="does not match"):
        require_active_projection_authorization(
            projection_store,
            authorization_ref=granted.authorization_ref,
            store_id="store-0001",
            policy_id="default",
            recipient="wrong-recipient",
            provider_id="anthropic",
            model_id="configured-hindsight-model",
            eligible_claim_kinds=("fact", "preference"),
            projection_method="semantic_summary",
            at="2026-08-09T13:00:00.000Z",
        )
    revoke_projection_authorization(
        projection_store,
        granted.authorization_ref,
        revoked_at="2026-08-09T14:00:00.000Z",
    )
    with pytest.raises(ProjectionValidationError, match="expired or revoked"):
        require_active_projection_authorization(
            projection_store,
            authorization_ref=granted.authorization_ref,
            store_id="store-0001",
            policy_id="default",
            recipient="hindsight-local-service",
            provider_id="anthropic",
            model_id="configured-hindsight-model",
            eligible_claim_kinds=("fact", "preference"),
            projection_method="semantic_summary",
            at="2026-08-09T15:00:00.000Z",
        )


def test_fabricated_and_expired_projection_authorizations_fail_closed(
    projection_store,
) -> None:
    with pytest.raises(ProjectionValidationError, match="not recorded"):
        require_active_projection_authorization(
            projection_store,
            authorization_ref="fabricated",
            store_id="store-0001",
            policy_id="default",
            recipient="hindsight-local-service",
            provider_id="anthropic",
            model_id="configured-hindsight-model",
            eligible_claim_kinds=None,
            projection_method="semantic_summary",
            at="2026-08-09T13:00:00.000Z",
        )
    granted = _grant(projection_store)
    with pytest.raises(ProjectionValidationError, match="expired or revoked"):
        require_active_projection_authorization(
            projection_store,
            authorization_ref=granted.authorization_ref,
            store_id="store-0001",
            policy_id="default",
            recipient="hindsight-local-service",
            provider_id="anthropic",
            model_id="configured-hindsight-model",
            eligible_claim_kinds=("fact", "preference"),
            projection_method="semantic_summary",
            at="2026-08-11T12:00:00.000Z",
        )
