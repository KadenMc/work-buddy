"""Source-bound optional model processing for Journal captures.

The exact capture is already durable before this processor runs.  This module
adds the narrower, optional inference boundary: it grants one run access to the
retained ``SourceRef``, writes the disclosure manifest before handing bytes to
the model, validates a small structured result, and binds that result back to
the Journal capture.  The manifest never stores the captured text.

Production composition is deliberately feature-gated.  Enabling Smart mode is
an explicit configuration decision because it may disclose private Journal
text to the configured frontier provider.  Tests inject the runner and never
perform network calls.

Two execution paths satisfy the same Journal contract, and Settings decides
which one a Smart capture uses.  The subscription-agent path launches a
least-authority detached agent on the account the user already signs in to and
follows their default chat model.  The API-model path calls one API endpoint in
process for the shortest wait and bills that API account per capture.  Both
report their own availability and disclose their own concrete provider/model
before any capture text moves.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from work_buddy.agent_execution.disclosure import (
    DisclosureDirection,
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosureSelector,
    create_source_bound_run,
)
from work_buddy.journal_capture.models import CaptureTarget, JournalCapture, JournalSmartAvailability
from work_buddy.journal_capture.service import SmartCaptureResult, TaskProposalFollowUp
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.llm.response import LLMResponse
from work_buddy.llm.tiers import ModelTier, resolve_tier
from work_buddy.paths import resolve
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import ActorRef
from work_buddy.sources.store import SourceStore


if TYPE_CHECKING:
    from work_buddy.journal_capture.smart_worker import (
        JournalAccountBackedSmartProcessor,
        JournalSmartWorkerSpec,
    )


logger = logging.getLogger(__name__)

_PURPOSE = "journal.smart_processing"
_TOOL_CALL_ID = "journal-smart-classify"
_MAX_SMART_INPUT_BYTES = 32 * 1024
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "enum": ["log", "running_notes"]},
        "summary": {"type": "string"},
        "effects": {"type": "array", "items": {"type": "string"}},
        "follow_up": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "properties": {
                    "kind": {"type": "string", "enum": ["task_proposal"]},
                    "task_text": {"type": "string", "maxLength": 500},
                    "rationale": {"type": "string", "maxLength": 1000},
                }, "required": ["kind", "task_text", "rationale"], "additionalProperties": False},
            ],
        },
    },
    "required": ["target", "summary", "effects", "follow_up"],
    "additionalProperties": False,
}
_SYSTEM = """You classify one already-saved Journal capture.

Choose exactly one destination:
- log: a chronological observation, event, or completed activity
- running_notes: an open consideration, intention, follow-up, or item worth retaining

Return the required structured fields. Keep summary under 240 characters and
effects to at most three short, factual phrases. Never rewrite the original
capture, invent facts, or claim that the classification changed the saved text.
If this is a concrete actionable intention, choose running_notes and optionally
attach ONE task_proposal follow_up with a concise task_text and rationale.
Otherwise follow_up is null. This only proposes a task for human review; it
never creates a task. Do not output URLs, IDs, tools, or other action kinds.
"""


class JournalSmartProcessingError(RuntimeError):
    """Stable, content-free failure surfaced through Journal effect state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SmartModelRunner(Protocol):
    def call(self, **kwargs: Any) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class JournalSmartProcessorSpec:
    tier: ModelTier
    provider_id: str
    model_id: str

    @classmethod
    def from_tier(cls, tier: ModelTier) -> "JournalSmartProcessorSpec":
        binding = resolve_tier(tier)
        # The first production slice intentionally requires a concrete model
        # before disclosure. A profile whose actual model is discovered only
        # after dispatch cannot truthfully populate the write-ahead manifest.
        if binding.backend != "anthropic" or not binding.model:
            raise JournalSmartProcessingError("smart_provider_not_preflightable")
        return cls(tier=tier, provider_id=binding.backend, model_id=binding.model)


