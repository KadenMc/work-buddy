"""Application composition and bounded worker tick for Truth projection."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict
from typing import Any, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
)
from work_buddy.hindsight_projection.disclosure import (
    AgentExecutionProjectionDisclosure,
)
from work_buddy.hindsight_projection.hindsight import HindsightProjectionDestination
from work_buddy.hindsight_projection.redaction_dispatch import (
    HindsightProjectionRedactionDispatcher,
)
from work_buddy.hindsight_projection.service import TruthHindsightProjectionService
from work_buddy.hindsight_projection.sources_adapter import (
    CapturedProjectionSourceLifecycle,
    SourceStoreProjectionDependencyRegistry,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
from work_buddy.hindsight_projection.truth_reader import (
    TruthStoreProjectionReader,
    projection_policy_from_config,
)
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.truth.store import TruthStore


def _projection_block(config: Mapping[str, Any]) -> Mapping[str, Any]:
    hindsight = config.get("hindsight", {})
    if not isinstance(hindsight, Mapping):
        return {}
    value = hindsight.get("truth_projection", {})
    if not isinstance(value, Mapping):
        raise ValueError("hindsight.truth_projection must be an object")
    return value


def _transport_identity(
    config: Mapping[str, Any],
    *,
    enabled: bool,
) -> tuple[str, str, str]:
    raw = _projection_block(config)
    values = tuple(str(raw.get(key) or "").strip() for key in (
        "recipient",
        "provider_id",
        "model_id",
    ))
    if enabled and not all(values):
        raise ValueError(
            "enabled Truth-to-Hindsight projection requires recipient, "
            "provider_id, and model_id"
        )
    # Disabled projection may still reconcile a content-free removal. These
    # placeholders never authorize or label an exact-content handoff.
    return tuple(value or "disabled" for value in values)  # type: ignore[return-value]


def build_projection_service(
    store: TruthStore,
    *,
    config: Mapping[str, Any] | None = None,
) -> TruthHindsightProjectionService:
    """Compose persistent Sources, Agent Execution, Truth, and Hindsight seams."""

    require_source_foundation_writable("hindsight_projection.compose_dispatch")

    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
    )
    from work_buddy.hindsight_projection.authorization import (
        require_active_projection_authorization,
    )
    from work_buddy.config import load_config
    from work_buddy.paths import resolve
    from work_buddy.security.local_identity import get_default_authority
    from work_buddy.sources.disclosure import SourcesDisclosureService
    from work_buddy.sources.models import ActorRef
    from work_buddy.sources.store import SourceStore

    active_config = load_config() if config is None else config
    policy = projection_policy_from_config(active_config, store_id=store.store_id)
    recipient, provider_id, model_id = _transport_identity(
        active_config,
        enabled=policy.enabled,
    )
    source_store = SourceStore.create(resolve("stores/sources"))
    enrolled = get_default_authority().enrolled_actor()

    def actor(subject: str) -> ActorRef:
        return ActorRef(
            issuer_authority_id=enrolled.issuer_authority_id,
            subject=subject,
            kind="service",
            tenant_scope_id=enrolled.tenant_scope_id,
        )

    issuer = actor("work-buddy-agent-execution")
    truth_principal = actor("work-buddy-truth-service")
    projection_actor = actor("work-buddy-hindsight-projection")
    sources = SourcesDisclosureService(
        source_store,
        tenant_scope_id=enrolled.tenant_scope_id,
        issuer=issuer,
    )
    gateway = DisclosureGateway(
        DisclosureManifestStore(resolve("db/agent-execution")),
        sources,
    )
    lifecycle = CapturedProjectionSourceLifecycle(
        source_store,
        actor=projection_actor,
    )
    projection_store = TruthHindsightProjectionStore(store.paths.db)

    def validate_authorization(effect) -> None:
        require_active_projection_authorization(
            projection_store,
            authorization_ref=effect.spec.authorization_ref,
            store_id=store.store_id,
            policy_id=effect.spec.policy_id,
            recipient=recipient,
            provider_id=provider_id,
            model_id=model_id,
            eligible_claim_kinds=(
                None
                if policy.eligible_claim_kinds is None
                else tuple(policy.eligible_claim_kinds)
            ),
            projection_method=policy.projection_method,
        )

    return TruthHindsightProjectionService(
        store=projection_store,
        truth=TruthStoreProjectionReader(store, policy=policy),
        destination=HindsightProjectionDestination(),
        disclosure=AgentExecutionProjectionDisclosure(
            gateway=gateway,
            sources=sources,
            source_lifecycle=lifecycle,
            recipient=recipient,
            provider_id=provider_id,
            model_id=model_id,
            authorization_validator=validate_authorization,
            producer=projection_actor,
        ),
        dependencies=SourceStoreProjectionDependencyRegistry(
            source_store,
            principal=truth_principal,
        ),
    )


def _build_redaction_dispatcher(
    truth_store: TruthStore,
    service: TruthHindsightProjectionService,
    *,
    policy,
    worker_id: str,
) -> HindsightProjectionRedactionDispatcher:
    from work_buddy.paths import resolve
    from work_buddy.security.local_identity import get_default_authority
    from work_buddy.sources.models import ActorRef
    from work_buddy.sources.store import SourceStore

    sources = SourceStore.create(resolve("stores/sources"))
    enrolled = get_default_authority().enrolled_actor()
    actor = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-hindsight-projection",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    return HindsightProjectionRedactionDispatcher(
        sources=sources,
        truth_store=truth_store,
        projection_store=TruthHindsightProjectionStore(truth_store.paths.db),
        projection_service=service,
        policy=policy,
        actor=actor,
        worker_id=worker_id,
    )


def run_projection_tick(
    *,
    store_id: str | None = None,
    limit_per_store: int = 20,
    reconcile: bool = True,
    registry: TruthStoreRegistry | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile and drain a bounded number of effects for reachable stores."""

    require_source_foundation_writable("hindsight_projection.dispatch")

    if isinstance(limit_per_store, bool) or not isinstance(limit_per_store, int):
        raise ValueError("limit_per_store must be an integer")
    if limit_per_store < 1 or limit_per_store > 500:
        raise ValueError("limit_per_store must be between 1 and 500")
    if not isinstance(reconcile, bool):
        raise ValueError("reconcile must be boolean")

    if config is None:
        from work_buddy.config import load_config

        active_config: Mapping[str, Any] = load_config()
    else:
        active_config = config
    inventory = registry or TruthStoreRegistry()
    if store_id is not None:
        stores = (inventory.open_store(store_id),)
    else:
        stores = tuple(
            inventory.open_store(row.store_id)
            for row in inventory.list_stores(refresh=True)
            if row.reachable
        )
    worker_id = f"truth-hindsight-{os.getpid()}"
    reports: list[dict[str, Any]] = []
    for truth_store in stores:
        try:
            policy = projection_policy_from_config(
                active_config,
                store_id=truth_store.store_id,
            )
            projection_store = TruthHindsightProjectionStore(truth_store.paths.db)
            if not policy.enabled and not projection_store.has_tracked_projection_state():
                reports.append(
                    {
                        "store_id": truth_store.store_id,
                        "ok": True,
                        "state": "dormant",
                        "reason": "projection_policy_disabled",
                    }
                )
                continue
            service = build_projection_service(truth_store, config=active_config)
            redactions = _build_redaction_dispatcher(
                truth_store,
                service,
                policy=policy,
                worker_id=worker_id + "-redaction",
            )
            # Sources leases are intentionally capped at 100 per call even
            # when the projection outbox tick is configured for a larger
            # bounded batch.
            prepared_redactions = redactions.prepare(
                limit=min(limit_per_store, 100)
            )
            reconciliation = (
                asdict(service.reconcile_truth()) if reconcile else None
            )
            states: Counter[str] = Counter()
            errors: Counter[str] = Counter()
            for _ in range(limit_per_store):
                result = service.process_next(worker_id=worker_id)
                states[result.state] += 1
                if result.error_code:
                    errors[result.error_code] += 1
                if result.state == "idle":
                    break
                if result.state in {"failed_retryable", "reconciling"}:
                    # Both states are already durable. Reacquiring the same
                    # head immediately would only burn this tick's whole
                    # budget on a transient dependency/provider outage.
                    break
            redaction_report = asdict(redactions.settle(prepared_redactions))
            reports.append(
                {
                    "store_id": truth_store.store_id,
                    "ok": not bool(errors),
                    "reconciliation": reconciliation,
                    "states": dict(sorted(states.items())),
                    "errors": dict(sorted(errors.items())),
                    "source_redactions": redaction_report,
                }
            )
        except Exception as exc:
            reports.append(
                {
                    "store_id": truth_store.store_id,
                    "ok": False,
                    "error_code": getattr(exc, "error_code", type(exc).__name__),
                }
            )
    return {
        "stores": reports,
        "store_count": len(reports),
        "ok": all(report["ok"] for report in reports),
    }


__all__ = ["build_projection_service", "run_projection_tick"]
