from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosureState,
)
from work_buddy.journal_capture.models import CaptureMode, CaptureTarget
from work_buddy.journal_capture.service import CommittedIngress, JournalCaptureService
from work_buddy.journal_capture.smart import (
    JournalSmartProcessorSpec,
    JournalSourceBoundSmartProcessor,
    configured_journal_smart_processor,
    configured_journal_smart_processing,
    JournalSmartProcessingError,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.llm.response import LLMResponse
from work_buddy.llm.tiers import ModelTier
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import ActorRef
from work_buddy.sources.store import SourceStore
from work_buddy.settings import get_journal_day_window
from work_buddy.settings.broker import get_journal_smart_execution_path
from work_buddy.settings.registry import (
    JOURNAL_SMART_EXECUTION_API_MODEL,
    JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT,
)


class _Runner:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def call(self, **kwargs):
        self.inputs.append(kwargs["user"])
        assert kwargs["cache_ttl_minutes"] == 0
        return LLMResponse(
            structured_output={
                "target": "running_notes",
                "summary": "A follow-up worth retaining.",
                "effects": ["Classified; original text unchanged"],
            },
            model="claude-haiku-test",
            backend="anthropic",
        )


def _write(_relative, absolute, content, **_kwargs):
    absolute.write_bytes(content.encode("utf-8"))
    return True


def test_smart_processing_is_source_bound_and_manifested(tmp_path, monkeypatch):
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-08-09.md").write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    source_store = SourceStore.create(tmp_path / "sources", authority_id="authority")
    tenant = "tenant-local"
    issuer = ActorRef("issuer-local", "agent-execution", "service", tenant)
    item = source_store.capture_source(
        content="Remember to fix the parser",
        source_role="human_input",
        tenant_scope_id=tenant,
        originating_surface="test-journal",
    )
    journal_store = JournalCaptureStore(tmp_path / "journal.db")
    disclosure_sources = SourcesDisclosureService(
        source_store,
        tenant_scope_id=tenant,
        issuer=issuer,
    )
    manifests = DisclosureManifestStore(tmp_path / "agent-execution.db")
    runner = _Runner()
    processor = JournalSourceBoundSmartProcessor(
        sources_store=source_store,
        journal_store=journal_store,
        disclosure_sources=disclosure_sources,
        disclosure_gateway=DisclosureGateway(manifests, disclosure_sources),
        spec=JournalSmartProcessorSpec(
            tier=ModelTier.FRONTIER_FAST,
            provider_id="anthropic",
            model_id="claude-haiku-test",
        ),
        runner=runner,
    )
    service = JournalCaptureService(
        journal_store,
        JournalContentAdapter(tmp_path),
        smart_processor=processor,
    )
    window = get_journal_day_window("2026-08-09")
    capture = service.accept(
        ingress=CommittedIngress(
            source_ref=item.source_ref.uri,
            representation_id=item.primary_representation_id,
            submission_id="submission-smart",
            command_id="command-smart",
            effect_id="effect-smart",
            authorization_fingerprint="gesture-bound-authorization",
        ),
        client_mutation_id="mutation-smart",
        day_id=f"journal-day:2026-08-09:{window.timezone}:{window.boundary}",
        target=CaptureTarget.AUTO,
        mode=CaptureMode.SMART,
        exact_text="Remember to fix the parser",
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T19:15:00+00:00",
        run_smart=True,
    )

    assert capture.resolved_target is CaptureTarget.RUNNING_NOTES
    assert capture.annotation is not None
    assert capture.annotation["producer_ref"].startswith("agent-execution:journal-smart-")
    assert capture.annotation["disclosure_manifest_sha256"]
    assert runner.inputs == ["Remember to fix the parser"]
    run_id = capture.annotation["producer_ref"].split(":", 1)[1]
    entries = manifests.list_entries(run_id)
    assert len(entries) == 1
    assert entries[0].state is DisclosureState.SENT
    assert entries[0].source_ref == item.source_ref.uri
    representation = source_store.get_representation(item.primary_representation_id)
    assert representation is not None
    assert entries[0].content_sha256 == representation.content_sha256
    assert manifests.manifest_digest(run_id).manifest_sha256 == capture.annotation[
        "disclosure_manifest_sha256"
    ]

    # Settled work is idempotent and does not disclose the same source again.
    again = service.process_smart(
        capture.capture_id,
        exact_text="Remember to fix the parser",
    )
    assert again.revision == capture.revision
    assert runner.inputs == ["Remember to fix the parser"]


def test_production_smart_processor_requires_explicit_supported_config(
    tmp_path, monkeypatch
):
    sources = SourceStore.create(tmp_path / "sources", authority_id="authority")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"journal": {"smart_processing": {"enabled": False}}},
    )
    assert (
        configured_journal_smart_processor(
            sources, journal, execution_path=JOURNAL_SMART_EXECUTION_API_MODEL
        )
        is None
    )

    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "journal": {
                "smart_processing": {
                    "enabled": True,
                    "tier": "local_fast",
                }
            }
        },
    )
    # A local profile whose concrete model is learned only after dispatch
    # cannot make a truthful write-ahead provider/model claim.
    assert (
        configured_journal_smart_processor(
            sources, journal, execution_path=JOURNAL_SMART_EXECUTION_API_MODEL
        )
        is None
    )


