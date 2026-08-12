"""Source-aware Agent Execution boundary for Co-work Truth analysis.

This module contains composition glue, not disclosure persistence.  Agent
Execution owns the ordered run manifest; Sources owns exact dynamic-content
capture and redaction-aware reservations.  Truth analysis calls this boundary
immediately around worker-facing responses and external research calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Mapping, Protocol, TypeVar

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureEntry,
    DisclosureGateway,
    DisclosureIdempotencyConflict,
    DisclosurePreflight,
    DisclosureReplayBlocked,
    DisclosureSelector,
    DisclosureSourceError,
    DisclosureState,
    DisclosureStateConflict,
    ManifestDigest,
    OutputManifestBinding,
    SourceAcknowledgementState,
)
from work_buddy.cowork.truth_analysis_runtime import TruthAnalysisRuntimeRun
from work_buddy.sources.models import canonical_json, sha256_bytes


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class CapturedDisclosureSource:
    """Exact retained boundary returned by Sources dynamic capture."""

    source_ref: str
    representation_id: str
    content_sha256: str
    byte_length: int


class TruthAnalysisDisclosureSources(Protocol):
    """Capture extension paired with Agent Execution's reservation protocol."""

    def capture_for_disclosure(
        self,
        *,
        exact_content: bytes,
        source_role: str,
        run_id: str,
        tool_call_id: str,
        idempotency_key: str,
        direction: DisclosureDirection,
        purpose: str,
        authorization_ref: str,
        recipient: str,
        provider_id: str,
        model_id: str,
        derivation_ref: str | None,
        input_manifest_sha256: str | None,
    ) -> CapturedDisclosureSource:
        """Retain exact bytes and authorize this bounded run disclosure."""

    def validate_disclosure_reservation(
        self,
        *,
        reservation_id: str,
        redaction_epoch: int,
    ) -> object:
        """Prove the captured source remains live at its reserved epoch."""


def _selection(run: TruthAnalysisRuntimeRun) -> tuple[str, str]:
    provider_id = str(run.selection.get("provider_id") or "").strip()
    model_id = str(run.selection.get("model_id") or "").strip()
    if not provider_id or not model_id:
        raise ValueError("Truth analysis run has no bound provider/model")
    return provider_id, model_id


