"""Shared Sources/Agent Execution boundary for Co-work hosted workers.

Co-work has two long-lived model-facing execution families in addition to
Truth analysis: job-scoped Verify workers and the document Chat agent.  Their
least-authority MCP capabilities must not turn managed document bytes into an
unaccounted side channel.  This module is the single composition seam that:

* retains the exact worker-facing JSON with Sources;
* reserves the live redaction epoch before any capability returns it;
* writes ``possibly_sent`` before releasing the value and never auto-replays
  an ambiguous handoff;
* revalidates the epoch even for an idempotent, previously-sent response; and
* binds durable worker output to the ordered input-manifest digest.

It deliberately reuses the Source Foundation primitives.  It is not a second
authorization, provenance, or disclosure framework.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Mapping, Sequence

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureEntry,
    DisclosureGateway,
    DisclosureIdempotencyConflict,
    DisclosureManifestStore,
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
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import canonical_json, sha256_bytes


@dataclass(frozen=True, slots=True)
class CoworkWorkerRun:
    """Server-derived execution and authorization binding for one worker."""

    run_id: str
    worker_session_id: str
    provider_id: str
    model_id: str
    authorization_ref: str
    purpose: str


class CoworkWorkerDisclosureBoundary:
    """Account exact Co-work worker input and bind its resulting output."""

    def __init__(
        self,
        gateway: DisclosureGateway,
        sources: SourcesDisclosureService,
    ) -> None:
        self.gateway = gateway
        self.sources = sources

    def ensure_run(self, run: CoworkWorkerRun) -> None:
        self.gateway.store.create_run(
            run_id=run.run_id,
            worker_session_id=run.worker_session_id,
        )

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

    @staticmethod
    def _sources_role(role: str) -> str:
        """Project Co-work's fine-grained label onto Sources' role vocabulary."""

        if role in {
            "human_input",
            "conversation_message",
            "imported_file",
            "document_selection",
            "audio",
            "transcript",
            "fetched_passage",
            "agent_output",
            "derived_content",
        }:
            return role
        if "document" in role or "snapshot" in role:
            return "document_selection"
        return "derived_content"

    def _validate_live(self, entry: DisclosureEntry) -> None:
        try:
            self.sources.validate_disclosure_reservation(
                reservation_id=entry.reservation_id,
                redaction_epoch=entry.redaction_epoch,
            )
        except Exception as exc:
            raise DisclosureSourceError(
                "the worker disclosure source is no longer authorized"
            ) from exc

    @staticmethod
    def _validate_existing(
        entry: DisclosureEntry,
        *,
        run: CoworkWorkerRun,
        exact_content: bytes,
        tool_call_id: str,
        recipient: str,
    ) -> None:
        if (
            entry.direction is not DisclosureDirection.INBOUND_TO_MODEL
            or entry.worker_session_id != run.worker_session_id
            or entry.tool_call_id != tool_call_id
            or entry.content_sha256 != sha256_bytes(exact_content)
            or entry.byte_length != len(exact_content)
            or entry.recipient != recipient
            or entry.provider_id != run.provider_id
            or entry.model_id != run.model_id
            or entry.authorization_ref != run.authorization_ref
            or entry.purpose != run.purpose
        ):
            raise DisclosureIdempotencyConflict(
                "worker disclosure identity was reused with different input"
            )

    def account_payload(
        self,
        run: CoworkWorkerRun,
        *,
        payload: Mapping[str, Any],
        source_role: str,
        tool_call_id: str,
        idempotency_key: str,
        recipient: str = "agent_model",
        derivation_refs: Sequence[str] = (),
    ) -> tuple[DisclosureEntry, ManifestDigest]:
        """Write ahead and account one exact capability response.

        A repeat of a proven-sent logical response is permitted only while its
        original Sources reservation and redaction epoch remain live.  A
        ``possibly_sent`` response is never returned again automatically.

        This method cannot know whether the MCP/gateway caller actually
        delivered its return value to the worker.  It therefore deliberately
        leaves the entry ``possibly_sent``.  The worker's later output call is
        the causal acknowledgement that advances the input to ``sent``.
        """

        self.ensure_run(run)
        exact_content = canonical_json(dict(payload)).encode("utf-8")
        # Sources owns the idempotency identity for both bytes and explicit
        # lineage. Validate/capture it even when the manifest entry already
        # exists so a retry cannot silently add or drop an origin edge.
        captured = self.sources.capture_for_disclosure(
            exact_content=exact_content,
            source_role=self._sources_role(source_role),
            run_id=run.run_id,
            tool_call_id=tool_call_id,
            idempotency_key=f"capture:{idempotency_key}",
            direction=DisclosureDirection.INBOUND_TO_MODEL,
            purpose=run.purpose,
            authorization_ref=run.authorization_ref,
            recipient=recipient,
            provider_id=run.provider_id,
            model_id=run.model_id,
            media_type="application/json",
            encoding="utf-8",
            derivation_refs=derivation_refs,
        )
        existing = self.gateway.store.get_by_idempotency(
            run_id=run.run_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            self._validate_existing(
                existing,
                run=run,
                exact_content=exact_content,
                tool_call_id=tool_call_id,
                recipient=recipient,
            )
            self._validate_live(existing)
            if existing.state is DisclosureState.SENT:
                return existing, self.gateway.store.input_manifest_digest(run.run_id)
            if existing.send_attempted:
                raise DisclosureReplayBlocked(
                    "the prior worker response has an ambiguous delivery outcome"
                )

        preflight = DisclosurePreflight(
            run_id=run.run_id,
            worker_session_id=run.worker_session_id,
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            direction=DisclosureDirection.INBOUND_TO_MODEL,
            source_ref=captured.source_ref,
            representation_id=captured.representation_id,
            selector=DisclosureSelector(kind="whole"),
            content_sha256=captured.content_sha256,
            byte_length=captured.byte_length,
            recipient=recipient,
            provider_id=run.provider_id,
            model_id=run.model_id,
            authorization_ref=run.authorization_ref,
            purpose=run.purpose,
        )
        entry = self.gateway.preflight(preflight)
        self._validate_live(entry)
        if entry.state is DisclosureState.SENT:
            return entry, self.gateway.store.input_manifest_digest(run.run_id)
        if entry.send_attempted:
            raise DisclosureReplayBlocked(
                "the prior worker response has an ambiguous delivery outcome"
            )
        self.gateway.mark_possibly_sent(entry.id)
        entry = self.gateway.store.get_entry(entry.id)
        return entry, self.gateway.store.input_manifest_digest(run.run_id)

    def acknowledge_inputs_from_output(
        self,
        run: CoworkWorkerRun,
    ) -> tuple[DisclosureEntry, ...]:
        """Use a worker output call as causal receipt of its ordered inputs.

        A capability return is only a local handoff attempt, so it remains
        ambiguous.  A later output/proposal call from the same leased worker is
        downstream evidence that the worker received the input.  Every source
        epoch is revalidated before any transition; ambiguous reads are never
        resent.  This acknowledgement happens before the domain output write.
        """

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
            elif current.source_acknowledgement is not SourceAcknowledgementState.ACKNOWLEDGED:
                current = self.gateway.reconcile_acknowledgement(current.id)
            acknowledged.append(current)
        return tuple(acknowledged)

    def bind_output(
        self,
        run: CoworkWorkerRun,
        *,
        output_ref: str,
        idempotency_key: str,
    ) -> OutputManifestBinding:
        """Bind output only while every admitted input remains live and sent."""

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
                from work_buddy.paths import resolve
                from work_buddy.security.local_identity import get_default_authority
                from work_buddy.sources.models import ActorRef
                from work_buddy.sources.store import SourceStore

                enrolled = get_default_authority().enrolled_actor()
                issuer = ActorRef(
                    issuer_authority_id=enrolled.issuer_authority_id,
                    subject="work-buddy-agent-execution",
                    kind="service",
                    tenant_scope_id=enrolled.tenant_scope_id,
                )
                sources = SourcesDisclosureService(
                    SourceStore.create(resolve("stores/sources")),
                    tenant_scope_id=enrolled.tenant_scope_id,
                    issuer=issuer,
                )
                _DEFAULT_BOUNDARY = CoworkWorkerDisclosureBoundary(
                    DisclosureGateway(
                        DisclosureManifestStore(resolve("db/agent-execution")),
                        sources,
                    ),
                    sources,
                )
    return _DEFAULT_BOUNDARY


__all__ = [
    "CoworkWorkerDisclosureBoundary",
    "CoworkWorkerRun",
    "configure_cowork_worker_disclosure",
    "get_cowork_worker_disclosure",
    "get_default_cowork_worker_disclosure",
]
