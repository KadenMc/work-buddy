from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.journal_capture import api as journal_api
from work_buddy.journal_capture.actions import JournalActionSourceService
from work_buddy.journal_capture.configuration import JournalProfileConfigurationService
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalCaptureValidationError,
)
from work_buddy.journal_capture.prompt_worker import (
    JournalPromptGenerationCapabilityService,
)
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    AgentOutputRequest,
    HumanInputRequest,
    SourceRef,
    SourceStore,
    TrustedIngressContext,
    TrustedIngressService,
    redact_source,
)
from work_buddy.truth.registry import TruthStoreRegistry


def _context(
    *,
    inputter: ActorRef,
    service: ActorRef,
    purposes: tuple[str, ...],
    namespace: str,
) -> TrustedIngressContext:
    issuer = ActorRef(
        inputter.issuer_authority_id,
        "trusted-test-ingress",
        "service",
        inputter.tenant_scope_id,
    )
    return TrustedIngressContext(
        issuer=issuer,
        issuer_version="test/v1",
        inputter=inputter,
        service_principal=service,
        tenant_scope_id=inputter.tenant_scope_id,
        surface="journal-test",
        namespace=namespace,
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="test",
        authorization_fingerprint="a" * 64,
        permitted_purposes=purposes,
    )


def _database_authority(store: JournalCaptureStore) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only' "
            "WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )


def test_versioned_item_actions_keep_cas_routes_and_selective_source_erasure(tmp_path):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    domain = JournalDomainService(journal)
    human = ActorRef("authority-test", "person-test", "human", "tenant-test")
    service = ActorRef(
        "authority-test", "work-buddy-journal-service", "service", "tenant-test"
    )
    trusted = _context(
        inputter=human,
        service=service,
        purposes=("journal.item_revision",),
        namespace="journal-item-action",
    )
    item = domain.create_native_item(
        local_date="2026-08-27",
        item_kind="running_note",
        plain_value="Original note",
        source_ref="wb-source://test/original",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="create-original-note",
        actor={"subject": "person-test"},
    )
    ingress = TrustedIngressService(sources)
    first = ingress.commit_human_input(
        trusted,
        HumanInputRequest(
            exact_content="First revision",
            client_mutation_id="source-first-revision",
            input_mode="direct_entry",
        ),
    )
    actions = JournalActionSourceService(journal, sources)
    edited = actions.update_item(
        source_ref=first.source_ref,
        representation_id=first.representation_id,
        service_principal=service,
        item_id=item.item_id,
        expected_revision=1,
        operation="edit",
        plain_value="First revision",
        client_mutation_id="action-first-revision",
        actor={"subject": "person-test"},
    )
    assert edited.current_revision == 2
    assert actions.update_item(
        source_ref=first.source_ref,
        representation_id=first.representation_id,
        service_principal=service,
        item_id=item.item_id,
        expected_revision=1,
        operation="edit",
        plain_value="First revision",
        client_mutation_id="action-first-revision",
        actor={"subject": "person-test"},
    ).current_revision == 2

    second = ingress.commit_human_input(
        trusted,
        HumanInputRequest(
            exact_content="Current revision",
            client_mutation_id="source-current-revision",
            input_mode="direct_entry",
        ),
    )
    corrected = actions.update_item(
        source_ref=second.source_ref,
        representation_id=second.representation_id,
        service_principal=service,
        item_id=item.item_id,
        expected_revision=2,
        operation="correct",
        plain_value="Current revision",
        client_mutation_id="action-current-revision",
        actor={"subject": "person-test"},
    )
    with pytest.raises(JournalCaptureConflict):
        domain.transition_native_item(
            item_id=item.item_id,
            expected_revision=2,
            operation="resolve",
            client_mutation_id="stale-resolution",
            actor={"subject": "person-test"},
        )
    routed, relation = domain.route_native_item(
        item_id=item.item_id,
        expected_revision=corrected.current_revision,
        target_domain="task",
        target_id="task-neutral-1",
        client_mutation_id="route-current-note",
        actor={"subject": "person-test"},
    )
    assert routed.lifecycle == "resolved"
    assert relation.lifecycle == "current"
    restored = domain.transition_native_item(
        item_id=item.item_id,
        expected_revision=routed.current_revision,
        operation="restore",
        client_mutation_id="restore-current-note",
        actor={"subject": "person-test"},
    )
    assert restored.lifecycle == "current"
    assert domain.list_relations(item.item_id)[0].lifecycle == "archived"

    sources.grant_access(
        source_ref=first.source_ref,
        principal=human,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=trusted.authorization_fingerprint,
    )
    redaction = redact_source(
        sources,
        source_ref=first.source_ref,
        actor=human,
        authorization_fingerprint=trusted.authorization_fingerprint,
        reason_code="user_requested",
    )
    assert redaction.pending_effect_ids
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=service,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    assert dispatcher.drain().delivered == 1
    assert domain.get_native_item(item.item_id).plain_value == "Current revision"
    with journal._connect() as conn:
        revisions = conn.execute(
            "SELECT revision,plain_value FROM journal_item_revisions "
            "WHERE item_id=? ORDER BY revision",
            (item.item_id,),
        ).fetchall()
    assert (2, "[redacted]") in [tuple(row) for row in revisions]
    assert (3, "Current revision") in [tuple(row) for row in revisions]


