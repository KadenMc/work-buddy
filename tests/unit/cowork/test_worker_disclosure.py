from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.agent_execution.disclosure import (
    DisclosureGateway,
    DisclosureManifestStore,
    DisclosureReplayBlocked,
    DisclosureSourceError,
    DisclosureState,
    SourceAcknowledgementState,
)
from work_buddy.cowork.worker_disclosure import (
    CoworkWorkerDisclosureBoundary,
    CoworkWorkerRun,
)
from work_buddy.sources import ActorRef, SourceStore
from work_buddy.sources.disclosure import SourcesDisclosureService
from work_buddy.sources.models import SourceRef


@pytest.fixture
def boundary(tmp_path: Path):
    source_store = SourceStore.create(tmp_path / "sources")
    tenant_id = "tenant-cowork-tests"
    issuer = ActorRef(
        source_store.authority_id,
        "cowork-agent-execution",
        "service",
        tenant_id,
    )
    sources = SourcesDisclosureService(
        source_store,
        tenant_scope_id=tenant_id,
        issuer=issuer,
    )
    manifests = DisclosureManifestStore(tmp_path / "agent-execution.db")
    gateway = DisclosureGateway(manifests, sources)
    return (
        CoworkWorkerDisclosureBoundary(gateway, sources),
        source_store,
        gateway,
    )


def _run() -> CoworkWorkerRun:
    return CoworkWorkerRun(
        run_id="cowork-worker-run-1",
        worker_session_id="cowork-worker-run-1",
        provider_id="claude-code",
        model_id="sonnet",
        authorization_ref="cowork-document-agent:conversation-1:generation-1",
        purpose="cowork_document_agent",
    )


def test_worker_inputs_are_ordered_and_output_binds_exact_manifest(boundary):
    disclosure, _sources, gateway = boundary
    run = _run()

    first, _digest = disclosure.account_payload(
        run,
        payload={"document": "first frozen version"},
        source_role="managed_document_context",
        tool_call_id="cowork_doc_get",
        idempotency_key="cowork-doc-get:first",
    )
    second, digest = disclosure.account_payload(
        run,
        payload={"target": "one exact frozen passage"},
        source_role="document_action_snapshot",
        tool_call_id="cowork_action_snapshot_get",
        idempotency_key="cowork-action-get:second",
    )
    assert first.state is DisclosureState.POSSIBLY_SENT
    assert second.state is DisclosureState.POSSIBLY_SENT
    binding = disclosure.bind_output(
        run,
        output_ref="cowork-chat-message:message-1",
        idempotency_key="cowork-chat-output:message-1",
    )

    persisted = gateway.store.list_entries(run.run_id)
    assert [entry.id for entry in persisted] == [first.id, second.id]
    assert all(entry.state is DisclosureState.SENT for entry in persisted)
    assert all(
        entry.source_acknowledgement is SourceAcknowledgementState.ACKNOWLEDGED
        for entry in persisted
    )
    assert digest.entry_count == 2
    assert binding.manifest_sha256 == gateway.store.manifest_digest(
        run.run_id
    ).manifest_sha256
    assert binding.entry_count == 2


def test_redaction_epoch_invalidates_replay_and_output_binding(boundary):
    disclosure, source_store, _gateway = boundary
    run = _run()
    entry, _digest = disclosure.account_payload(
        run,
        payload={"document": "private frozen bytes"},
        source_role="managed_document_context",
        tool_call_id="cowork_doc_get",
        idempotency_key="cowork-doc-get:redaction",
    )
    source_ref = SourceRef.parse(entry.source_ref)
    with source_store.write_transaction() as conn:
        conn.execute(
            "UPDATE source_items SET lifecycle_state = 'redacted', "
            "redaction_epoch = redaction_epoch + 1 WHERE authority_id = ? "
            "AND source_item_id = ?",
            (source_ref.authority_id, source_ref.item_id),
        )
        conn.execute(
            "UPDATE source_representations SET inline_content = NULL, "
            "blob_sha256 = NULL, redacted_at = '2026-08-10T00:00:00Z' "
            "WHERE authority_id = ? AND source_item_id = ?",
            (source_ref.authority_id, source_ref.item_id),
        )

    with pytest.raises(DisclosureSourceError):
        disclosure.account_payload(
            run,
            payload={"document": "private frozen bytes"},
            source_role="managed_document_context",
            tool_call_id="cowork_doc_get",
            idempotency_key="cowork-doc-get:redaction",
        )
    with pytest.raises(DisclosureSourceError):
        disclosure.bind_output(
            run,
            output_ref="cowork-chat-message:message-redacted",
            idempotency_key="cowork-chat-output:message-redacted",
        )


def test_crash_after_local_handoff_never_auto_replays_ambiguous_input(boundary):
    disclosure, _sources, gateway = boundary
    run = _run()
    entry, _digest = disclosure.account_payload(
        run,
        payload={"document": "ambiguous frozen bytes"},
        source_role="managed_document_context",
        tool_call_id="cowork_doc_get",
        idempotency_key="cowork-doc-get:ambiguous",
    )
    assert entry.state is DisclosureState.POSSIBLY_SENT
    assert gateway.store.get_entry(entry.id).state is DisclosureState.POSSIBLY_SENT

    # Model/gateway delivery crashes here: no output call arrives to provide a
    # causal acknowledgement. The same logical read remains permanently
    # ambiguous and is never returned automatically again.
    with pytest.raises(DisclosureReplayBlocked):
        disclosure.account_payload(
            run,
            payload={"document": "ambiguous frozen bytes"},
            source_role="managed_document_context",
            tool_call_id="cowork_doc_get",
            idempotency_key="cowork-doc-get:ambiguous",
        )
    assert gateway.store.get_entry(entry.id).state is DisclosureState.POSSIBLY_SENT
