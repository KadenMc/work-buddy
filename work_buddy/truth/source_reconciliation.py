"""Production composition for bounded Truth-to-Sources receipt recovery."""

from __future__ import annotations

from typing import Any

from work_buddy.paths import resolve
from work_buddy.security.actors import ActorRef
from work_buddy.security.local_identity import get_default_authority
from work_buddy.sources.store import SourceStore
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.source_claims import reconcile_pending_source_usages


def reconcile_truth_source_usages(
    *,
    store_id: str | None = None,
    limit_per_store: int = 100,
    registry: TruthStoreRegistry | None = None,
) -> dict[str, Any]:
    """Recover committed Truth source reservations after restart/crash."""

    if (
        isinstance(limit_per_store, bool)
        or not isinstance(limit_per_store, int)
        or not 1 <= limit_per_store <= 1000
    ):
        raise ValueError("limit_per_store must be between 1 and 1000")
    inventory = registry or TruthStoreRegistry()
    source_store = SourceStore.create(resolve("stores/sources"))
    enrolled = get_default_authority().enrolled_actor()
    actor = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-truth-source-reconciler",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    requested_store_ids = (
        (store_id,)
        if store_id is not None
        else tuple(
            sorted(
                {
                    row.store_id
                    for row in inventory.list_stores(refresh=True)
                    if row.reachable
                }
            )
        )
    )
    reports: list[dict[str, Any]] = []
    for requested_store_id in requested_store_ids:
        try:
            truth_store = inventory.open_store(requested_store_id)
        except Exception as exc:
            reports.append(
                {
                    "store_id": requested_store_id,
                    "ok": False,
                    "error_code": type(exc).__name__,
                }
            )
            continue
        try:
            counts = reconcile_pending_source_usages(
                truth_store,
                source_store,
                actor=actor,
                limit=limit_per_store,
            )
            reports.append({"store_id": truth_store.store_id, "ok": True, **counts})
        except Exception as exc:
            reports.append(
                {
                    "store_id": requested_store_id,
                    "ok": False,
                    "error_code": type(exc).__name__,
                }
            )
    return {
        "ok": all(bool(report["ok"]) for report in reports),
        "stores": reports,
    }


__all__ = ["reconcile_truth_source_usages"]