def test_expired_smart_authorization_pauses_without_model_egress_and_can_be_reauthorized(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "2026-08-09.md").write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    source_store = SourceStore.create(tmp_path / "sources", authority_id="authority")
    tenant = "tenant-local"
    issuer = ActorRef("issuer-local", "agent-execution", "service", tenant)
    item = source_store.capture_source(
        content="Keep this exact text",
        source_role="human_input",
        tenant_scope_id=tenant,
        originating_surface="test-journal",
    )
    journal_store = JournalCaptureStore(tmp_path / "journal.db")
    disclosure_sources = SourcesDisclosureService(
        source_store,
        tenant_scope_id=tenant,
        issuer=issuer,
    )
    runner = _Runner()
    processor = JournalSourceBoundSmartProcessor(
        sources_store=source_store,
        journal_store=journal_store,
        disclosure_sources=disclosure_sources,
        disclosure_gateway=DisclosureGateway(
            DisclosureManifestStore(tmp_path / "agent-execution.db"),
            disclosure_sources,
        ),
        spec=JournalSmartProcessorSpec(
            tier=ModelTier.FRONTIER_FAST,
            provider_id="anthropic",
            model_id="claude-haiku-test",
        ),
        runner=runner,
    )
    service = JournalCaptureService(
        journal_store,
        JournalContentAdapter(tmp_path),
        smart_processor=processor,
    )
    window = get_journal_day_window("2026-08-09")
    capture = service.accept(
        ingress=CommittedIngress(
            source_ref=item.source_ref.uri,
            representation_id=item.primary_representation_id,
            submission_id="submission-expired",
            command_id="command-expired",
            effect_id="effect-expired",
            authorization_fingerprint="expired-authorization",
            authorization_expires_at="2000-01-01T00:00:00+00:00",
        ),
        client_mutation_id="mutation-expired",
        day_id=f"journal-day:2026-08-09:{window.timezone}:{window.boundary}",
        target=CaptureTarget.AUTO,
        mode=CaptureMode.SMART,
        exact_text="Keep this exact text",
        input_mode="direct_entry",
        stated_at=None,
        submitted_at="2026-08-09T19:15:00+00:00",
        run_smart=True,
    )

    assert capture.processing_status.value == "failed"
    assert capture.processing_error_code == "journal_authorization_expired"
    assert runner.inputs == []
    effect = next(
        effect
        for effect in journal_store.effects_for_capture(capture.capture_id)
        if effect.effect_type == "auto_route"
    )
    assert effect.state.value == "paused"

    journal_store.reauthorize_effect(
        capture.capture_id,
        "auto_route",
        authorization_fingerprint="fresh-authorization",
        authorization_expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    completed = service.process_smart(
        capture.capture_id,
        exact_text="Keep this exact text",
    )
    assert completed.processing_status.value == "succeeded"
    assert runner.inputs == ["Keep this exact text"]


def test_availability_distinguishes_opt_in_and_provider_failure(tmp_path, monkeypatch):
    sources = SourceStore.create(tmp_path / "sources")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {})
    processor, availability = configured_journal_smart_processing(
        sources, journal, execution_path=JOURNAL_SMART_EXECUTION_API_MODEL
    )
    assert processor is None and availability.state == "disabled_by_policy"
    assert availability.code == "smart_not_enabled"
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {
        "journal": {"smart_processing": {"enabled": True, "tier": "invalid-tier"}}
    })
    processor, availability = configured_journal_smart_processing(
        sources, journal, execution_path=JOURNAL_SMART_EXECUTION_API_MODEL
    )
    assert processor is None and availability.state == "provider_unavailable"
    assert availability.code == "provider_not_preflightable"
    assert availability.as_dict()["disclosure"]["maxInputBytes"] == 32768


