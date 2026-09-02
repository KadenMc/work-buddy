"""Exact Source retention primitives for one-off legacy import cohorts.

Domain importers own their cohort state and publication transaction.  This
service owns the cross-database Sources side of the protocol: trusted ingress,
idempotent usage reservation, pre-commit validation, acknowledgement, and
exact-byte verification.  It deliberately returns only identifiers and
digests; callers must not put imported prose in receipts or logs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib

from work_buddy.sources.ingress import (
    HumanInputCommit,
    HumanInputRequest,
    TrustedIngressContext,
    TrustedIngressService,
)
from work_buddy.sources.models import SourceRef, UsageReservation
from work_buddy.sources.store import SourceStore


class ExactImportSourceError(RuntimeError):
    """An import Source dependency is missing, changed, or unavailable."""


@dataclass(frozen=True, slots=True)
class ExactImportSourceBinding:
    source_ref: str
    representation_id: str
    submission_id: str | None
    usage_id: str
    usage_status: str


class ExactImportSourceService:
    """Retain exact import bytes under a deterministic consumer contract."""

    def __init__(
        self,
        store: SourceStore,
        *,
        purpose: str,
        consumer_domain: str,
        use_kind: str,
    ) -> None:
        if not purpose or not consumer_domain or not use_kind:
            raise ValueError("an import Source contract is required")
        self.store = store
        self.ingress = TrustedIngressService(store)
        self.purpose = purpose
        self.consumer_domain = consumer_domain
        self.use_kind = use_kind

    def retain(
        self,
        *,
        exact_content: bytes,
        client_mutation_id: str,
        consumer_id: str,
        context: TrustedIngressContext,
        media_type: str = "text/markdown",
        source_committed: Callable[[HumanInputCommit], None] | None = None,
    ) -> ExactImportSourceBinding:
        self._validate_context(context)
        committed = self.ingress.commit_human_input(
            context,
            HumanInputRequest(
                exact_content=exact_content,
                client_mutation_id=client_mutation_id,
                input_mode="import",
                media_type=media_type,
            ),
        )
        if source_committed is not None:
            source_committed(committed)
        reservation = self._reserve(
            source_ref=committed.source_ref,
            representation_id=committed.representation_id,
            consumer_id=consumer_id,
            context=context,
        )
        self._precheck(reservation)
        return ExactImportSourceBinding(
            source_ref=committed.source_ref.uri,
            representation_id=committed.representation_id,
            submission_id=committed.submission_id,
            usage_id=reservation.usage_id,
            usage_status=reservation.status,
        )

    def reconcile(
        self,
        *,
        source_ref: str,
        representation_id: str,
        consumer_id: str,
        context: TrustedIngressContext,
    ) -> ExactImportSourceBinding:
        self._validate_context(context)
        reservation = self._reserve(
            source_ref=SourceRef.parse(source_ref),
            representation_id=representation_id,
            consumer_id=consumer_id,
            context=context,
        )
        self._precheck(reservation)
        return ExactImportSourceBinding(
            source_ref=source_ref,
            representation_id=representation_id,
            submission_id=None,
            usage_id=reservation.usage_id,
            usage_status=reservation.status,
        )

    def acknowledge(self, usage_id: str) -> None:
        reservation = self.store.acknowledge_usage(usage_id)
        if reservation.status != "acknowledged":
            raise ExactImportSourceError("the import Source dependency was not acknowledged")

    def release(self, usage_id: str) -> None:
        reservation = self.store.release_usage(usage_id)
        if reservation.status != "released":
            raise ExactImportSourceError("the import Source dependency was not released")

    def verify_exact(
        self,
        *,
        source_ref: str,
        representation_id: str,
        expected_sha256: str,
        expected_byte_length: int,
    ) -> None:
        try:
            parsed = SourceRef.parse(source_ref)
            connection = self.store.connect()
            try:
                row = self.store._representation_row(
                    connection, parsed, representation_id
                )
                content = self.store._read_representation_row(row)
            finally:
                connection.close()
        except Exception as exc:
            raise ExactImportSourceError(
                "the retained import Source is unavailable"
            ) from exc
        if (
            len(content) != expected_byte_length
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise ExactImportSourceError(
                "the retained import Source differs from the frozen inventory"
            )

    def _validate_context(self, context: TrustedIngressContext) -> None:
        if self.purpose not in context.permitted_purposes:
            raise ExactImportSourceError(
                "trusted ingress does not permit the import Source purpose"
            )

    def _reserve(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        consumer_id: str,
        context: TrustedIngressContext,
    ) -> UsageReservation:
        return self.store.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=context.service_principal,
            purpose=self.purpose,
            consumer_domain=self.consumer_domain,
            consumer_id=consumer_id,
            use_kind=self.use_kind,
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )

    def _precheck(self, reservation: UsageReservation) -> None:
        if reservation.status == "reserved":
            self.store.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise ExactImportSourceError("the import Source dependency is unavailable")


__all__ = [
    "ExactImportSourceBinding",
    "ExactImportSourceError",
    "ExactImportSourceService",
]
