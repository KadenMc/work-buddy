"""Focused tests for the persisted Co-work document-agent lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from work_buddy.agent_execution import registry as execution_registry
from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.conversations import execution as conversation_execution
from work_buddy.conversations import store as conversation_store
from work_buddy.cowork import document_agent


@pytest.fixture
def document_agent_store(tmp_path, monkeypatch):
    database = tmp_path / "throwaway-document-agent.db"
    monkeypatch.setattr(conversation_store, "_DB_PATH", database)
    conn = conversation_store.get_connection()
    try:
        conversation_store._ensure_schema(conn)
    finally:
        conn.close()
    return database


def _conversation():
    return conversation_store.create_conversation(
        title="Throwaway Co-work chat",
        source="cowork_document",
        metadata={
            "cowork_store_id": "store-a",
            "cowork_document_id": "doc-a",
        },
    )


def test_prompt_binds_authority_but_contains_no_source_content() -> None:
    prompt = document_agent.build_document_agent_prompt(
        store_id="store-a",
        document_id="doc-a",
        conversation_id="conversation-a",
        consumer="cowork-document:store-a:doc-a",
        generation="generation-a",
        producer_model="configured-model",
        conversation_history=[
            {
                "message_id": "cowork-reply-user-old",
                "role": "agent",
                "content": "Already handled.",
                "message_type": "text",
                "status": "sent",
            }
        ],
        feedback=document_agent.FeedbackPromptContext(
            text="Needs support.",
            exact="Selected sentence.",
            prefix="Before ",
            suffix=" After",
            message_id="user-new",
        ),
    )

    for binding in (
        "store_id: store-a",
        "document_id: doc-a",
        "conversation_id: conversation-a",
        "inbox_generation: generation-a",
        "producer_model: configured-model",
    ):
        assert binding in prompt
    assert "cowork_doc_get again after every receive" in prompt
    assert "exact message_id" in prompt
    assert "Never infer this" in prompt
    assert "association by matching text" in prompt
    assert 'message_id="cowork-reply-<that user' in prompt
    assert "created=true or replayed=true" in prompt
    assert "conversation_ack" in prompt
    assert "lease_lost" in prompt
    assert "Never write the Markdown file or Yjs state directly" in prompt
    assert "Never use conversation_ask for an open-ended/freeform prompt" in prompt
    assert "producer_model='configured-model'" in prompt
    assert "consumer='cowork-document:store-a:doc-a'" in prompt
    assert "generation='generation-a'" in prompt
    assert "Source delivery boundary" in prompt
    assert "intentionally contains no document" in prompt
    for protected in (
        "Already handled.",
        "Needs support.",
        "Selected sentence.",
        "user-new",
        "BEGIN COWORK CONVERSATION JSON",
        "BEGIN COWORK FEEDBACK JSON",
    ):
        assert protected not in prompt


def test_bound_wake_reuses_selected_execution_and_preserves_lock_order(
    document_agent_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del document_agent_store
    from work_buddy.cowork import lifecycle_lock, policy
    from work_buddy.truth import documents as truth_documents
    from work_buddy.truth import registry as truth_registry

    conversation = _conversation()
    selected = AgentExecutionSelection(
        provider_id="codex",
        model_id="gpt-selected",
        provider_label="Codex",
        model_label="Selected model",
    )
    conversation_execution.set_execution(
        conversation.conversation_id,
        selected.to_dict(),
        expected_revision=None,
    )
    monkeypatch.setattr(
        execution_registry,
        "default_selection",
        lambda: AgentExecutionSelection(
            provider_id="claude-code",
            model_id="sonnet",
            provider_label="Claude Code",
            model_label="Sonnet",
        ),
    )

    events: list[str] = []
    original_get = conversation_store.get_conversation

    def _get_conversation(conversation_id: str):
        events.append("conversations")
        return original_get(conversation_id)

    @contextmanager
    def _lifecycle_guard(store_id: str, document_id: str):
        assert (store_id, document_id) == ("store-a", "doc-a")
        events.append("lifecycle-enter")
        try:
            yield
        finally:
            events.append("lifecycle-exit")

    class _Registry:
        def open_store(self, store_id: str):
            assert store_id == "store-a"
            events.append("truth-store")
            return SimpleNamespace(store_id=store_id)

    document = SimpleNamespace(id="doc-a")

    def _ensure(**kwargs: Any):
        events.append("ensure")
        assert kwargs["execution"] == selected
        return document_agent.DocumentAgentStatus(
            status="running",
            alive=True,
            started=True,
            error=None,
        )

    monkeypatch.setattr(conversation_store, "get_conversation", _get_conversation)
    monkeypatch.setattr(lifecycle_lock, "document_lifecycle_lock", _lifecycle_guard)
    monkeypatch.setattr(truth_registry, "TruthStoreRegistry", _Registry)
    monkeypatch.setattr(
        truth_documents,
        "get_document",
        lambda _store, _document_id: (
            events.append("truth-document") or document
        ),
    )
    monkeypatch.setattr(
        truth_documents,
        "current_lifecycle",
        lambda _store, _document_id: (
            events.append("truth-lifecycle") or "active"
        ),
    )
    monkeypatch.setattr(
        policy,
        "document_surface_allowed",
        lambda _store, _document: events.append("truth-policy") or True,
    )
    monkeypatch.setattr(document_agent, "ensure_document_agent", _ensure)

    status = document_agent.ensure_bound_document_agent(
        conversation.conversation_id
    )

    assert status is not None and status.status == "running"
    assert events == [
        "conversations",
        "lifecycle-enter",
        "truth-store",
        "truth-document",
        "truth-lifecycle",
        "truth-policy",
        "conversations",
        "ensure",
        "lifecycle-exit",
    ]


def test_bound_wake_ignores_general_purpose_conversation(
    document_agent_store,
) -> None:
    del document_agent_store
    conversation = conversation_store.create_conversation(
        title="General chat",
        source="house_agent",
    )

    assert (
        document_agent.ensure_bound_document_agent(conversation.conversation_id)
        is None
    )


def test_ensure_spawns_once_and_reuses_live_persisted_generation(
    document_agent_store,
) -> None:
    conversation = _conversation()
    spawns: list[dict[str, Any]] = []
    registrations: list[tuple[str, int]] = []

    def _spawn(**kwargs):
        spawns.append(dict(kwargs))
        return {"status": "ok", "pid": 43210}

    first = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43210,
        register_agent=lambda conversation_id, pid: registrations.append(
            (conversation_id, pid)
        ),
        spawn_agent=_spawn,
    )
    second = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43210,
        register_agent=lambda *_args: pytest.fail("live driver must not re-register"),
        spawn_agent=lambda **_kwargs: pytest.fail("live driver must not respawn"),
    )

    assert first == document_agent.DocumentAgentStatus(
        status="running",
        alive=True,
        started=True,
        error=None,
    )
    assert second.status == "running"
    assert second.alive is True
    assert second.started is False
    assert len(spawns) == 1
    assert spawns[0]["consumer"] == "cowork-document:store-a:doc-a"
    assert spawns[0]["generation"]
    assert registrations == [(conversation.conversation_id, 43210)]


def test_concurrent_ensure_has_one_spawn_owner(document_agent_store) -> None:
    conversation = _conversation()
    spawns: list[str] = []

    def _spawn(**kwargs):
        spawns.append(str(kwargs["generation"]))
        return {"status": "ok", "pid": 43333}

    def _ensure():
        return document_agent.ensure_document_agent(
            store_id="store-a",
            document_id="doc-a",
            conversation_id=conversation.conversation_id,
            process_alive=lambda pid: pid == 43333,
            register_agent=lambda *_args: None,
            spawn_agent=_spawn,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _index: _ensure(), range(2)))

    assert len(spawns) == 1
    assert all(status.status == "running" for status in statuses)
    assert sorted(status.started for status in statuses) == [False, True]


def test_starting_generation_can_receive_before_parent_activation(
    document_agent_store,
) -> None:
    from work_buddy.mcp_server import op_registry

    op_registry.load_builtin_ops()
    send = op_registry.get_op("op.wb.conversation_send")
    assert send is not None
    conversation = _conversation()
    turn = conversation_store.post_user_message(
        conversation.conversation_id,
        "Turn available during process startup.",
    )
    assert turn is not None
    observed: dict[str, Any] = {}

    def _spawn(**kwargs):
        observed["delivery"] = conversation_store.receive_user_message(
            kwargs["conversation_id"],
            kwargs["consumer"],
            kwargs["generation"],
        )
        observed["lease_during_spawn"] = conversation_store.get_agent_lease(
            kwargs["conversation_id"],
            kwargs["consumer"],
        )
        observed["send"] = send(
            kwargs["conversation_id"],
            "Handled during startup.",
            f"cowork-reply-{turn.message_id}",
            kwargs["consumer"],
            kwargs["generation"],
        )
        return {"status": "ok", "pid": 43444}

    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43444,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert observed["delivery"]["status"] == "message"
    assert observed["delivery"]["message"]["message_id"] == turn.message_id
    assert observed["lease_during_spawn"]["status"] == "starting"
    assert observed["send"]["created"] is True
    assert status.status == "running"
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    activated = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert activated is not None
    assert activated["status"] == "running"


def test_starting_generation_empty_long_poll_stays_owned_until_activation(
    document_agent_store,
) -> None:
    conversation = _conversation()
    observed: dict[str, Any] = {}

    def _spawn(**kwargs):
        observed["delivery"] = conversation_store.receive_user_message(
            kwargs["conversation_id"],
            kwargs["consumer"],
            kwargs["generation"],
            timeout_seconds=0.03,
            poll_interval_seconds=0.01,
        )
        return {"status": "ok", "pid": 43445}

    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 43445,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert observed["delivery"]["status"] == "timeout"
    assert status.status == "running"


def test_spawn_failure_is_persisted_and_raw_error_is_not_exposed(
    document_agent_store,
) -> None:
    conversation = _conversation()
    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        spawn_agent=lambda **_kwargs: {
            "status": "error",
            "error": "C:\\secret\\internal-command --api-key raw-secret",
        },
    )
    assert status.status == "spawn_failed"
    assert status.alive is False
    assert status.started is False
    assert status.error == "Chat couldn’t start. Try again."
    assert "secret" not in status.error

    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    persisted = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert persisted is not None
    assert persisted["status"] == "spawn_failed"
    assert persisted["error"] == status.error


def test_dead_pid_retry_rotates_generation_immediately(
    document_agent_store,
) -> None:
    conversation = _conversation()
    spawns: list[dict[str, Any]] = []

    def _spawn(**kwargs):
        spawns.append(dict(kwargs))
        return {"status": "ok", "pid": 50000 + len(spawns)}

    first = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 50001,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert first.status == "running"

    second = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda pid: pid == 50002,
        register_agent=lambda *_args: None,
        spawn_agent=_spawn,
    )
    assert second.status == "running"
    assert len(spawns) == 2
    assert spawns[0]["generation"] != spawns[1]["generation"]


def test_live_pid_has_headroom_for_long_in_flight_work(
    document_agent_store,
) -> None:
    conversation = _conversation()
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    generation = "generation-long-work"
    claim = conversation_store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claim is not None and claim["claimed"] is True
    assert conversation_store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        60123,
    )
    lease = conversation_store.get_agent_lease(
        conversation.conversation_id,
        consumer,
    )
    assert lease is not None
    heartbeat = datetime.fromisoformat(str(lease["heartbeat_at"]))

    working = document_agent.inspect_document_agent(
        conversation.conversation_id,
        consumer=consumer,
        process_alive=lambda _pid: True,
        now=heartbeat + timedelta(minutes=10),
    )
    stale = document_agent.inspect_document_agent(
        conversation.conversation_id,
        consumer=consumer,
        process_alive=lambda _pid: True,
        now=heartbeat + timedelta(minutes=16),
    )
    assert working.status == "running"
    assert working.alive is True
    assert stale.status == "stopped"
    assert stale.alive is False


def test_stale_live_generation_is_terminated_before_retry(
    document_agent_store,
    monkeypatch,
) -> None:
    conversation = _conversation()
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    generation = "generation-stale"
    claimed = conversation_store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claimed is not None and claimed["claimed"] is True
    assert conversation_store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        61001,
    )
    stale_at = (datetime.now().astimezone() - timedelta(minutes=20)).isoformat()
    conn = conversation_store.get_connection()
    try:
        conn.execute(
            """UPDATE conversation_agent_leases
               SET started_at = ?, heartbeat_at = ?, updated_at = ?
               WHERE conversation_id = ? AND consumer = ?""",
            (
                stale_at,
                stale_at,
                stale_at,
                conversation.conversation_id,
                consumer,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    terminated: list[int] = []
    monkeypatch.setattr(
        document_agent,
        "_terminate_spawned_driver",
        lambda pid, **_kwargs: terminated.append(pid),
    )
    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda _pid: True,
        register_agent=lambda *_args: None,
        spawn_agent=lambda **_kwargs: {"status": "ok", "pid": 61002},
    )

    assert status.status == "running"
    assert terminated == [61001]


def test_activation_failure_terminates_newly_spawned_process(
    document_agent_store,
    monkeypatch,
) -> None:
    conversation = _conversation()
    terminated: list[int] = []
    monkeypatch.setattr(
        document_agent,
        "activate_agent_lease",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        document_agent,
        "_terminate_spawned_driver",
        lambda pid, **_kwargs: terminated.append(pid),
    )

    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=lambda _pid: True,
        register_agent=lambda *_args: None,
        spawn_agent=lambda **_kwargs: {"status": "ok", "pid": 62001},
    )

    assert status.status == "stopped"
    assert terminated == [62001]


def test_process_check_error_fences_and_terminates_before_retry(
    document_agent_store,
    monkeypatch,
) -> None:
    conversation = _conversation()
    consumer = document_agent.document_agent_consumer("store-a", "doc-a")
    generation = "generation-check-error"
    claimed = conversation_store.claim_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
    )
    assert claimed is not None and claimed["claimed"] is True
    assert conversation_store.activate_agent_lease(
        conversation.conversation_id,
        consumer,
        generation,
        63001,
    )
    checks = 0

    def process_alive(_pid: int) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise OSError("transient process lookup failure")
        return True

    terminated: list[int] = []
    monkeypatch.setattr(
        document_agent,
        "_terminate_spawned_driver",
        lambda pid, **_kwargs: terminated.append(pid),
    )
    status = document_agent.ensure_document_agent(
        store_id="store-a",
        document_id="doc-a",
        conversation_id=conversation.conversation_id,
        process_alive=process_alive,
        register_agent=lambda *_args: None,
        spawn_agent=lambda **_kwargs: {"status": "ok", "pid": 63002},
    )

    assert status.status == "running"
    assert terminated == [63001]


def test_spawn_session_uses_explicit_model_and_authorized_executor(
    document_agent_store,
    monkeypatch,
) -> None:
    observed: dict[str, Any] = {}

    from work_buddy.agent_execution.models import (
        AgentExecutionSelection,
        AgentSpawnOutcome,
    )
    from work_buddy.agent_execution import registry

    selection = AgentExecutionSelection(
        provider_id="claude-code",
        model_id="configured-model",
        provider_label="Claude Code",
        model_label="Configured model",
    )

    def _start_detached(request):
        observed["request"] = request
        return AgentSpawnOutcome(
            status="ok",
            selection=request.selection,
            pid=70123,
        )

    monkeypatch.setattr(registry, "start_detached", _start_detached)
    result = document_agent.spawn_document_agent_session(
        store_id="store-a",
        document_id="doc-a",
        conversation_id="conversation-a",
        consumer="cowork-document:store-a:doc-a",
        generation="generation-a",
        producer_model="configured-model",
        execution=selection,
        history_loader=lambda _conversation_id: pytest.fail(
            "a source-free launch must not read conversation history"
        ),
    )
    assert result["status"] == "ok"
    assert result["pid"] == 70123
    request = observed["request"]
    assert request.name.startswith("cowork-doc-a-conversation-a")
    assert "producer_model: configured-model" in request.prompt
    assert request.max_budget_usd == 2.0
    assert request.selection == selection
    assert request.session_id == "generation-a-cowork"


def test_restore_fence_blocks_document_agent_before_lease_or_transport(
    document_agent_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del document_agent_store
    from work_buddy.backups.source_foundation_restore import (
        SourceFoundationRestorePending,
        write_restore_fence,
    )

    conversation = _conversation()
    marker = tmp_path / "restore" / "source_foundation_restore_pending.json"
    monkeypatch.setattr(
        "work_buddy.backups.source_foundation_restore.restore_fence_path",
        lambda: marker,
    )
    write_restore_fence({"snapshot_id": "document-agent-restore"}, path=marker)

    with pytest.raises(SourceFoundationRestorePending) as exc_info:
        document_agent.ensure_document_agent(
            store_id="store-a",
            document_id="doc-a",
            conversation_id=conversation.conversation_id,
            spawn_agent=lambda **_kwargs: pytest.fail(
                "provider transport must not run while restore is fenced"
            ),
        )

    assert exc_info.value.operation == "cowork.document_agent.ensure"
    assert conversation_store.get_agent_lease(
        conversation.conversation_id,
        document_agent.document_agent_consumer("store-a", "doc-a"),
    ) is None