def test_prompt_generation_is_durable_source_backed_and_explicitly_reviewed(tmp_path):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    domain = JournalDomainService(journal)
    domain.create_prompt_definition_version(
        prompt_id="focus-context",
        wording="Add useful context",
    )
    domain.create_module_instance_version(
        module_instance_id="prompt.focus-context",
        module_type_id="prompt_result",
        module_type_version=1,
        label="Focus context",
        behavior_id="provenance_only",
        behavior_version=1,
    )
    human = ActorRef("authority-test", "person-test", "human", "tenant-test")
    service = ActorRef(
        "authority-test", "work-buddy-journal-service", "service", "tenant-test"
    )
    human_context = _context(
        inputter=human,
        service=service,
        purposes=("journal.prompt_input",),
        namespace="journal-prompt-input",
    )
    seed = TrustedIngressService(sources).commit_human_input(
        human_context,
        HumanInputRequest(
            exact_content="Keep the original seed separate.",
            client_mutation_id="prompt-seed-source-0001",
            input_mode="direct_entry",
        ),
    )
    actions = JournalActionSourceService(journal, sources)
    interaction = actions.create_prompt_interaction(
        source_ref=seed.source_ref,
        representation_id=seed.representation_id,
        service_principal=service,
        interaction_id="interaction-neutral-1",
        local_date="2026-08-27",
        module_instance_id="prompt.focus-context",
        module_instance_version=1,
        prompt_id="focus-context",
        prompt_version=1,
        input_text="Keep the original seed separate.",
        result_retention="all_versions",
        result_search_mode="content",
        client_mutation_id="prompt-seed-create-0001",
    )
    with pytest.raises(JournalCaptureConflict):
        domain.create_prompt_interaction(
            interaction_id="interaction-neutral-duplicate",
            local_date="2026-08-27",
            module_instance_id="prompt.focus-context",
            module_instance_version=1,
            prompt_id="focus-context",
            prompt_version=1,
            input_text="A second seed must not displace the first.",
            source_ref="wb-source://test/duplicate-seed",
            result_retention="all_versions",
            result_search_mode="content",
            client_mutation_id="prompt-seed-duplicate-0001",
        )
    generation = domain.request_prompt_generation(
        interaction_id=str(interaction["interactionId"]),
        expected_revision=1,
        client_mutation_id="prompt-generate-request-0001",
        actor={"subject": "person-test"},
        context_manifest={"schema": "test/v1", "disclosedContext": []},
    )
    assert generation["status"] == "pending"
    assert interaction["variants"] == []
    lease = domain.claim_prompt_generation_request(
        request_id=str(generation["requestId"]),
        worker_id="journal-worker-neutral",
        provider_id="test-provider",
        model_id="test-model",
    )
    assert lease is not None and lease["status"] == "leased"

    class FakeDisclosure:
        def __init__(self):
            self.accounted = False
            self.bound = False

        def account_payload(self, _run, **_kwargs):
            self.accounted = True
            return object(), SimpleNamespace(manifest_sha256="b" * 64)

        def bind_output(self, _run, **_kwargs):
            assert self.accounted
            self.bound = True
            return SimpleNamespace(
                manifest_sha256="b" * 64,
                entry_count=1,
            )

    disclosure = FakeDisclosure()
    worker = JournalPromptGenerationCapabilityService(
        journal,
        sources,
        disclosure=disclosure,  # type: ignore[arg-type]
    )
    context_payload = worker.context(
        request_id=str(lease["requestId"]),
        lease_token=str(lease["leaseToken"]),
        agent_session_id="journal-worker-neutral",
    )
    assert context_payload["seed"] == "Keep the original seed separate."
    completed = worker.complete(
        request_id=str(lease["requestId"]),
        lease_token=str(lease["leaseToken"]),
        result_text="Generated context variant.",
        agent_session_id="journal-worker-neutral",
    )
    variant_id = str(completed["variantId"])
    assert disclosure.bound is True
    output_item = sources.get_item(SourceRef.parse(str(completed["sourceRef"])))
    assert output_item is not None and output_item.source_role == "agent_output"
    current = domain.get_prompt_interaction("interaction-neutral-1")
    assert current["inputText"] == "Keep the original seed separate."
    assert current["variants"][0]["variantId"] == variant_id
    assert current["variants"][0]["authorship"] == "generated"
    assert current["variants"][0]["reviewState"] == "unreviewed"
    assert current["generationRequests"][0]["status"] == "succeeded"
    revision = domain.decide_prompt_result(
        interaction_id="interaction-neutral-1",
        variant_id=variant_id,
        decision_kind="accept",
        expected_revision=2,
        client_mutation_id="prompt-accept-result-0001",
        actor={"subject": "person-test"},
    )
    assert revision == 3
    assert domain.get_prompt_interaction("interaction-neutral-1")["variants"][0][
        "lifecycle"
    ] == "accepted"

    crashed = domain.request_prompt_generation(
        interaction_id="interaction-neutral-1",
        expected_revision=3,
        client_mutation_id="prompt-generate-request-crashed",
        actor={"subject": "person-test"},
        context_manifest={"schema": "test/v1", "disclosedContext": []},
    )
    crashed_lease = domain.claim_prompt_generation_request(
        request_id=str(crashed["requestId"]),
        worker_id="journal-worker-crashed",
        provider_id="test-provider",
        model_id="test-model",
    )
    assert crashed_lease is not None
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE journal_prompt_generation_requests SET lease_expires_at=? "
            "WHERE request_id=?",
            ("2000-01-01T00:00:00+00:00", crashed["requestId"]),
        )
    expired = domain.get_prompt_generation_request(str(crashed["requestId"]))
    assert expired["status"] == "expired"
    assert expired["retryable"] is True

    # Recovery requires a fresh explicit mutation. It reuses the durable
    # request identity, revokes the old token, and increments attempts only
    # when the replacement worker actually claims the request.
    retry = domain.request_prompt_generation(
        interaction_id="interaction-neutral-1",
        expected_revision=3,
        client_mutation_id="prompt-generate-request-retry",
        actor={"subject": "person-test"},
        context_manifest={"schema": "test/v1", "disclosedContext": []},
    )
    assert retry["requestId"] == crashed["requestId"]
    assert retry["status"] == "pending"
    replacement = domain.claim_prompt_generation_request(
        request_id=str(retry["requestId"]),
        worker_id="journal-worker-replacement",
        provider_id="test-provider",
        model_id="test-model",
    )
    assert replacement is not None and replacement["attempts"] == 2
    with pytest.raises(JournalCaptureConflict):
        domain.validate_prompt_generation_lease(
            request_id=str(crashed["requestId"]),
            lease_token=str(crashed_lease["leaseToken"]),
        )
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE journal_prompt_generation_requests SET lease_expires_at=? "
            "WHERE request_id=?",
            ("2000-01-01T00:00:00+00:00", retry["requestId"]),
        )
    bounded = domain.claim_prompt_generation_request(
        request_id=str(retry["requestId"]),
        worker_id="journal-worker-bounded-reclaim",
    )
    assert bounded is not None and bounded["attempts"] == 3
    with journal.transaction() as conn:
        conn.execute(
            "UPDATE journal_prompt_generation_requests SET lease_expires_at=? "
            "WHERE request_id=?",
            ("2000-01-01T00:00:00+00:00", retry["requestId"]),
        )
    assert domain.claim_prompt_generation_request(
        request_id=str(retry["requestId"]),
        worker_id="journal-worker-unbounded-reclaim",
    ) is None
    assert domain.get_prompt_generation_request(str(retry["requestId"]))[
        "status"
    ] == "failed"
    explicit_after_bound = domain.request_prompt_generation(
        interaction_id="interaction-neutral-1",
        expected_revision=3,
        client_mutation_id="prompt-generate-request-explicit-after-bound",
        actor={"subject": "person-test"},
        context_manifest={"schema": "test/v1", "disclosedContext": []},
    )
    explicit_lease = domain.claim_prompt_generation_request(
        request_id=str(explicit_after_bound["requestId"]),
        worker_id="journal-worker-explicit-after-bound",
    )
    assert explicit_lease is not None and explicit_lease["attempts"] == 4


