"""Cross-database lifecycle races for document conversation creation."""

from __future__ import annotations

import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pytest

from work_buddy.conversations import store as conversation_store
from work_buddy.conversations import execution as conversation_execution
from work_buddy.cowork import (
    api,
    bootstrap,
    conversations,
    lifecycle_lock,
    retirement,
    retirement_api,
    sitting_api,
    sitting_lifecycle,
)
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.identity import sha256_bytes

from .conftest import AGENT, HUMAN


def _ready(store_ctx, *, path: str, key: str):
    store = store_ctx["store"]
    source = b"# Lifecycle race\n\nOriginal sentence.\n"
    intent, _ = bootstrap.prepare_bootstrap(
        store,
        metadata={
            "mode": "create",
            "path": path,
            "title": "Lifecycle race",
            "initial_source_sha256": sha256_bytes(source),
            "idempotency_key": key,
        },
        source=source,
        actor=HUMAN,
    )
    snapshot = b"YDOC:" + source
    receipt = bootstrap.commit_bootstrap(
        store,
        bootstrap_id=intent.id,
        snapshot=snapshot,
        source_sha256=intent.source_sha256,
        snapshot_sha256=sha256_bytes(snapshot),
        ydoc_schema=bootstrap.YDOC_SCHEMA,
        actor=HUMAN,
    )
    return documents.get_document(store, receipt["document_id"])


def _prepare_retirement(store, document, *, key: str):
    intent, _ = retirement.prepare_retirement(
        store,
        document_id=document.id,
        actor=HUMAN,
        idempotency_key=key,
    )
    return intent


def _prepare_redirect_sitting(
    client,
    store,
    document,
    *,
    key: str,
):
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=document.ydoc_snapshot_sha256,
    )
    proposal = proposals.propose_edit(
        store,
        document_id=document.id,
        base_content_sha256=document.content_sha256,
        base_structured_head_sha256=head,
        selector=CompositeSelector(exact="Original sentence."),
        quote_exact="Original sentence.",
        replacement="Replacement sentence.",
        rationale="Exercise routing delivery.",
        tldr="Route this proposal.",
        actor=AGENT,
    )
    body = {
        "items": [
            {
                "proposal_id": proposal.id,
                "verb": "redirect",
                "canonical_sha256": proposal.canonical_sha256,
                "redirect_note": "Try a smaller change.",
            }
        ],
        "expected_file_sha256": document.content_sha256,
        "expected_ydoc_head_sha256": head,
        "idempotency_key": key,
    }
    response = client.post(
        f"/api/truth/doc/{document.id}/sitting/prepare"
        f"?store_id={store.store_id}",
        json=body,
    )
    assert response.status_code == 201
    return proposal, body, response.get_json()["intent_id"]


def _conversation_counts() -> dict[str, int]:
    conn = conversation_store.get_connection()
    try:
        return {
            "conversations": int(
                conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            ),
            "messages": int(
                conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            ),
            "leases": int(
                conn.execute(
                    "SELECT COUNT(*) FROM conversation_agent_leases"
                ).fetchone()[0]
            ),
        }
    finally:
        conn.close()