class TruthAnalysisDisclosureBoundary:
    """Run-aware disclosure operations used by the four worker capabilities."""

    def __init__(
        self,
        gateway: DisclosureGateway,
        sources: TruthAnalysisDisclosureSources,
    ) -> None:
        self.gateway = gateway
        self.sources = sources

    def ensure_run(self, run: TruthAnalysisRuntimeRun) -> None:
        self.gateway.store.create_run(
            run_id=run.run_id,
            worker_session_id=run.session_id,
        )

    def _validate_live(self, entry: DisclosureEntry) -> None:
        try:
            self.sources.validate_disclosure_reservation(
                reservation_id=entry.reservation_id,
                redaction_epoch=entry.redaction_epoch,
            )
        except Exception as exc:
            raise DisclosureSourceError(
                "the Truth analysis disclosure source is no longer authorized"
            ) from exc

    @staticmethod
    def _validate_existing_inbound(
        entry: DisclosureEntry,
        *,
        run: TruthAnalysisRuntimeRun,
        exact_content: bytes,
        tool_call_id: str,
        recipient: str,
        provider_id: str,
    ) -> None:
        _execution_provider_id, model_id = _selection(run)
        if (
            entry.direction is not DisclosureDirection.INBOUND_TO_MODEL
            or entry.worker_session_id != run.session_id
            or entry.tool_call_id != tool_call_id
            or entry.content_sha256 != sha256_bytes(exact_content)
            or entry.byte_length != len(exact_content)
            or entry.recipient != recipient
            or entry.provider_id != provider_id
            or entry.model_id != model_id
            or entry.authorization_ref != run.authorization_receipt_id
            or entry.purpose != "truth_analysis"
        ):
            raise DisclosureIdempotencyConflict(
                "worker disclosure identity was reused with different input"
            )

    def _capture_preflight(
        self,
        run: TruthAnalysisRuntimeRun,
        *,
        exact_content: bytes,
        source_role: str,
        tool_call_id: str,
        idempotency_key: str,
        direction: DisclosureDirection,
        recipient: str,
        provider_id: str,
        derivation_ref: str | None = None,
        input_manifest_sha256: str | None = None,
    ) -> DisclosurePreflight:
        self.ensure_run(run)
        _execution_provider_id, model_id = _selection(run)
        captured = self.sources.capture_for_disclosure(
            exact_content=exact_content,
            source_role=source_role,
            run_id=run.run_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            direction=direction,
            purpose="truth_analysis",
            authorization_ref=run.authorization_receipt_id,
            recipient=recipient,
            provider_id=provider_id,
            model_id=model_id,
            derivation_ref=derivation_ref,
            input_manifest_sha256=input_manifest_sha256,
        )
        if (
            captured.content_sha256 != sha256_bytes(exact_content)
            or captured.byte_length != len(exact_content)
        ):
            raise ValueError("Sources captured a different disclosure boundary")
        return DisclosurePreflight(
            run_id=run.run_id,
            worker_session_id=run.session_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            direction=direction,
            source_ref=captured.source_ref,
            representation_id=captured.representation_id,
            selector=DisclosureSelector(kind="whole"),
            content_sha256=captured.content_sha256,
            byte_length=captured.byte_length,
            recipient=recipient,
            provider_id=provider_id,
            model_id=model_id,
            authorization_ref=run.authorization_receipt_id,
            purpose="truth_analysis",
            derivation_ref=derivation_ref,
            input_manifest_sha256=input_manifest_sha256,
        )

    def account_inbound(
        self,
        run: TruthAnalysisRuntimeRun,
        *,
        payload: object,
        source_role: str,
        tool_call_id: str,
        idempotency_key: str,
        derivation_ref: str | None = None,
    ) -> DisclosureEntry:
        """Account a worker-facing value as it leaves the capability kernel."""

        exact_content = canonical_json(payload).encode("utf-8")
        execution_provider_id, _model_id = _selection(run)
        existing = self.gateway.store.get_by_idempotency(
            run_id=run.run_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            self._validate_existing_inbound(
                existing,
                run=run,
                exact_content=exact_content,
                tool_call_id=tool_call_id,
                recipient="agent_model",
                provider_id=execution_provider_id,
            )
            self._validate_live(existing)
            if existing.state is DisclosureState.SENT:
                return existing
            if existing.send_attempted:
                raise DisclosureReplayBlocked(
                    "the prior worker response has an ambiguous delivery outcome"
                )
        preflight = self._capture_preflight(
            run,
            exact_content=exact_content,
            source_role=source_role,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            direction=DisclosureDirection.INBOUND_TO_MODEL,
            recipient="agent_model",
            provider_id=execution_provider_id,
            derivation_ref=derivation_ref,
        )
        entry = self.gateway.preflight(preflight)
        self._validate_live(entry)
        if entry.state is DisclosureState.SENT:
            return entry
        if entry.send_attempted:
            raise DisclosureReplayBlocked(
                "the prior worker response has an ambiguous delivery outcome"
            )
        self.gateway.mark_possibly_sent(entry.id)
        # The local capability return is only a handoff attempt. A later
        # worker output call supplies causal evidence that the model received
        # this exact response; until then replay remains blocked as ambiguous.
        return self.gateway.store.get_entry(entry.id)

    def execute_outbound(
        self,
        run: TruthAnalysisRuntimeRun,
        *,
        exact_content: bytes,
        source_role: str,
        tool_call_id: str,
        idempotency_key: str,
        recipient: str,
        provider_id: str,
        call: Callable[[], _T],
        external_egress: Callable[[_T], bool],
        derivation_ref: str | None = None,
    ) -> _T:
        """Bracket one replay-safe provider operation with write-ahead state."""

        existing = self.gateway.store.get_by_idempotency(
            run_id=run.run_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _execution_provider_id, model_id = _selection(run)
            if (
                existing.direction is not DisclosureDirection.OUTBOUND_TO_PROVIDER
                or existing.worker_session_id != run.session_id
                or existing.tool_call_id != tool_call_id
                or existing.content_sha256 != sha256_bytes(exact_content)
                or existing.byte_length != len(exact_content)
                or existing.recipient != recipient
                or existing.provider_id != provider_id
                or existing.model_id != model_id
                or existing.authorization_ref != run.authorization_receipt_id
            ):
                raise DisclosureIdempotencyConflict(
                    "outbound disclosure identity was reused with different input"
                )
            if existing.state is DisclosureState.POSSIBLY_SENT:
                raise DisclosureReplayBlocked(
                    "the prior provider call has an ambiguous delivery outcome"
                )
            # The independently durable research broker is responsible for
            # returning its stored receipt without invoking the provider again.
            return call()

        input_digest = self.gateway.store.input_manifest_digest(run.run_id)
        preflight = self._capture_preflight(
            run,
            exact_content=exact_content,
            source_role=source_role,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            direction=DisclosureDirection.OUTBOUND_TO_PROVIDER,
            recipient=recipient,
            provider_id=provider_id,
            derivation_ref=derivation_ref,
            input_manifest_sha256=input_digest.manifest_sha256,
        )
        entry = self.gateway.preflight(preflight)
        self.gateway.mark_possibly_sent(entry.id)
        result = call()
        if external_egress(result):
            self.gateway.mark_sent(entry.id)
        else:
            self.gateway.reconcile(
                entry.id,
                proven_outcome=DisclosureState.NOT_SENT,
            )
        return result

    def manifest_digest(self, run: TruthAnalysisRuntimeRun) -> ManifestDigest:
        self.ensure_run(run)
        return self.gateway.store.manifest_digest(run.run_id)

    def acknowledge_inputs_from_output(
        self,
        run: TruthAnalysisRuntimeRun,
    ) -> tuple[DisclosureEntry, ...]:
        """Treat a bound worker output as causal receipt of all run inputs."""

        self.ensure_run(run)
        entries = tuple(
            entry
            for entry in self.gateway.store.list_entries(run.run_id)
            if entry.direction is DisclosureDirection.INBOUND_TO_MODEL
        )
        if not entries:
            raise DisclosureSourceError(
                "worker output cannot acknowledge an empty input manifest"
            )
        # Validate the complete set before any state transition so one stale
        # source cannot produce a partially acknowledged manifest.
        for entry in entries:
            if entry.state is DisclosureState.NOT_SENT or not entry.send_attempted:
                raise DisclosureStateConflict(
                    "worker output cannot acknowledge an input with no handoff attempt"
                )
            self._validate_live(entry)

        acknowledged: list[DisclosureEntry] = []
        for entry in entries:
            self._validate_live(entry)
            current = entry
            if current.state is DisclosureState.POSSIBLY_SENT:
                current = self.gateway.mark_sent(current.id)
            elif (
                current.source_acknowledgement
                is not SourceAcknowledgementState.ACKNOWLEDGED
            ):
                current = self.gateway.reconcile_acknowledgement(current.id)
            acknowledged.append(current)
        return tuple(acknowledged)

    def bind_output(
        self,
        run: TruthAnalysisRuntimeRun,
        *,
        output_ref: str,
        idempotency_key: str,
    ) -> OutputManifestBinding:
        self.ensure_run(run)
        entries = self.acknowledge_inputs_from_output(run)
        for entry in entries:
            if (
                entry.state is not DisclosureState.SENT
                or entry.source_acknowledgement
                is not SourceAcknowledgementState.ACKNOWLEDGED
            ):
                raise DisclosureReplayBlocked(
                    "worker output cannot bind an unacknowledged input disclosure"
                )
            self._validate_live(entry)
        return self.gateway.store.bind_output_manifest(
            run_id=run.run_id,
            output_ref=output_ref,
            idempotency_key=idempotency_key,
        )


_BOUNDARY: TruthAnalysisDisclosureBoundary | None = None
_DEFAULT_BOUNDARY: TruthAnalysisDisclosureBoundary | None = None
_DEFAULT_BOUNDARY_LOCK = threading.Lock()


def configure_truth_analysis_disclosure(
    boundary: TruthAnalysisDisclosureBoundary | None,
) -> None:
    """Install the application-composed boundary; ``None`` disables it in tests."""

    global _BOUNDARY
    _BOUNDARY = boundary


def configured_truth_analysis_disclosure() -> TruthAnalysisDisclosureBoundary | None:
    return _BOUNDARY


def get_default_truth_analysis_disclosure() -> TruthAnalysisDisclosureBoundary:
    """Build the production boundary from persistent machine-local services."""

    global _DEFAULT_BOUNDARY
    if _DEFAULT_BOUNDARY is None:
        with _DEFAULT_BOUNDARY_LOCK:
            if _DEFAULT_BOUNDARY is None:
                from work_buddy.agent_execution.disclosure import (
                    DisclosureManifestStore,
                )
                from work_buddy.paths import resolve
                from work_buddy.security.local_identity import get_default_authority
                from work_buddy.sources.disclosure import SourcesDisclosureService
                from work_buddy.sources.models import ActorRef
                from work_buddy.sources.store import SourceStore

                enrolled = get_default_authority().enrolled_actor()
                issuer = ActorRef(
                    issuer_authority_id=enrolled.issuer_authority_id,
                    subject="work-buddy-agent-execution",
                    kind="service",
                    tenant_scope_id=enrolled.tenant_scope_id,
                )
                sources_store = SourceStore.create(resolve("stores/sources"))
                sources = SourcesDisclosureService(
                    sources_store,
                    tenant_scope_id=enrolled.tenant_scope_id,
                    issuer=issuer,
                )
                _DEFAULT_BOUNDARY = TruthAnalysisDisclosureBoundary(
                    DisclosureGateway(
                        DisclosureManifestStore(resolve("db/agent-execution")),
                        sources,
                    ),
                    sources,
                )
    return _DEFAULT_BOUNDARY


def account_worker_context(
    boundary: TruthAnalysisDisclosureBoundary,
    run: TruthAnalysisRuntimeRun,
    context: Mapping[str, Any],
    *,
    target_derivation_ref: str,
) -> None:
    """Record target and existing Truth as distinct ordered model inputs."""

    boundary.account_inbound(
        run,
        payload=context.get("target"),
        source_role="document_selection",
        tool_call_id="truth-analysis-job-get:target",
        idempotency_key=f"truth-analysis-context-target:{run.context_sha256}",
        derivation_ref=target_derivation_ref,
    )
    boundary.account_inbound(
        run,
        payload=context.get("existing_truth"),
        source_role="derived_content",
        tool_call_id="truth-analysis-job-get:existing-truth",
        idempotency_key=f"truth-analysis-context-existing:{run.context_sha256}",
    )