class JournalSourceBoundSmartProcessor:
    """One structured Journal classification behind Agent Disclosure."""

    def __init__(
        self,
        *,
        sources_store: SourceStore,
        journal_store: JournalCaptureStore,
        disclosure_sources: SourcesDisclosureService,
        disclosure_gateway: DisclosureGateway,
        spec: JournalSmartProcessorSpec,
        runner: SmartModelRunner | None = None,
    ) -> None:
        self.sources_store = sources_store
        self.journal_store = journal_store
        self.disclosure_sources = disclosure_sources
        self.disclosure_gateway = disclosure_gateway
        self.spec = spec
        if runner is None:
            from work_buddy.llm.runner_v2 import LLMRunner

            runner = LLMRunner()
        self.runner = runner

    @property
    def disclosure_summary(self) -> str:
        return (
            "Up to 32 KiB of the exact saved capture is sent to "
            f"{self.spec.provider_id} · {self.spec.model_id} for one "
            "classification. This processor has no tools or web access."
        )

    def process(
        self,
        *,
        capture: JournalCapture,
        exact_text: str,
    ) -> SmartCaptureResult:
        exact = exact_text.encode("utf-8")
        if len(exact) > _MAX_SMART_INPUT_BYTES:
            # Capture persistence/materialization is independent and remains
            # intact. Do not silently truncate a source before inference.
            raise JournalSmartProcessingError("smart_source_too_large")
        digest = hashlib.sha256(exact).hexdigest()
        effect_type = (
            "auto_route"
            if capture.requested_target is CaptureTarget.AUTO
            else "smart_annotate"
        )
        effect = next(
            (
                candidate
                for candidate in self.journal_store.effects_for_capture(
                    capture.capture_id
                )
                if candidate.effect_type == effect_type
            ),
            None,
        )
        if effect is None:
            raise JournalSmartProcessingError("smart_effect_missing")

        attempt_key = f"{capture.capture_id}-{capture.revision}"
        run_id = f"journal-smart-{attempt_key}"
        worker_session_id = f"journal-smart-worker-{attempt_key}"
        authorization_ref = effect.authorization_fingerprint
        run = create_source_bound_run(
            self.disclosure_gateway,
            run_id=run_id,
            worker_session_id=worker_session_id,
            recipient="agent_model",
            provider_id=self.spec.provider_id,
            model_id=self.spec.model_id,
            authorization_ref=authorization_ref,
            purpose=_PURPOSE,
        )
        self.disclosure_sources.grant_existing_source_for_disclosure(
            source_ref=capture.source_ref,
            representation_id=capture.representation_id,
            run_id=run_id,
            direction=DisclosureDirection.INBOUND_TO_MODEL,
            purpose=_PURPOSE,
            authorization_ref=authorization_ref,
            recipient="agent_model",
            provider_id=self.spec.provider_id,
            model_id=self.spec.model_id,
            tool_call_id=_TOOL_CALL_ID,
        )

        response, _entry = run.execute_resolved_inbound(
            tool_call_id=_TOOL_CALL_ID,
            idempotency_key=f"journal-smart-input-{attempt_key}",
            source_ref=capture.source_ref,
            representation_id=capture.representation_id,
            selector=DisclosureSelector(kind="whole"),
            content_sha256=digest,
            byte_length=len(exact),
            resolve_content=lambda: exact,
            handoff=self._call_model,
        )
        result = validate_smart_result(response.structured_output)
        binding = run.bind_output(
            output_ref=f"journal-capture:{capture.capture_id}",
            idempotency_key=f"journal-smart-output-{attempt_key}",
        )
        return SmartCaptureResult(
            target=result.target,
            summary=result.summary,
            effects=result.effects,
            producer_ref=f"agent-execution:{run_id}",
            model_id=response.model or self.spec.model_id,
            disclosure_manifest_sha256=binding.manifest_sha256,
            follow_up=result.follow_up,
        )

    def _call_model(self, exact: bytes) -> LLMResponse:
        try:
            user = exact.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JournalSmartProcessingError("smart_source_not_utf8") from exc
        return self.runner.call(
            tier=self.spec.tier,
            system=_SYSTEM,
            user=user,
            output_schema=_OUTPUT_SCHEMA,
            max_tokens=768,
            temperature=0.0,
            cache_ttl_minutes=0,
            trace_id=None,
            detail="Journal smart processing",
        )

    @staticmethod
    def _validated_result(
        response: LLMResponse,
    ) -> dict[str, Any]:
        if response.is_error():
            raise JournalSmartProcessingError("smart_model_failed")
        result = validate_smart_result(response.structured_output)
        return {
            "target": result.target,
            "summary": result.summary,
            "effects": result.effects,
            "follow_up": result.follow_up,
        }


def validate_smart_result(value: Any) -> SmartCaptureResult:
    """Validate the only structured result a Smart worker may commit."""

    if not isinstance(value, Mapping):
        raise JournalSmartProcessingError("smart_model_invalid_output")
    if set(value) - {"target", "summary", "effects", "follow_up"}:
        raise JournalSmartProcessingError("smart_model_invalid_output")
    try:
        target = CaptureTarget(str(value.get("target") or ""))
    except ValueError as exc:
        raise JournalSmartProcessingError("smart_model_invalid_output") from exc
    if target is CaptureTarget.AUTO:
        raise JournalSmartProcessingError("smart_model_invalid_output")
    summary = value.get("summary")
    effects = value.get("effects")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > 240
        or not isinstance(effects, list)
        or len(effects) > 3
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 160
            for item in effects
        )
    ):
        raise JournalSmartProcessingError("smart_model_invalid_output")
    follow_up = value.get("follow_up")
    typed_follow_up = None
    if follow_up is not None:
        if (
            not isinstance(follow_up, Mapping)
            or set(follow_up) != {"kind", "task_text", "rationale"}
            or follow_up.get("kind") != "task_proposal"
            or not isinstance(follow_up.get("task_text"), str)
            or not follow_up["task_text"].strip()
            or len(follow_up["task_text"]) > 500
            or not isinstance(follow_up.get("rationale"), str)
            or not follow_up["rationale"].strip()
            or len(follow_up["rationale"]) > 1000
        ):
            raise JournalSmartProcessingError("smart_model_invalid_output")
        typed_follow_up = TaskProposalFollowUp(
            task_text=follow_up["task_text"].strip(),
            rationale=follow_up["rationale"].strip(),
        )
    return SmartCaptureResult(
        target=target,
        summary=summary.strip(),
        effects=tuple(item.strip() for item in effects),
        follow_up=typed_follow_up,
    )