def _retirement_wins_race(
    *,
    app,
    store,
    document,
    intent,
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable,
    crossed: threading.Event,
):
    """Pause retirement after Truth commit while it still owns lifecycle lock."""

    retired_before_close = threading.Event()
    release_retirement = threading.Event()
    operation_attempted_lock = threading.Event()
    operation_thread = threading.local()
    original_stop = retirement_api._stop_document_conversation
    original_lock = lifecycle_lock.document_lifecycle_lock

    def _blocking_stop(store_id: str, document_id: str) -> None:
        retired_before_close.set()
        if not release_retirement.wait(timeout=10):
            raise AssertionError("test did not release retirement")
        original_stop(store_id, document_id)

    @contextlib.contextmanager
    def _observed_lock(store_id: str, document_id: str, **kwargs):
        if getattr(operation_thread, "active", False):
            operation_attempted_lock.set()
        with original_lock(store_id, document_id, **kwargs):
            yield

    monkeypatch.setattr(retirement_api, "_stop_document_conversation", _blocking_stop)
    monkeypatch.setattr(lifecycle_lock, "document_lifecycle_lock", _observed_lock)

    retire_url = (
        f"/api/truth/doc/{document.id}/retire?store_id={store.store_id}"
    )

    def _retire():
        with app.test_client() as thread_client:
            return thread_client.post(
                retire_url,
                json={"intent_id": intent.id},
                headers={"X-WB-User-Ref": HUMAN.ref},
            )

    def _operate():
        operation_thread.active = True
        try:
            with app.test_client() as thread_client:
                return operation(thread_client)
        finally:
            operation_thread.active = False

    with ThreadPoolExecutor(max_workers=2) as pool:
        retire_future = pool.submit(_retire)
        if not retired_before_close.wait(timeout=10):
            if retire_future.done():
                failed_response = retire_future.result(timeout=1)
                raise AssertionError(
                    "retirement did not reach conversation close: "
                    f"{failed_response.status_code} {failed_response.get_json()}"
                )
            raise AssertionError("retirement did not reach conversation close")
        assert documents.current_lifecycle(store, document.id) == "retired"
        operation_future = pool.submit(_operate)
        assert operation_attempted_lock.wait(timeout=10)
        # The operation has reached the lifecycle boundary but cannot enter its
        # Truth/conversations work while retirement owns that boundary.
        assert not crossed.is_set()
        release_retirement.set()
        retire_response = retire_future.result(timeout=10)
        operation_response = operation_future.result(timeout=10)
    return retire_response, operation_response


@pytest.mark.parametrize("kind", ["start", "feedback"])
def test_retirement_wins_before_start_or_feedback_creates_any_artifact(
    store_ctx,
    client,
    fake_document_agent,
    monkeypatch,
    kind,
):
    app = client.application
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path=f"docs/{kind}-retirement-race.md",
        key=f"{kind}-retirement-race-bootstrap-0001",
    )
    intent = _prepare_retirement(
        store,
        document,
        key=f"{kind}-retirement-race-0001",
    )
    assert conversations.find_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    ) is None
    before_conversations = _conversation_counts()
    with store._read_connection() as conn:
        before_truth = {
            "spans": int(
                conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0]
            ),
            "evidence": int(
                conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            ),
        }

    crossed = threading.Event()
    original_resolve = api._resolve_document

    def _observed_resolve(target_store, document_id):
        crossed.set()
        return original_resolve(target_store, document_id)

    monkeypatch.setattr(api, "_resolve_document", _observed_resolve)

    if kind == "start":
        def _operation(thread_client):
            return thread_client.post(
                f"/api/truth/doc/{document.id}/conversation"
                f"?store_id={store.store_id}"
            )
    else:
        def _operation(thread_client):
            return thread_client.post(
                f"/api/truth/doc/{document.id}/feedback"
                f"?store_id={store.store_id}",
                json={
                    "span": {
                        "exact": "Original sentence.",
                        "prefix": "",
                        "suffix": "",
                    },
                    "text": "This must never land after retirement.",
                },
            )

    retire_response, operation_response = _retirement_wins_race(
        app=app,
        store=store,
        document=document,
        intent=intent,
        monkeypatch=monkeypatch,
        operation=_operation,
        crossed=crossed,
    )
    assert retire_response.status_code == 200
    assert operation_response.status_code == 409
    expected_code = "document_retired" if kind == "feedback" else None
    if expected_code is not None:
        assert operation_response.get_json()["error"]["code"] == expected_code
    assert conversations.find_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    ) is None
    assert _conversation_counts() == before_conversations
    with store._read_connection() as conn:
        assert (
            int(conn.execute("SELECT COUNT(*) FROM document_spans").fetchone()[0])
            == before_truth["spans"]
        )
        assert (
            int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
            == before_truth["evidence"]
        )
    assert fake_document_agent == []