def _enable_smart(monkeypatch) -> None:
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"journal": {"smart_processing": {"enabled": True}}},
    )


def _stub_account_selection(monkeypatch) -> None:
    """Answer the account preflight without probing a real local runtime."""

    from work_buddy.agent_execution import registry as execution_registry
    from work_buddy.agent_execution.models import AgentExecutionSelection

    selection = AgentExecutionSelection(
        "claude-code", "sonnet", "Claude Code", "Sonnet"
    )
    monkeypatch.setattr(
        execution_registry, "default_selection", lambda: selection
    )
    monkeypatch.setattr(
        execution_registry,
        "validate_selection",
        lambda candidate, refresh=False: candidate,
    )


def test_smart_execution_path_defaults_to_the_subscription_agent() -> None:
    # work-buddy already runs on an agent runtime the user signs in to, while
    # the API path additionally needs a configured API model, so the account
    # the user already has is the honest default.
    assert get_journal_smart_execution_path() == (
        JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT
    )


def test_subscription_agent_path_reports_the_account_provider_and_model(
    tmp_path, monkeypatch
):
    from work_buddy.journal_capture.smart_worker import (
        JournalAccountBackedSmartProcessor,
    )

    sources = SourceStore.create(tmp_path / "sources")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    _enable_smart(monkeypatch)
    _stub_account_selection(monkeypatch)
    processor, availability = configured_journal_smart_processing(
        sources, journal, execution_path=JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT
    )
    assert isinstance(processor, JournalAccountBackedSmartProcessor)
    assert availability.state == "ready" and availability.code == "ready"
    assert availability.provider == "Claude Code"
    assert availability.model == "Sonnet"
    assert "Claude Code · Sonnet" in processor.disclosure_summary
    assert "no web access" in processor.disclosure_summary


def test_api_model_path_reports_the_preflighted_provider_and_model(
    tmp_path, monkeypatch
):
    sources = SourceStore.create(tmp_path / "sources")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    _enable_smart(monkeypatch)
    processor, availability = configured_journal_smart_processing(
        sources, journal, execution_path=JOURNAL_SMART_EXECUTION_API_MODEL
    )
    assert isinstance(processor, JournalSourceBoundSmartProcessor)
    assert availability.state == "ready" and availability.code == "ready"
    assert availability.provider == "anthropic"
    assert availability.model == processor.spec.model_id
    assert f"anthropic · {processor.spec.model_id}" in processor.disclosure_summary
    assert "no tools or web access" in processor.disclosure_summary