def test_prompt_generation_fails_closed_when_module_behavior_forbids_ai(tmp_path):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(journal)
    domain.create_prompt_definition_version(
        prompt_id="human-only-prompt",
        wording="Keep this human-only",
    )
    domain.create_module_instance_version(
        module_instance_id="human-only.prompt",
        module_type_id="prompt_result",
        module_type_version=1,
        label="Human-only prompt",
        behavior_id="human_value",
        behavior_version=1,
    )
    domain.create_prompt_interaction(
        interaction_id="interaction-human-only-1",
        local_date="2026-08-27",
        module_instance_id="human-only.prompt",
        module_instance_version=1,
        prompt_id="human-only-prompt",
        prompt_version=1,
        input_text="Do not disclose this to a model.",
        source_ref="wb-source://test/human-only",
        result_retention="all_versions",
        result_search_mode="content",
    )

    with pytest.raises(
        JournalCaptureValidationError,
        match="AI generation is not permitted",
    ):
        domain.request_prompt_generation(
            interaction_id="interaction-human-only-1",
            expected_revision=1,
            client_mutation_id="human-only-generation-rejected",
            actor={"subject": "person-test"},
            context_manifest={"schema": "test/v1", "disclosedContext": []},
        )


def _prompt_profile() -> dict:
    return {
        "profileId": "user.neutral.prompt.profile",
        "expectedRevision": 0,
        "name": "Context Journal",
        "description": "A neutral prompt test profile.",
        "modules": [
            {
                "slotId": "context",
                "moduleInstanceId": "user.neutral.context",
                "expectedVersion": 0,
                "moduleTypeId": "prompt_result",
                "moduleTypeVersion": 1,
                "label": "Context",
                "behaviorId": "provenance_only",
                "behaviorVersion": 1,
                "scheduleKind": "always",
                "schedule": {},
                "fields": [
                    {
                        "slotId": "focus",
                        "fieldId": "user.neutral.context.focus",
                        "expectedVersion": 0,
                        "owner": "user",
                        "stableKey": "focus",
                        "label": "Focus",
                        "description": "A short seed for generated context.",
                        "valueKind": "long_text",
                        "behaviorId": "provenance_only",
                        "behaviorVersion": 1,
                        "prompt": {
                            "promptId": "user.neutral.context.focus.prompt",
                            "expectedVersion": 0,
                            "wording": "Add useful context",
                            "helpText": "The seed remains visible separately.",
                            "requiredness": "optional",
                            "scheduleKind": "always",
                        },
                    }
                ],
            }
        ],
    }