def test_retirement_wins_before_sitting_routing_can_create_a_binding(
    store_ctx,
    client,
    fake_document_agent,
    monkeypatch,
):
    app = client.application
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-retirement-race.md",
        key="routing-retirement-race-bootstrap-0001",
    )
    _proposal, _body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-retirement-race-sitting-0001",
    )
    intent = _prepare_retirement(
        store,
        document,
        key="routing-retirement-race-0001",
    )
    assert conversations.find_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    ) is None
    before_conversations = _conversation_counts()

    crossed = threading.Event()
    original_commit = sitting_lifecycle.commit_sitting

    def _observed_commit(*args, **kwargs):
        crossed.set()
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(sitting_api.sitting_lifecycle, "commit_sitting", _observed_commit)

    def _operation(thread_client):
        return thread_client.put(
            f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
            f"?store_id={store.store_id}",
            json={},
        )

    retire_response, operation_response = _retirement_wins_race(
        app=app,
        store=store,
        document=document,
        intent=intent,
        monkeypatch=monkeypatch,
        operation=_operation,
        crossed=crossed,
    )
    assert retire_response.status_code == 200
    assert operation_response.status_code == 409
    assert operation_response.get_json()["error"]["code"] == "document_retired"
    assert conversations.find_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    ) is None
    assert _conversation_counts() == before_conversations
    assert fake_document_agent == []


def test_routing_commit_reports_durable_delivery_agent_status_and_reconciles_prepare(
    store_ctx,
    client,
    fake_document_agent,
):
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-delivery-success.md",
        key="routing-delivery-success-bootstrap-0001",
    )
    proposal, prepare_body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-delivery-success-sitting-0001",
    )
    commit = client.put(
        f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
        f"?store_id={store.store_id}",
        json={},
    )
    assert commit.status_code == 200
    payload = commit.get_json()
    delivery = payload["routing_deliveries"][0]
    assert delivery == {
        "delivery_id": delivery["delivery_id"],
        "verb": "redirect",
        "proposal_id": proposal.id,
        "note": "Try a smaller change.",
        "delivered": True,
        "conversation_id": delivery["conversation_id"],
        "message_id": delivery["message_id"],
        "reason": None,
        "agent": {
            "status": "running",
            "alive": True,
            "started": True,
            "error": None,
        },
        "execution": delivery["execution"],
    }
    assert delivery["execution"]["selection"] == {
        "provider_id": "claude-code",
        "model_id": "sonnet",
        "provider_label": "Claude Code",
        "model_label": "Sonnet",
        "revision": delivery["execution"]["selection"]["revision"],
        "schema_version": 1,
        "persisted": True,
    }
    assert delivery["delivery_id"]
    assert delivery["conversation_id"]
    assert delivery["message_id"] == delivery["delivery_id"]
    assert len(fake_document_agent) == 1
    assert (
        fake_document_agent[0]["conversation_id"]
        == delivery["conversation_id"]
    )
    assert fake_document_agent[0]["execution"].provider_id == "claude-code"
    assert fake_document_agent[0]["execution"].model_id == "sonnet"

    # A lost commit response is recovered through repeated prepare. The
    # deterministic delivery id is replayed, not duplicated, and its real
    # delivery/agent result is returned again.
    recovered = client.post(
        f"/api/truth/doc/{document.id}/sitting/prepare"
        f"?store_id={store.store_id}",
        json=prepare_body,
    )
    assert recovered.status_code == 200
    recovered_payload = recovered.get_json()
    assert recovered_payload["state"] == "committed"
    replay = recovered_payload["result"]["routing_deliveries"][0]
    assert replay["delivered"] is True
    assert replay["conversation_id"] == delivery["conversation_id"]
    assert replay["message_id"] == delivery["message_id"]
    assert replay["agent"]["status"] == "running"
    assert replay["execution"]["selection"] == delivery["execution"]["selection"]
    conn = conversation_store.get_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE message_id = ?",
                (delivery["delivery_id"],),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_routing_commit_preserves_the_conversation_pinned_execution(
    store_ctx,
    client,
    fake_document_agent,
):
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-delivery-codex.md",
        key="routing-delivery-codex-bootstrap-0001",
    )
    binding = conversations.ensure_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    )
    pinned = conversation_execution.set_execution(
        binding.conversation_id,
        {
            "provider_id": "codex",
            "model_id": "gpt-5.6",
            "provider_label": "Codex",
            "model_label": "GPT-5.6",
        },
    )
    _proposal, _prepare_body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-delivery-codex-sitting-0001",
    )

    response = client.put(
        f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
        f"?store_id={store.store_id}",
        json={},
    )

    assert response.status_code == 200
    delivery = response.get_json()["routing_deliveries"][0]
    assert delivery["execution"]["selection"]["provider_id"] == "codex"
    assert delivery["execution"]["selection"]["model_id"] == "gpt-5.6"
    assert delivery["execution"]["selection"]["revision"] == pinned.revision
    assert len(fake_document_agent) == 1
    assert fake_document_agent[0]["execution"].provider_id == "codex"
    assert fake_document_agent[0]["execution"].model_id == "gpt-5.6"