def configured_journal_smart_processor(
    sources_store: SourceStore,
    journal_store: JournalCaptureStore,
    *,
    execution_path: str | None = None,
) -> "JournalSourceBoundSmartProcessor | JournalAccountBackedSmartProcessor | None":
    """Compose the production processor only after an explicit config opt-in."""

    return configured_journal_smart_processing(
        sources_store,
        journal_store,
        execution_path=execution_path,
    )[0]


def _api_model_processing(
    sources_store: SourceStore,
    journal_store: JournalCaptureStore,
    configuration: Mapping[str, Any],
) -> tuple[JournalSourceBoundSmartProcessor, JournalSmartProcessorSpec]:
    """One in-process API call whose concrete model is known before disclosure."""

    tier = ModelTier(str(configuration.get("tier") or ModelTier.FRONTIER_FAST.value))
    spec = JournalSmartProcessorSpec.from_tier(tier)
    from work_buddy.security.local_identity import get_default_authority

    enrolled = get_default_authority().enrolled_actor()
    issuer = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-agent-execution",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    disclosure_sources = SourcesDisclosureService(
        sources_store,
        tenant_scope_id=enrolled.tenant_scope_id,
        issuer=issuer,
    )
    gateway = DisclosureGateway(
        DisclosureManifestStore(resolve("db/agent-execution")),
        disclosure_sources,
    )
    processor = JournalSourceBoundSmartProcessor(
        sources_store=sources_store,
        journal_store=journal_store,
        disclosure_sources=disclosure_sources,
        disclosure_gateway=gateway,
        spec=spec,
    )
    return processor, spec


def _subscription_agent_processing(
    journal_store: JournalCaptureStore,
) -> tuple["JournalAccountBackedSmartProcessor", "JournalSmartWorkerSpec"]:
    """One detached least-authority worker on the account already signed in."""

    from work_buddy.journal_capture.smart_worker import (
        JournalAccountBackedSmartProcessor,
        JournalSmartProcessingRunner,
        JournalSmartWorkerSpec,
    )

    runner = JournalSmartProcessingRunner()
    spec = JournalSmartWorkerSpec(runner.prepare())
    processor = JournalAccountBackedSmartProcessor(
        journal_store=journal_store,
        spec=spec,
        runner=runner,
    )
    return processor, spec


def configured_journal_smart_processing(
    sources_store: SourceStore,
    journal_store: JournalCaptureStore,
    *,
    enabled: bool | None = None,
    execution_path: str | None = None,
) -> tuple[
    "JournalSourceBoundSmartProcessor | JournalAccountBackedSmartProcessor | None",
    JournalSmartAvailability,
]:
    """Preserve policy-disabled vs broken-provider status without any model call."""

    from work_buddy.config import load_config
    from work_buddy.settings.registry import (
        JOURNAL_SMART_EXECUTION_API_MODEL,
    )

    value = load_config().get("journal", {}).get("smart_processing", {}) or {}
    if not isinstance(value, Mapping):
        value = {}
    if enabled is None:
        enabled = value.get("enabled") is True
    if not enabled:
        return None, JournalSmartAvailability()
    if execution_path is None:
        from work_buddy.settings.broker import get_journal_smart_execution_path

        execution_path = get_journal_smart_execution_path()
    spec = None
    try:
        if execution_path == JOURNAL_SMART_EXECUTION_API_MODEL:
            processor, spec = _api_model_processing(
                sources_store, journal_store, value
            )
            provider, model = spec.provider_id, spec.model_id
        else:
            processor, spec = _subscription_agent_processing(journal_store)
            provider, model = spec.provider_label, spec.model_label
        return processor, JournalSmartAvailability(
            state="ready", code="ready", reason="Smart is ready. Your exact capture is saved before processing.",
            provider=provider, model=model,
        )
    except Exception as exc:
        logger.warning(
            "Journal Smart processing is disabled because its provider is invalid (%s)",
            getattr(exc, "code", type(exc).__name__),
        )
        return None, JournalSmartAvailability(
            state="provider_unavailable", code="provider_not_preflightable",
            reason="Smart is enabled, but its provider could not be prepared. Check the configured model, then retry setup. Direct capture still works.",
            provider=spec.provider_id if spec else None, model=spec.model_id if spec else None,
        )
