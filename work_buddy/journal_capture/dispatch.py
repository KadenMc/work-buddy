"""Restart-safe delivery of source-owned Journal capture commands."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.journal_capture.models import CaptureMode, CaptureTarget, JournalCaptureError
from work_buddy.journal_capture.service import CommittedIngress, JournalCaptureService
from work_buddy.sources.dispatch import SourceOutbox
from work_buddy.sources.errors import SourceError, SourceLeaseConflict
from work_buddy.sources.models import ActorRef, OutboxEffect, SourceRef
from work_buddy.sources.resolve import resolve_and_reserve_source
from work_buddy.sources.store import SourceStore
from work_buddy.truth.registry import TruthStoreRegistry


logger = logging.getLogger(__name__)
_EFFECT_TYPE = "journal.capture.materialize"
_REDACTION_EFFECT_TYPE = "source.redaction"


class _RedactionWaiting(JournalCaptureError):
    code = "journal_redaction_waiting_for_capture"

    def __init__(self) -> None:
        super().__init__(
            "The Journal capture is still settling before source redaction.",
            retryable=True,
        )


@dataclass(frozen=True)
class DispatchSummary:
    delivered: int = 0
    failed: int = 0
    deferred: int = 0


class JournalSourceDispatcher:
    """Consume only Journal capture effects from the shared Sources outbox.

    The outbox command is intentionally content-free.  Delivery resolves the
    exact retained representation under the stable Journal service principal,
    then calls the idempotent Journal domain service.  A crash at any boundary
    can therefore replay without duplicating either the capture or its entry.
    """

    def __init__(
        self,
        sources: SourceStore,
        journal: JournalCaptureService,
        *,
        service_principal: ActorRef,
        worker_id: str = "journal-source-dispatch",
        document_registry: TruthStoreRegistry | None = None,
    ) -> None:
        self.sources = sources
        self.journal = journal
        self.service_principal = service_principal
        self.worker_id = worker_id
        self.outbox = SourceOutbox(sources)
        self.document_registry = (
            document_registry if document_registry is not None else TruthStoreRegistry()
        )

    def drain(self, *, limit: int = 25) -> DispatchSummary:
        # Fence before leasing Sources work or resolving exact content.  This
        # remains necessary for a dispatcher cached before a restore begins.
        require_source_foundation_writable("journal_capture.dispatch")
        captures = self.outbox.lease(
            self.worker_id,
            limit=limit,
            lease_seconds=60,
            target_domain="journal",
            effect_type=_EFFECT_TYPE,
        )
        redactions = self.outbox.lease(
            self.worker_id,
            limit=limit,
            lease_seconds=60,
            target_domain="journal",
            effect_type=_REDACTION_EFFECT_TYPE,
        )
        delivered = failed = deferred = 0
        for effect in (*captures, *redactions):
            try:
                if effect.effect_type == _EFFECT_TYPE:
                    capture_id = self._deliver_capture(effect)
                    result_ref = f"journal-capture:{capture_id}"
                else:
                    result_ref = self._deliver_redaction(effect)
                if self._acknowledge_result(effect.effect_id, result_ref):
                    delivered += 1
                else:
                    deferred += 1
            except SourceLeaseConflict:
                # Another worker owns the effect now. Never fail or overwrite
                # its live lease merely because this worker ran for longer.
                deferred += 1
            except (
                JournalCaptureError,
                SourceError,
                ValueError,
                UnicodeDecodeError,
                KeyError,
            ) as exc:
                retryable = isinstance(exc, JournalCaptureError) and exc.retryable
                logger.warning(
                    "Journal source effect %s could not be delivered (%s)",
                    effect.effect_id,
                    getattr(exc, "code", "journal_source_command_invalid"),
                )
                try:
                    self.outbox.fail(
                        effect.effect_id, self.worker_id,
                        error_code=getattr(exc, "code", "journal_source_command_invalid"),
                        retryable=retryable,
                    )
                except SourceLeaseConflict:
                    deferred += 1
                    continue
                if retryable:
                    deferred += 1
                else:
                    failed += 1
            except Exception:
                # Operational failures remain retryable.  Never include source
                # bytes or provider text in logs or the durable error code.
                logger.exception("Journal source effect delivery failed")
                try:
                    self.outbox.fail(effect.effect_id, self.worker_id,
                                     error_code="journal_dispatch_failed", retryable=True)
                except SourceLeaseConflict:
                    pass
                deferred += 1
        return DispatchSummary(delivered=delivered, failed=failed, deferred=deferred)

    def deliver_exact(self, effect_id: str) -> str:
        """Deliver one known capture command for the synchronous HTTP path."""

        require_source_foundation_writable("journal_capture.dispatch")
        current = self.outbox.get(effect_id)
        if current is None:
            raise KeyError("journal_source_command_not_found")
        if current.status == "succeeded":
            capture = self.journal.store.get_capture_by_source_effect(effect_id)
            if capture is None:
                raise ValueError("journal_source_command_invalid")
            return capture.capture_id
        effect = self.outbox.lease_exact(
            effect_id,
            self.worker_id,
            lease_seconds=60,
        )
        if effect is None or effect.effect_type != _EFFECT_TYPE:
            raise ValueError("journal_source_command_unavailable")
        try:
            capture_id = self._deliver_capture(effect)
            result_ref = f"journal-capture:{capture_id}"
            self._acknowledge_result(effect.effect_id, result_ref)
            # Persistence is already acknowledged by the domain. A deferred
            # outbox receipt cannot turn that successful save into an error.
            return capture_id
        except Exception as exc:
            retryable = not isinstance(exc, (ValueError, UnicodeDecodeError, KeyError))
            if isinstance(exc, JournalCaptureError):
                retryable = exc.retryable
            if isinstance(exc, SourceError):
                retryable = False
            try:
                self.outbox.fail(effect.effect_id, self.worker_id,
                                 error_code=getattr(exc, "code", "journal_dispatch_failed"),
                                 retryable=retryable)
            except SourceLeaseConflict:
                pass
            raise

    def drain_postseal_held(
        self,
        *,
        cohort_id: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Drain only the Source IDs frozen into one postseal batch.

        Binding briefly holds Sources then Journal writer locks, but those
        locks are released before any dispatcher connection leases work.  The
        finalizer reacquires the same lock order for a read-only cross-store
        proof, avoiding SQLite self-deadlock while keeping both snapshots
        stable at each protocol boundary.
        """

        authority = self.journal.authority
        bound = authority.bind_postseal_source_drain(
            sources=self.sources,
            cohort_id=cohort_id,
            client_mutation_id=client_mutation_id,
            actor=actor,
        )
        effect_ids = authority.source_drain_effect_ids(
            cohort_id=cohort_id,
            client_mutation_id=client_mutation_id,
        )
        delivered = failed = deferred = 0
        for effect_id in effect_ids:
            current = self.outbox.get(effect_id)
            if current is not None and current.status == "succeeded":
                delivered += 1
                continue
            try:
                effect = self.outbox.lease_exact(
                    effect_id,
                    self.worker_id,
                    lease_seconds=60,
                )
                if effect is None:
                    deferred += 1
                    continue
                capture_id = self._deliver_capture(
                    effect,
                    postseal_drain_batch_id=client_mutation_id,
                )
                if self._acknowledge_result(
                    effect.effect_id, f"journal-capture:{capture_id}"
                ):
                    delivered += 1
                else:
                    deferred += 1
            except SourceLeaseConflict:
                deferred += 1
            except (
                JournalCaptureError,
                SourceError,
                ValueError,
                UnicodeDecodeError,
                KeyError,
            ) as exc:
                retryable = isinstance(exc, JournalCaptureError) and exc.retryable
                try:
                    self.outbox.fail(
                        effect_id,
                        self.worker_id,
                        error_code=getattr(
                            exc, "code", "journal_source_command_invalid"
                        ),
                        retryable=retryable,
                    )
                except SourceLeaseConflict:
                    deferred += 1
                    continue
                if retryable:
                    deferred += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("Controlled Journal Source drain failed")
                try:
                    self.outbox.fail(
                        effect_id,
                        self.worker_id,
                        error_code="journal_dispatch_failed",
                        retryable=True,
                    )
                except SourceLeaseConflict:
                    pass
                deferred += 1
        if failed or deferred:
            return {
                **bound,
                "status": "pending",
                "delivered": delivered,
                "failed": failed,
                "deferred": deferred,
            }
        result = authority.finalize_postseal_source_drain(
            sources=self.sources,
            cohort_id=cohort_id,
            client_mutation_id=client_mutation_id,
            actor=actor,
        )
        return {
            **result,
            "status": "drained",
            "delivered": delivered,
            "failed": failed,
            "deferred": deferred,
        }

    def _acknowledge_result(self, effect_id: str, result_ref: str) -> bool:
        """Re-lease only to acknowledge a completed result, never rerun work.

        Long inference can outlive the original lease. Sources still enforces
        authorization expiry and ownership; a live competing lease is never
        stolen, and a restore fence still blocks any acknowledgement write.
        """

        require_source_foundation_writable("journal_capture.acknowledge")
        result_sha = hashlib.sha256(result_ref.encode("utf-8")).hexdigest()

        def complete():
            self.outbox.complete(effect_id, self.worker_id,
                                 result_ref=result_ref, result_sha256=result_sha)

        try:
            complete()
            return True
        except SourceLeaseConflict:
            try:
                leased = self.outbox.lease_exact(effect_id, self.worker_id, lease_seconds=60)
                if leased is None:
                    current = self.outbox.get(effect_id)
                    if current is None or current.status != "succeeded":
                        return False
                complete()
                return True
            except SourceLeaseConflict:
                return False

    def _deliver_capture(
        self,
        effect: OutboxEffect,
        *,
        postseal_drain_batch_id: str | None = None,
    ) -> str:
        payload = _mapping(effect.payload)
        if (
            effect.target_domain != "journal"
            or effect.effect_type != _EFFECT_TYPE
            or payload.get("schema") != "wb.journal-capture/v1"
        ):
            raise ValueError("journal_source_command_invalid")
        source_ref = SourceRef.from_dict(_mapping(payload.get("source_ref")))
        representation_id = _required_text(payload, "representation_id")
        submission_id = _required_text(payload, "submission_id")
        command_id = _required_text(payload, "command_id")
        parameters = _mapping(payload.get("parameters"))
        if command_id != effect.command_id:
            raise ValueError("journal_source_command_invalid")

        target = CaptureTarget(_required_text(parameters, "target_id"))
        mode = CaptureMode(_required_text(parameters, "mode"))
        purpose = "journal.smart_processing" if mode is CaptureMode.SMART else "journal.materialize"
        reserved = resolve_and_reserve_source(
            self.sources,
            source_ref=source_ref,
            representation_id=representation_id,
            principal=self.service_principal,
            purpose=purpose,
            consumer_domain="journal",
            consumer_id=effect.effect_id,
            use_kind="journal_capture_materialization",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        exact_text = reserved.resolved.content.decode("utf-8")
        item = self.sources.get_item(source_ref)
        if item is None:
            raise KeyError("source_not_found")
        self.sources.precommit_recheck_usage(reserved.reservation.usage_id)

        capture = self.journal.accept(
            ingress=CommittedIngress(
                source_ref=source_ref.uri,
                representation_id=representation_id,
                submission_id=submission_id,
                command_id=command_id,
                effect_id=effect.effect_id,
                authorization_fingerprint=effect.authorization_fingerprint,
                authorization_expires_at=effect.authorization_expires_at,
                usage_id=reserved.reservation.usage_id,
            ),
            client_mutation_id=_required_text(parameters, "client_mutation_id"),
            day_id=_required_text(parameters, "day_id"),
            target=target,
            mode=mode,
            exact_text=exact_text,
            input_mode=_required_text(parameters, "input_mode"),
            stated_at=_optional_text(parameters, "stated_at"),
            submitted_at=item.committed_at,
            run_smart=mode is CaptureMode.SMART,
            follow_up_action=_optional_text(parameters, "follow_up_action"),
            smart_disclosure_sha256=_optional_text(parameters, "smart_disclosure_sha256"),
            postseal_drain_batch_id=postseal_drain_batch_id,
        )
        # The reservation was committed before the readable domain copy.
        # A source redaction racing after this point therefore has a durable
        # Journal target to scrub, even if this acknowledgement is retried.
        self.sources.acknowledge_usage(reserved.reservation.usage_id)
        return capture.capture_id

    def _deliver_redaction(self, effect: OutboxEffect) -> str:
        payload = _mapping(effect.payload)
        if (
            effect.target_domain != "journal"
            or effect.effect_type != _REDACTION_EFFECT_TYPE
            or payload.get("schema") != "wb.source-redaction-effect/v1"
            or payload.get("consumer_domain") != "journal"
            or payload.get("redaction_policy") != "scrub"
        ):
            raise ValueError("journal_redaction_command_invalid")
        source_ref = SourceRef.from_dict(_mapping(payload.get("source_ref")))
        source_effect_id = _required_text(payload, "consumer_id")
        usage_id = _required_text(payload, "usage_id")
        redaction_event_id = _required_text(payload, "redaction_event_id")
        epoch = payload.get("redaction_epoch")
        if not isinstance(epoch, int) or epoch < 1:
            raise ValueError("journal_redaction_command_invalid")

        imported = self.journal.store.get_import_source_dependency(
            source_usage_id=usage_id,
            source_usage_consumer_id=source_effect_id,
            source_ref=source_ref.uri,
        )
        if imported is not None:
            result_sha = hashlib.sha256(
                (
                    "journal-import-source-redaction:"
                    f"{redaction_event_id}:native-copies-removed"
                ).encode("utf-8")
            ).hexdigest()
            self.journal.store.mark_import_source_redacted(
                cohort_id=str(imported["cohort_id"]),
                file_id=str(imported["file_id"]),
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
                redaction_event_id=redaction_event_id,
                redaction_epoch=epoch,
                result_sha256=result_sha,
            )
            self.sources.release_usage(usage_id)
            self.journal.store.mark_import_source_usage_released(
                cohort_id=str(imported["cohort_id"]),
                file_id=str(imported["file_id"]),
                source_usage_id=usage_id,
            )
            return f"journal-import-source-redaction:{redaction_event_id}"

        revision_dependency = (
            self.journal.store.get_item_revision_source_dependency(
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
            )
        )
        if revision_dependency is not None:
            result_sha = hashlib.sha256(
                (
                    "journal-item-revision-source-redaction:"
                    f"{redaction_event_id}:revision-copy-removed"
                ).encode("utf-8")
            ).hexdigest()
            self.journal.store.mark_item_revision_source_redacted(
                dependency_id=str(revision_dependency["dependency_id"]),
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
                redaction_event_id=redaction_event_id,
                redaction_epoch=epoch,
                result_sha256=result_sha,
            )
            self.sources.release_usage(usage_id)
            self.journal.store.mark_item_revision_source_usage_released(
                dependency_id=str(revision_dependency["dependency_id"]),
                source_usage_id=usage_id,
            )
            return f"journal-item-revision-source-redaction:{redaction_event_id}"

        native_dependency = self.journal.store.get_native_item_source_dependency(
            source_usage_id=usage_id,
            source_usage_consumer_id=source_effect_id,
            source_ref=source_ref.uri,
        )
        if native_dependency is not None:
            result_sha = hashlib.sha256(
                (
                    "journal-native-item-source-redaction:"
                    f"{redaction_event_id}:native-copy-removed"
                ).encode("utf-8")
            ).hexdigest()
            self.journal.store.mark_native_item_source_redacted(
                dependency_id=str(native_dependency["dependency_id"]),
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
                redaction_event_id=redaction_event_id,
                redaction_epoch=epoch,
                result_sha256=result_sha,
            )
            self.sources.release_usage(usage_id)
            self.journal.store.mark_native_item_source_usage_released(
                dependency_id=str(native_dependency["dependency_id"]),
                source_usage_id=usage_id,
            )
            return f"journal-native-item-source-redaction:{redaction_event_id}"

        field_dependency = self.journal.store.get_field_value_source_dependency(
            source_usage_id=usage_id,
            source_usage_consumer_id=source_effect_id,
            source_ref=source_ref.uri,
        )
        if field_dependency is not None:
            result_sha = hashlib.sha256(
                (
                    "journal-field-value-source-redaction:"
                    f"{redaction_event_id}:typed-copy-removed"
                ).encode("utf-8")
            ).hexdigest()
            self.journal.store.mark_field_value_source_redacted(
                dependency_id=str(field_dependency["dependency_id"]),
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
                redaction_event_id=redaction_event_id,
                redaction_epoch=epoch,
                result_sha256=result_sha,
            )
            self.sources.release_usage(usage_id)
            self.journal.store.mark_field_value_source_usage_released(
                dependency_id=str(field_dependency["dependency_id"]),
                source_usage_id=usage_id,
            )
            return f"journal-field-value-source-redaction:{redaction_event_id}"

        prompt_dependency = self.journal.store.get_prompt_source_dependency(
            source_usage_id=usage_id,
            source_usage_consumer_id=source_effect_id,
            source_ref=source_ref.uri,
        )
        if prompt_dependency is not None:
            result_sha = hashlib.sha256(
                (
                    "journal-prompt-source-redaction:"
                    f"{redaction_event_id}:{prompt_dependency['dependency_kind']}"
                ).encode("utf-8")
            ).hexdigest()
            self.journal.store.mark_prompt_source_redacted(
                dependency_kind=str(prompt_dependency["dependency_kind"]),
                dependency_id=str(prompt_dependency["dependency_id"]),
                source_usage_id=usage_id,
                source_usage_consumer_id=source_effect_id,
                source_ref=source_ref.uri,
                redaction_event_id=redaction_event_id,
                redaction_epoch=epoch,
                result_sha256=result_sha,
            )
            self.sources.release_usage(usage_id)
            self.journal.store.mark_prompt_source_usage_released(
                dependency_kind=str(prompt_dependency["dependency_kind"]),
                dependency_id=str(prompt_dependency["dependency_id"]),
                source_usage_id=usage_id,
            )
            return f"journal-prompt-source-redaction:{redaction_event_id}"

        original = self.outbox.get(source_effect_id)
        if original is not None and original.status in {"pending", "retryable", "leased"}:
            # Do not declare an absent copy while another worker can still
            # commit it.  The leased redaction becomes retryable and follows
            # the original command on the next drain.
            raise _RedactionWaiting()

        capture = self.journal.store.get_capture_by_source_effect(source_effect_id)
        self.journal.store.pause_source_proposals(source_effect_id=source_effect_id, source_ref=source_ref.uri)
        result_sha = hashlib.sha256(
            f"journal-source-redaction:{redaction_event_id}:no-readable-copy".encode()
        ).hexdigest()
        mixed_projection = False
        if capture is not None and capture.entry_id is not None:
            entry = self.journal.store.get_entry(capture.entry_id)
            if entry is None:
                raise KeyError("journal_entry_not_found")
            mirror = self.journal.store.get_document_binding(entry.entry_id)
            if mirror is not None:
                mixed_projection = (
                    mirror.source_use_kind == "mixed_derivative"
                    or mirror.source_disclosure_kind == "semantic_derivative"
                    or mirror.source_redaction_policy == "review"
                )
                if not mixed_projection:
                    try:
                        document_store = self.document_registry.open_store(mirror.store_id)
                        causality = DocumentCausalityStore(document_store.paths.sidecar)
                        mixed_projection = any(
                            change.operation_kind == "direct_editor_update"
                            for change in causality.changes_for_binding(
                                mirror.binding_id, limit=1000
                            )
                        )
                    except Exception as exc:
                        logger.warning(
                            "Deferred Journal redaction while document causality is unavailable (%s)",
                            getattr(exc, "code", type(exc).__name__),
                        )
                        raise _RedactionWaiting() from exc
            if mixed_projection and mirror is not None:
                self.journal.store.mark_document_source_review_required(
                    entry.entry_id,
                    details={
                        "schema": "wb.source-maintenance-attention/v1",
                        "kind": "source_redaction_review_required",
                        "reason": "journal_exact_copy_redacted_file_is_mixed_derivative",
                        "bindingId": mirror.binding_id,
                        "documentId": mirror.document_id,
                        "sourceRef": source_ref.uri,
                        "redactionEventId": redaction_event_id,
                        "journalUsageId": usage_id,
                        "activeDocumentUsageId": mirror.source_usage_id,
                    },
                )
                result_sha = hashlib.sha256(
                    (
                        "journal-source-redaction:"
                        + redaction_event_id
                        + ":database-copy-removed:mixed-file-retained"
                    ).encode()
                ).hexdigest()
            elif self.journal.adapter.marker_is_present(entry) or entry.projection_state.value == "committed":
                projection = self.journal.adapter.redact(
                    entry,
                    redaction_event_id=redaction_event_id,
                )
                result_sha = projection.result_sha256

        self.journal.store.mark_source_redacted(
            source_effect_id=source_effect_id,
            source_usage_id=usage_id,
            source_ref=source_ref.uri,
            redaction_event_id=redaction_event_id,
            redaction_epoch=epoch,
            result_sha256=result_sha,
            projection_state=(
                "paused_diverged"
                if capture is not None
                and capture.entry_id is not None
                and mixed_projection
                else "committed"
            ),
        )
        self.sources.release_usage(usage_id)
        return f"journal-source-redaction:{redaction_event_id}"


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("journal_source_command_invalid")
    return value


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 512:
        raise ValueError("journal_source_command_invalid")
    return item


def _optional_text(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item or len(item) > 512:
        raise ValueError("journal_source_command_invalid")
    return item