def test_routing_write_failure_is_truthful_without_undoing_sitting(
    store_ctx,
    client,
    fake_document_agent,
    monkeypatch,
):
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-delivery-failure.md",
        key="routing-delivery-failure-bootstrap-0001",
    )
    proposal, _prepare_body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-delivery-failure-sitting-0001",
    )

    def _fail_delivery(**_kwargs):
        raise OSError("throwaway conversation write failure")

    monkeypatch.setattr(conversations, "deliver_decision", _fail_delivery)
    response = client.put(
        f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
        f"?store_id={store.store_id}",
        json={},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["result"] == "kept_open_redirected"
    delivery = payload["routing_deliveries"][0]
    assert delivery["proposal_id"] == proposal.id
    assert delivery["delivered"] is False
    assert delivery["conversation_id"] is None
    assert delivery["message_id"] is None
    assert delivery["reason"] == "Couldn’t add this to chat. Try again."
    assert "agent" not in delivery
    assert fake_document_agent == []


def test_routing_spawn_failure_keeps_delivery_and_returns_safe_agent_status(
    store_ctx,
    client,
    monkeypatch,
):
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-spawn-failure.md",
        key="routing-spawn-failure-bootstrap-0001",
    )
    _proposal, _prepare_body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-spawn-failure-sitting-0001",
    )

    def _fail_spawn(**_kwargs):
        raise RuntimeError("C:\\private\\launcher --token raw-secret")

    monkeypatch.setattr(
        sitting_api.document_agent,
        "ensure_document_agent",
        _fail_spawn,
    )
    response = client.put(
        f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
        f"?store_id={store.store_id}",
        json={},
    )
    assert response.status_code == 200
    delivery = response.get_json()["routing_deliveries"][0]
    assert delivery["delivered"] is True
    assert delivery["conversation_id"]
    assert delivery["message_id"] == delivery["delivery_id"]
    assert delivery["reason"] is None
    assert delivery["agent"] == {
        "status": "spawn_failed",
        "alive": False,
        "started": False,
        "error": "Chat couldn’t start. Try again.",
    }
    assert "secret" not in delivery["agent"]["error"]


def test_routing_commit_keeps_delivery_when_saved_execution_is_corrupt(
    store_ctx,
    client,
    fake_document_agent,
):
    store = store_ctx["store"]
    document = _ready(
        store_ctx,
        path="docs/routing-corrupt-execution.md",
        key="routing-corrupt-execution-bootstrap-0001",
    )
    binding = conversations.ensure_document_conversation(
        document_id=document.id,
        store_id=store.store_id,
    )
    proposal, _prepare_body, sitting_id = _prepare_redirect_sitting(
        client,
        store,
        document,
        key="routing-corrupt-execution-sitting-0001",
    )
    conn = conversation_store.get_connection()
    try:
        row = conn.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (binding.conversation_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(row["metadata"])
        metadata[conversation_execution.EXECUTION_METADATA_KEY] = {
            "schema_version": True,
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
            "revision": "invalid-bool-schema",
        }
        conn.execute(
            "UPDATE conversations SET metadata = ? WHERE conversation_id = ?",
            (json.dumps(metadata), binding.conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.put(
        f"/api/truth/doc/{document.id}/sitting/{sitting_id}/commit"
        f"?store_id={store.store_id}",
        json={},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["result"] == "kept_open_redirected"
    delivery = payload["routing_deliveries"][0]
    assert delivery["proposal_id"] == proposal.id
    assert delivery["delivered"] is True
    assert delivery["conversation_id"] == binding.conversation_id
    assert delivery["message_id"] == delivery["delivery_id"]
    assert delivery["agent"]["status"] == "spawn_failed"
    assert delivery["execution"]["read_only"] is True
    assert delivery["execution"]["error"]["code"] == (
        "execution_selection_corrupt"
    )
    assert delivery["execution"]["selection"]["model_id"] == (
        "saved-selection-unreadable"
    )
    assert fake_document_agent == []
    conn = conversation_store.get_connection()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE message_id = ?",
                (delivery["delivery_id"],),
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