def test_public_item_and_prompt_api_enforce_authority_cas_and_pending_generation(
    tmp_path, monkeypatch
):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    domain = JournalDomainService(store)
    local_date = journal_api.current_day()["localDate"]
    configuration = JournalProfileConfigurationService(store)
    configuration.save(
        _prompt_profile(),
        client_mutation_id="save-neutral-prompt-profile",
        actor={"subject": "person-test"},
    )
    domain.activate_profile(
        profile_id="user.neutral.prompt.profile",
        profile_revision=1,
        effective_local_date=local_date,
        expected_activation_revision=1,
        client_mutation_id="activate-neutral-prompt-profile",
        actor={"subject": "person-test"},
    )
    _database_authority(store)
    item = domain.create_native_item(
        local_date=local_date,
        item_kind="running_note",
        plain_value="Initial action note",
        source_ref="wb-source://test/action-note",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="create-api-action-note",
        actor={"subject": "person-test"},
        module_instance_id="user.neutral.context",
        module_instance_version=1,
    )
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    human = ActorRef("authority-http", "person-http", "human", "tenant-http")
    gestures: list[tuple[str, str]] = []

    def authorize(*, action, subject, context_sha256):
        gestures.append((action, subject))
        assert len(context_sha256) == 64
        return SimpleNamespace(
            principal=SimpleNamespace(actor=human),
            gesture_id=f"gesture-{len(gestures)}",
            action=action,
            assurance="enrolled_local_session_gesture",
        )

    monkeypatch.setattr(journal_api, "require_human_authority_request", authorize)

    class FakeGenerationRunner:
        def prepare(self):
            return AgentExecutionSelection("test-provider", "test-model")

        def start(self, **_kwargs):
            return {
                "status": "started",
                "providerId": "test-provider",
                "modelId": "test-model",
                "workerSessionId": "test-worker-session",
            }

    monkeypatch.setattr(
        journal_api, "_prompt_generation_runner", FakeGenerationRunner()
    )
    app = Flask("journal-public-actions")
    journal_api.register_routes(app)
    client = app.test_client()

    edited = client.post(
        f"/api/journal/items/{item.item_id}/edit",
        json={
            "clientMutationId": "http-edit-action-note",
            "expectedRevision": 1,
            "exactText": "Edited action note",
        },
    )
    assert edited.status_code == 200, edited.json
    assert edited.json["item"]["revision"] == 2
    assert "correct" in edited.json["item"]["actions"]
    stale = client.post(
        f"/api/journal/items/{item.item_id}/resolve",
        json={
            "clientMutationId": "http-stale-action-note",
            "expectedRevision": 1,
        },
    )
    assert stale.status_code == 409

    prompt_body = {
        "clientMutationId": "http-create-prompt-seed",
        "localDate": local_date,
        "moduleInstanceId": "user.neutral.context",
        "moduleInstanceVersion": 1,
        "promptId": "user.neutral.context.focus.prompt",
        "promptVersion": 1,
        "exactInput": "A concise seed.",
        "resultRetention": "all_versions",
        "resultSearchMode": "content",
    }
    created = client.post("/api/journal/prompt-interactions", json=prompt_body)
    assert created.status_code == 201, created.json
    interaction = created.json["interaction"]
    queued = client.post(
        f"/api/journal/prompt-interactions/{interaction['interactionId']}/generate",
        json={
            "clientMutationId": "http-queue-prompt-generation",
            "expectedRevision": 1,
        },
    )
    assert queued.status_code == 200, queued.json
    assert queued.json["generation"]["status"] == "pending"
    assert queued.json["interaction"]["variants"] == []

    lease = domain.claim_prompt_generation(worker_id="http-test-worker")
    assert lease is not None
    agent = ActorRef(
        "authority-http", "agent-run-http", "agent_run", "tenant-http"
    )
    output_context = _context(
        inputter=agent,
        service=ActorRef(
            "authority-http",
            "work-buddy-journal-service",
            "service",
            "tenant-http",
        ),
        purposes=("journal.prompt_result",),
        namespace="journal-prompt-result",
    )
    output = TrustedIngressService(sources).commit_agent_output(
        output_context,
        AgentOutputRequest(
            exact_content="Generated context from the seed.",
            client_mutation_id="http-prompt-result-source",
        ),
    )
    ingested = client.post(
        f"/api/journal/prompt-generations/{lease['requestId']}/results",
        json={
            "leaseToken": lease["leaseToken"],
            "sourceRef": output.source_ref.uri,
            "representationId": output.representation_id,
            "clientMutationId": "http-prompt-result-ingest",
            "producerId": "http-test-worker",
            "providerId": "test-provider",
            "modelId": "test-model",
            "generationReceipt": {"runId": "agent-run-http"},
        },
    )
    assert ingested.status_code == 200, ingested.json
    assert ingested.json["interaction"]["variants"][0]["resultText"] == (
        "Generated context from the seed."
    )
    variant_id = ingested.json["variantId"]
    decided = client.post(
        f"/api/journal/prompt-interactions/{interaction['interactionId']}"
        f"/variants/{variant_id}/decide",
        json={
            "clientMutationId": "http-accept-prompt-result",
            "expectedRevision": 2,
            "decision": "accept",
        },
    )
    assert decided.status_code == 200, decided.json
    assert decided.json["interaction"]["variants"][0]["lifecycle"] == "accepted"

    with sources.connect() as conn:
        source_count_before_failure = conn.execute(
            "SELECT COUNT(*) FROM source_items"
        ).fetchone()[0]
    failed_lease: dict[str, str] = {}
    failed_launches = 0

    class FailingGenerationRunner:
        def prepare(self):
            return AgentExecutionSelection("test-provider", "test-model")

        def start(self, *, store, request_id, **_kwargs):
            nonlocal failed_launches
            failed_launches += 1
            failed = JournalDomainService(store)
            claimed = failed.claim_prompt_generation_request(
                request_id=request_id,
                worker_id=f"journal-prompt:{request_id}",
                provider_id="test-provider",
                model_id="test-model",
            )
            assert claimed is not None
            failed_lease.update(
                request_id=request_id,
                lease_token=str(claimed["leaseToken"]),
            )
            failed.fail_prompt_generation(
                request_id=request_id,
                lease_token=str(claimed["leaseToken"]),
                error_code="worker_launch_failed",
            )
            raise RuntimeError("detached worker launch failed")

    monkeypatch.setattr(
        journal_api, "_prompt_generation_runner", FailingGenerationRunner()
    )
    failed_body = {
        "clientMutationId": "http-failed-prompt-generation",
        "expectedRevision": 3,
    }
    failed = client.post(
        f"/api/journal/prompt-interactions/{interaction['interactionId']}/generate",
        json=failed_body,
    )
    assert failed.status_code == 503, failed.json
    assert failed.json["generation"]["status"] == "failed"
    assert failed.json["generation"]["retryable"] is True
    assert len(failed.json["interaction"]["variants"]) == 1

    # An HTTP replay of the same gesture reports the durable failure instead
    # of launching again or creating another Source/result variant.
    replay = client.post(
        f"/api/journal/prompt-interactions/{interaction['interactionId']}/generate",
        json=failed_body,
    )
    assert replay.status_code == 200, replay.json
    assert replay.json["generation"]["status"] == "failed"
    assert replay.json["dispatch"] is None
    assert failed_launches == 1
    assert len(replay.json["interaction"]["variants"]) == 1
    with sources.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM source_items"
        ).fetchone()[0] == source_count_before_failure

    class RetryGenerationRunner:
        def prepare(self):
            return AgentExecutionSelection("test-provider", "test-model")

        def start(self, *, store, request_id, **_kwargs):
            claimed = JournalDomainService(store).claim_prompt_generation_request(
                request_id=request_id,
                worker_id=f"journal-prompt:{request_id}",
                provider_id="test-provider",
                model_id="test-model",
            )
            assert claimed is not None
            return {"status": "started", "workerSessionId": f"journal-prompt:{request_id}"}

    monkeypatch.setattr(
        journal_api, "_prompt_generation_runner", RetryGenerationRunner()
    )
    retried = client.post(
        f"/api/journal/prompt-interactions/{interaction['interactionId']}/generate",
        json={
            "clientMutationId": "http-retry-prompt-generation",
            "expectedRevision": 3,
        },
    )
    assert retried.status_code == 200, retried.json
    assert retried.json["generation"]["status"] == "leased"
    assert retried.json["generation"]["attempts"] == 2

    # Retry revoked the failed attempt's token. A late completion cannot win
    # after replacement and cannot add a duplicate variant.
    late = client.post(
        f"/api/journal/prompt-generations/{failed_lease['request_id']}/results",
        json={
            "leaseToken": failed_lease["lease_token"],
            "sourceRef": output.source_ref.uri,
            "representationId": output.representation_id,
            "clientMutationId": "http-late-prompt-result",
            "producerId": "late-test-worker",
            "providerId": "test-provider",
            "modelId": "test-model",
            "generationReceipt": {"runId": "late-agent-run"},
        },
    )
    assert late.status_code == 409, late.json
    assert len(
        domain.get_prompt_interaction(interaction["interactionId"])["variants"]
    ) == 1
    assert gestures == [
        ("journal.item.edit", f"journal-item:{item.item_id}"),
        ("journal.item.resolve", f"journal-item:{item.item_id}"),
        (
            "journal.prompt.create",
            f"journal-prompt:{local_date}:user.neutral.context:user.neutral.context.focus.prompt",
        ),
        ("journal.prompt.generate", f"journal-prompt:{interaction['interactionId']}"),
        (
            "journal.prompt.decide",
            f"journal-prompt:{interaction['interactionId']}:{variant_id}",
        ),
        ("journal.prompt.generate", f"journal-prompt:{interaction['interactionId']}"),
        ("journal.prompt.generate", f"journal-prompt:{interaction['interactionId']}"),
        ("journal.prompt.generate", f"journal-prompt:{interaction['interactionId']}"),
    ]
