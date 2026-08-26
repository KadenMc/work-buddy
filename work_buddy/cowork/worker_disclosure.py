"""Co-work source-origin extensions to shared scoped-worker disclosure.

The shared execution boundary owns accounting and manifests. Co-work adds
only native action-snapshot origins and its injectable host composition seam.
"""

from __future__ import annotations

import threading
from typing import Any

from work_buddy.agent_execution.disclosure import DisclosureSourceError
from work_buddy.agent_execution.worker_disclosure import (
    WorkerDisclosureBoundary,
    WorkerRun as CoworkWorkerRun,
    create_worker_disclosure_boundary,
)


class CoworkWorkerDisclosureBoundary(WorkerDisclosureBoundary):
    """Add exact native Co-work snapshot origins to worker disclosure."""

    def capture_action_snapshot_origins(
        self,
        *,
        store_id: str,
        action_snapshot_id: str,
        purpose: str,
        namespace: str,
        truth_registry: Any | None = None,
    ) -> tuple[str, ...]:
        """Capture both exact native parts embedded in a worker response."""

        from work_buddy.cowork.verify import ActionSnapshot
        from work_buddy.cowork.verify import store as verify_store
        from work_buddy.sources.cowork import (
            CoworkActionSnapshotProvider,
            cowork_action_snapshot_origin,
        )
        from work_buddy.sources.providers import (
            ProviderRegistry,
            source_capture_from_origin,
        )
        from work_buddy.truth.registry import TruthStoreRegistry

        selected_registry = truth_registry or TruthStoreRegistry()
        truth_store = selected_registry.open_store(store_id)
        action = verify_store.get_record(
            truth_store,
            ActionSnapshot,
            action_snapshot_id,
        )
        if action is None:
            raise DisclosureSourceError(
                "the worker action-snapshot source is unavailable"
            )
        registry = ProviderRegistry()
        provider = CoworkActionSnapshotProvider(
            tenant_scope_id=self.sources.tenant_scope_id,
            issuer=self.sources.issuer,
            registry=selected_registry,
        )
        registry.register(provider)
        refs: list[str] = []
        for part, expected_digest in (
            ("target", action.target_text_sha256),
            ("projection", action.projection_sha256),
        ):
            ref = source_capture_from_origin(
                self.sources.store,
                registry,
                provider_id=provider.provider_id,
                origin_ref=cowork_action_snapshot_origin(
                    store_id=store_id,
                    action_snapshot_id=action.id,
                    revision=action.canonical_sha256,
                    part=part,
                ),
                principal=self.sources.issuer,
                purpose=purpose,
                tenant_scope_id=self.sources.tenant_scope_id,
                originating_surface="cowork_worker_context",
                expected_revision=action.canonical_sha256,
                expected_digest=expected_digest,
                namespace=namespace,
            )
            refs.append(ref.uri)
        return tuple(refs)


_BOUNDARY: CoworkWorkerDisclosureBoundary | None = None
_DEFAULT_BOUNDARY: CoworkWorkerDisclosureBoundary | None = None
_DEFAULT_BOUNDARY_LOCK = threading.Lock()


def configure_cowork_worker_disclosure(
    boundary: CoworkWorkerDisclosureBoundary | None,
) -> None:
    """Install a test/application boundary; ``None`` restores lazy defaulting."""

    global _BOUNDARY
    _BOUNDARY = boundary


def get_cowork_worker_disclosure() -> CoworkWorkerDisclosureBoundary:
    if _BOUNDARY is not None:
        return _BOUNDARY
    return get_default_cowork_worker_disclosure()


def get_default_cowork_worker_disclosure() -> CoworkWorkerDisclosureBoundary:
    global _DEFAULT_BOUNDARY
    if _DEFAULT_BOUNDARY is None:
        with _DEFAULT_BOUNDARY_LOCK:
            if _DEFAULT_BOUNDARY is None:
                _DEFAULT_BOUNDARY = create_worker_disclosure_boundary(
                    CoworkWorkerDisclosureBoundary
                )
    return _DEFAULT_BOUNDARY


__all__ = [
    "CoworkWorkerDisclosureBoundary",
    "CoworkWorkerRun",
    "configure_cowork_worker_disclosure",
    "get_cowork_worker_disclosure",
    "get_default_cowork_worker_disclosure",
]