def test_saved_setting_selects_the_path_without_an_explicit_argument(
    tmp_path, monkeypatch
):
    from work_buddy.settings import broker
    from work_buddy.settings.registry import JOURNAL_SMART_EXECUTION_ID
    from work_buddy.journal_capture.smart_worker import (
        JournalAccountBackedSmartProcessor,
    )

    sources = SourceStore.create(tmp_path / "sources")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    _enable_smart(monkeypatch)
    _stub_account_selection(monkeypatch)
    processor, _availability = configured_journal_smart_processing(sources, journal)
    assert isinstance(processor, JournalAccountBackedSmartProcessor)

    broker.update_value(
        JOURNAL_SMART_EXECUTION_ID,
        scope="profile",
        value=JOURNAL_SMART_EXECUTION_API_MODEL,
        expected_revision="value:0",
    )
    processor, _availability = configured_journal_smart_processing(sources, journal)
    assert isinstance(processor, JournalSourceBoundSmartProcessor)


def test_unavailable_subscription_account_is_reported_not_silently_swapped(
    tmp_path, monkeypatch
):
    from work_buddy.agent_execution import registry as execution_registry

    sources = SourceStore.create(tmp_path / "sources")
    journal = JournalCaptureStore(tmp_path / "journal.db")
    _enable_smart(monkeypatch)

    def unavailable() -> None:
        raise RuntimeError("no agent runtime is signed in")

    monkeypatch.setattr(execution_registry, "default_selection", unavailable)
    processor, availability = configured_journal_smart_processing(
        sources, journal, execution_path=JOURNAL_SMART_EXECUTION_SUBSCRIPTION_AGENT
    )
    assert processor is None
    assert availability.state == "provider_unavailable"
    assert availability.code == "provider_not_preflightable"


def test_one_capture_spawn_is_bounded_far_below_the_general_agent_allowance(
    monkeypatch,
):
    from work_buddy.agent_execution import registry as execution_registry
    from work_buddy.agent_execution.models import (
        AgentExecutionSelection,
        AgentSpawnOutcome,
    )
    from work_buddy.journal_capture.smart_worker import (
        SMART_PROCESSING_BUDGET_USD,
        JournalSmartProcessingRunner,
        JournalSmartWorkerSpec,
    )

    assert 0 < SMART_PROCESSING_BUDGET_USD <= 0.05

    requests = []

    def record(request):
        requests.append(request)
        return AgentSpawnOutcome(
            status="ok", selection=request.selection, pid=4321,
            session_id=request.session_id,
        )

    monkeypatch.setattr(execution_registry, "start_detached", record)

    class _Store:
        def claim_smart_processing_request(self, **_kwargs):
            return {"leaseToken": "lease-token"}

    class _Capture:
        capture_id = "capture-budget"

    class _Effect:
        effect_id = "effect-budget"

    outcome = JournalSmartProcessingRunner().start(
        store=_Store(),
        capture=_Capture(),
        effect=_Effect(),
        spec=JournalSmartWorkerSpec(
            AgentExecutionSelection("claude-code", "sonnet", "Claude Code", "Sonnet")
        ),
        smart_disclosure_sha256="disclosure-hash",
    )
    assert outcome["status"] == "started"
    assert requests[0].max_budget_usd == SMART_PROCESSING_BUDGET_USD


@pytest.mark.parametrize("follow_up", [
    [{"kind": "task_proposal", "task_text": "one", "rationale": "why"}],
    {"kind": "task_create", "task_text": "one", "rationale": "why"},
    {"kind": "task_proposal", "task_text": "one", "rationale": "why", "href": "https://example.com"},
    {"kind": "task_proposal", "task_text": "", "rationale": "why"},
])
def test_model_cannot_supply_multiple_actions_direct_tasks_or_links(follow_up):
    with pytest.raises(JournalSmartProcessingError):
        JournalSourceBoundSmartProcessor._validated_result(LLMResponse(structured_output={
            "target": "running_notes", "summary": "Intention", "effects": [], "follow_up": follow_up,
        }))
