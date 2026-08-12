from __future__ import annotations

from work_buddy.hindsight_projection.sources_adapter import (
    SourceStoreProjectionDependencyRegistry,
)
from work_buddy.security.actors import ActorRef
from work_buddy.sources.models import SourceRef, canonical_sha256
from work_buddy.sources.store import SourceStore

from .conftest import make_snapshot, make_spec


def test_source_dependency_is_digest_checked_idempotent_and_releasable(
    tmp_path,
    projection_store,
) -> None:
    store = SourceStore.create(tmp_path / "sources", authority_id="authority1")
    tenant = "tenant001"
    principal = ActorRef(
        issuer_authority_id="authority1",
        subject="truth-projection-service",
        kind="service",
        tenant_scope_id=tenant,
    )
    item = store.capture_source(
        content=b"source passage",
        source_role="conversation_message",
        tenant_scope_id=tenant,
        originating_surface="test",
    )
    store.grant_access(
        source_ref=item.source_ref,
        principal=principal,
        purpose="truth_hindsight_projection",
        access_mode="metadata",
        authorization_fingerprint=canonical_sha256({"test": "grant"}),
        content_boundary={"representation_id": item.primary_representation_id},
    )
    registry = SourceStoreProjectionDependencyRegistry(
        store,
        principal=principal,
    )
    spec = make_spec()
    snapshot = make_snapshot(spec)
    # Bind the fixture to the actual retained occurrence.
    dependency = snapshot.source_dependencies[0]
    snapshot = snapshot.__class__(
        **{
            field: getattr(snapshot, field)
            for field in snapshot.__dataclass_fields__
            if field != "source_dependencies"
        },
        source_dependencies=(
            dependency.__class__(
                source_ref=item.source_ref.uri,
                representation_id=item.primary_representation_id,
                content_sha256=dependency.content_sha256,
                relation=dependency.relation,
                selector=dependency.selector,
            ),
        ),
    )
    effect = projection_store.enqueue(spec)

    first = registry.reserve(effect=effect, attempt_no=1, snapshot=snapshot)
    replay = registry.reserve(effect=effect, attempt_no=1, snapshot=snapshot)
    assert replay == first
    registry.acknowledge(first)
    registry.release(first)
    retried = registry.reserve(effect=effect, attempt_no=2, snapshot=snapshot)
    assert retried[0].usage_id != first[0].usage_id
    registry.release(retried)

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT status FROM source_usage_intents WHERE usage_id = ?",
            (first[0].usage_id,),
        ).fetchone()
        assert row["status"] == "released"
    finally:
        conn.close()
