from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from work_buddy.hindsight_projection.contracts import (
    DesiredProjectionState,
    ProjectionClaimSnapshot,
    ProjectionIntentSpec,
    SourceDependency,
)
from work_buddy.hindsight_projection.schema import (
    install_truth_hindsight_projection_schema,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore


NOW = "2026-08-09T12:00:00.000Z"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_spec(
    *,
    generation: str | None = None,
    desired: DesiredProjectionState = DesiredProjectionState.UPSERT,
    reason: str = "claim_confirmed",
    purge: bool = False,
) -> ProjectionIntentSpec:
    return ProjectionIntentSpec(
        claim_id="claim-0001",
        claim_generation=generation or digest("generation-1"),
        policy_id="default",
        desired_state=desired,
        reason_code=reason,
        eligibility_sha256=digest((generation or "generation-1") + desired.value),
        authorization_ref="authorization:truth-projection",
        purge_projection_source=purge,
        requested_at=NOW,
    )


def make_snapshot(spec: ProjectionIntentSpec) -> ProjectionClaimSnapshot:
    return ProjectionClaimSnapshot(
        claim_id=spec.claim_id,
        policy_id=spec.policy_id,
        claim_generation=spec.claim_generation,
        claim_canonical_sha256=digest("canonical-claim"),
        proposition="I prefer concise, evidence-first review summaries.",
        claim_kind="preference",
        lifecycle_status="confirmed",
        lifecycle_event_id="event-0001",
        applicability_scope={"kind": "global"},
        valid_from="2026-08-01T00:00:00.000Z",
        valid_to=None,
        current=True,
        policy_eligible=True,
        source_state="clean",
        eligibility_sha256=spec.eligibility_sha256,
        evaluated_at=NOW,
        source_dependencies=(
            SourceDependency(
                source_ref="wb-source://authority1/item/item0001",
                representation_id="representation-0001",
                content_sha256=digest("source passage"),
                relation="paraphrase",
                selector={"kind": "whole"},
            ),
        ),
    )


@pytest.fixture
def projection_store(tmp_path: Path) -> TruthHindsightProjectionStore:
    db_path = tmp_path / "truth.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        install_truth_hindsight_projection_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return TruthHindsightProjectionStore(db_path)
