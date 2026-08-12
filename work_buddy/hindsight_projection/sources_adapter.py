"""Public Sources adapters for projection dependency/redaction accounting."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Sequence

from work_buddy.hindsight_projection.contracts import (
    DependencyUsage,
    ProjectionClaimSnapshot,
    ProjectionEffect,
)
from work_buddy.sources.models import ActorRef, SourceRef, canonical_sha256
from work_buddy.sources.redact import redact_source
from work_buddy.sources.resolve import resolve_and_reserve_source
from work_buddy.sources.store import SourceStore


class SourceStoreProjectionDependencyRegistry:
    """Register semantic derivatives without reading their source bytes.

    The injected principal must already have purpose-bound metadata access to
    each source representation.  This class never grants itself access.
    """

    def __init__(
        self,
        store: SourceStore,
        *,
        principal: ActorRef,
        purpose: str = "truth_hindsight_projection",
    ) -> None:
        self.store = store
        self.principal = principal
        self.purpose = purpose

    def reserve(
        self,
        *,
        effect: ProjectionEffect,
        attempt_no: int,
        snapshot: ProjectionClaimSnapshot,
    ) -> tuple[DependencyUsage, ...]:
        if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
            raise ValueError("attempt_no must be a positive integer")
        usages: list[DependencyUsage] = []
        seen: set[tuple[str, str, str]] = set()
        try:
            for dependency in snapshot.source_dependencies:
                key = (
                    dependency.source_ref,
                    dependency.representation_id,
                    canonical_sha256(dict(dependency.selector)),
                )
                if key in seen:
                    continue
                seen.add(key)
                consumer_id = "hindsight-" + hashlib.sha256(
                    (
                        effect.effect_id
                        + "\0"
                        + str(attempt_no)
                        + "\0"
                        + dependency.source_ref
                        + "\0"
                        + dependency.representation_id
                        + "\0"
                        + key[2]
                    ).encode("utf-8")
                ).hexdigest()
                reserved = resolve_and_reserve_source(
                    self.store,
                    source_ref=SourceRef.parse(dependency.source_ref),
                    representation_id=dependency.representation_id,
                    principal=self.principal,
                    purpose=self.purpose,
                    consumer_domain="hindsight_projection",
                    consumer_id=consumer_id,
                    use_kind="semantic_derivative",
                    disclosure_kind="metadata_only",
                    redaction_policy="invalidate",
                    selector={
                        "schema": "wb.truth-hindsight-dependency/v1",
                        "relation": dependency.relation,
                        "selector": dict(dependency.selector),
                    },
                    expected_digest=dependency.content_sha256,
                )
                usages.append(
                    DependencyUsage(
                        usage_id=reserved.reservation.usage_id,
                        source_ref=dependency.source_ref,
                        representation_id=dependency.representation_id,
                        redaction_epoch=reserved.reservation.redaction_epoch,
                    )
                )
        except Exception:
            # The Sources usage rows are durable and deterministic. Best-effort
            # release prevents a later dependency failure from stranding the
            # earlier reservations in the ordinary (non-crash) path.
            for usage in usages:
                try:
                    self.store.release_usage(usage.usage_id)
                except Exception:
                    pass
            raise
        return tuple(usages)

    def acknowledge(self, usages: Sequence[DependencyUsage]) -> None:
        for usage in usages:
            self.store.acknowledge_usage(usage.usage_id)

    def release(self, usages: Sequence[DependencyUsage]) -> None:
        for usage in usages:
            self.store.release_usage(usage.usage_id)


class CapturedProjectionSourceLifecycle:
    """Bounded cleanup authority for derived projection source items."""

    def __init__(self, store: SourceStore, *, actor: ActorRef) -> None:
        self.store = store
        self.actor = actor

    @staticmethod
    def _fingerprint(authorization_ref: str) -> str:
        return canonical_sha256(
            {
                "authorization_ref": authorization_ref,
                "purpose": "truth_hindsight_projection_source_redaction",
            }
        )

    def register(
        self,
        *,
        source_ref: str,
        representation_id: str,
        authorization_ref: str,
    ) -> None:
        parsed = SourceRef.parse(source_ref)
        binding_id = "hindsight-redact-" + hashlib.sha256(
            (
                source_ref
                + "\0"
                + representation_id
                + "\0"
                + authorization_ref
            ).encode("utf-8")
        ).hexdigest()
        try:
            self.store.grant_access(
                source_ref=parsed,
                principal=self.actor,
                purpose="redaction",
                access_mode="metadata",
                authorization_fingerprint=self._fingerprint(authorization_ref),
                scope={"consumer_domain": "hindsight_projection"},
                trusted_service_id="truth-hindsight-projection",
                content_boundary={"representation_id": representation_id},
                binding_id=binding_id,
            )
        except sqlite3.IntegrityError:
            # Deterministic binding IDs make an exact registration replay a
            # no-op.  A SHA-256 collision is outside the declared threat model.
            return

    def redact(
        self,
        source_ref: str,
        *,
        authorization_ref: str,
        reason_code: str,
    ) -> None:
        redact_source(
            self.store,
            source_ref=SourceRef.parse(source_ref),
            actor=self.actor,
            authorization_fingerprint=self._fingerprint(authorization_ref),
            reason_code=reason_code,
        )
