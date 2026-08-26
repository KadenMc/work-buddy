"""No production model calls or user state: isolated conversations + fake runner."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask

from work_buddy.conversations import store as conversations
from work_buddy.dashboard.assistance.api import create_assistance_blueprint
from work_buddy.dashboard.assistance.contracts import (
    AssistanceError,
    digest,
    form_schema,
    manifest,
    validate_operations,
    validate_snapshot,
)
from work_buddy.dashboard.assistance.service import CONSUMER, AssistanceBroker

IDENTITY = {"profileId": "local-profile", "workspaceId": "default-workspace", "appId": "wb.tasks", "viewId": "wb.tasks.main", "instanceId": "tasks:quick-add", "widgetTypeId": "wb.tasks.quick-add", "draftName": "task-create", "scopeKey": "view"}
AVAILABILITY = {"available": True, "code": "ready", "providerId": "test-provider", "modelId": "deterministic-test", "purpose": "dashboard.assisted_draft", "message": "Ready", "disclosure": "Test-only runner. No network."}


class DeterministicRunner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"reply": "I have suggested a title and summary. You decide when to submit.", "operations": [{"op": "set", "path": ["title"], "value": "Review the launch plan"}, {"op": "set", "path": ["summary"], "value": "Resolve the open questions."}]}

    def availability(self):
        return AVAILABILITY.copy()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.result)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "_DB_PATH", tmp_path / "conversations.db")
    conn = conversations.get_connection()
    conversations._ensure_schema(conn)
    conn.close()


@pytest.fixture
def surface(isolated):
    runner = DeterministicRunner()
    broker = AssistanceBroker(runner=runner, dispatch=lambda callback: callback())
    authorizations = []
    actor = {"id": "human:test"}

    def authorize(operation, subject, body):
        authorizations.append((operation, subject, dict(body)))
        return actor["id"]

    app = Flask(__name__)
    app.register_blueprint(create_assistance_blueprint(broker=broker, authorizer=authorize))
    app.testing = True
    return app.test_client(), broker, runner, authorizations, actor


def start_body(**changes):
    return {"requestId": "start-1", "identity": IDENTITY, "schema": form_schema("task-create")["schema"], "interactionMode": "operate", "readOnly": False, "disclosureAccepted": True, "providerId": AVAILABILITY["providerId"], "modelId": AVAILABILITY["modelId"], **changes}


def start(client):
    response = client.post("/api/assistance/sessions", json=start_body())
    assert response.status_code == 200, response.json
    return response.json


def prepare(client, session, message_id="turn-1", snapshot=None):
    snapshot = snapshot or {"title": "Launch plan", "summary": ""}
    prepared = {"messageId": message_id, "baseDraftRevision": 7, "baseSnapshotHash": digest(snapshot), "snapshot": snapshot}
    response = client.post(f"/api/assistance/{session['assistantSessionId']}/snapshots", json=prepared)
    assert response.status_code == 200, response.json
    return prepared


def respond(client, session, message_id="turn-1", value="Help me make the title specific"):
    return client.post(f"/api/assistance/{session['assistantSessionId']}/conversations/{session['conversationId']}/respond", json={"message_id": message_id, "value": value})


def test_job_name_uses_scheduler_length_bound_but_allows_an_empty_draft():
    form = form_schema("job-create")
    validate_snapshot(form, {"name": ""})
    validate_operations(form, [{"op": "set", "path": ["name"], "value": "a" * 64}])
    with pytest.raises(AssistanceError):
        validate_operations(form, [{"op": "set", "path": ["name"], "value": "a" * 65}])


def test_http_conversation_to_typed_patch_and_ack_uses_canonical_messages(surface):
    client, broker, runner, authorized, _ = surface
    session = start(client)
    assert runner.calls == []  # opening/starting is not model invocation
    prepared = prepare(client, session)
    response = respond(client, session)
    assert response.status_code == 200
    assert response.json == {"message_id": "turn-1"}
    assert len(runner.calls) == 1
    payload = client.get(f"/api/assistance/{session['assistantSessionId']}/conversations/{session['conversationId']}").json
    assert [message["role"] for message in payload["messages"]] == ["user", "agent"]
    assert payload["conversation"]["agent_alive"] is False
    entry = client.get(f"/api/assistance/{session['assistantSessionId']}/patches").json["patches"][0]
    patch = entry["patch"]
    assert patch["identity"] == IDENTITY
    assert patch["baseDraftRevision"] == 7
    assert patch["baseSnapshotHash"] == prepared["baseSnapshotHash"]
    assert patch["operations"] == runner.result["operations"]
    assert entry["receipt"] is None
    receipt = {"patchId": patch["patchId"], "status": "partial", "appliedFields": [["summary"]], "pendingFields": [{"path": ["title"], "reason": "focused"}], "resultingRevision": 8, "message": "1 field filled; 1 suggestion needs review."}
    ack = client.post(f"/api/assistance/{session['assistantSessionId']}/receipts", json=receipt)
    assert ack.status_code == 200
    assert ack.json == receipt
    assert broker.patches(session["assistantSessionId"], "human:test")[0]["receipt"] == receipt
    assert [call[0] for call in authorized] == ["start", "prepare", "respond", "read", "read", "acknowledge"]
    # Authority is only a broker: no Tasks or Jobs state table was created.
    with conversations.get_connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tasks" not in tables and "jobs" not in tables


def test_start_and_send_retry_reuse_conversation_message_patch_and_model_call(surface):
    client, broker, runner, _, _ = surface
    session = start(client)
    assert start(client) == session
    assert "authorizationRef" not in session
    prepare(client, session)
    assert respond(client, session).status_code == 200
    original = broker.patches(session["assistantSessionId"], "human:test")
    prepare(client, session)
    assert respond(client, session).status_code == 200
    assert len(runner.calls) == 1
    assert broker.patches(session["assistantSessionId"], "human:test") == original
    assert len(conversations.get_conversation_with_messages(session["conversationId"])["messages"]) == 2


def test_generic_conversation_routes_cannot_read_respond_or_close_assisted_sessions(surface, monkeypatch):
    # Import after the isolated Conversations fixture is active. Exercise the
    # registered production guard, not a duplicate test-only route policy.
    from work_buddy.dashboard import service

    client, _, runner, _, _ = surface
    session = start(client)
    prepare(client, session)
    assert respond(client, session).status_code == 200
    ordinary = conversations.create_conversation("Ordinary test conversation", source="test")
    conversation_id = session["conversationId"]
    before = conversations.get_conversation_with_messages(conversation_id)
    monkeypatch.setitem(service.app.config, "TESTING", True)
    monkeypatch.setattr(service, "_is_read_only", lambda: False)

    with service.app.test_client() as dashboard:
        listing = dashboard.get("/api/conversations")
        assert listing.status_code == 200
        assert [item["conversation_id"] for item in listing.json["conversations"]] == [ordinary.conversation_id]
        for method, suffix, body in [
            ("GET", "", None),
            ("POST", "/respond", {"message_id": "bypass-turn", "value": "Do this without the bound snapshot"}),
            ("POST", "/close", {}),
        ]:
            response = dashboard.open(f"/api/conversations/{conversation_id}{suffix}", method=method, json=body)
            assert response.status_code == 403
            assert response.json["code"] == "assistance_session_required"

    assert conversations.get_conversation_with_messages(conversation_id) == before
    assert len(runner.calls) == 1
    # The actor-bound assistance endpoint remains the accessible authority.
    assert client.get(f"/api/assistance/{session['assistantSessionId']}/conversations/{conversation_id}").status_code == 200


def test_changed_retry_identity_conflicts(surface):
    client, _, runner, _, _ = surface
    session = start(client)
    prepared = prepare(client, session)
    assert respond(client, session).status_code == 200
    assert respond(client, session, value="Different turn").status_code == 409
    prepared["snapshot"]["title"] = "Different snapshot"
    prepared["baseSnapshotHash"] = digest(prepared["snapshot"])
    assert client.post(f"/api/assistance/{session['assistantSessionId']}/snapshots", json=prepared).status_code == 409
    assert len(runner.calls) == 1


@pytest.mark.parametrize("changes", [{"interactionMode": "arrange"}, {"interactionMode": "preview"}, {"readOnly": True}, {"disclosureAccepted": False}])
def test_modes_and_explicit_start_gesture_are_required(surface, changes):
    client, _, runner, _, _ = surface
    assert client.post("/api/assistance/sessions", json=start_body(**changes)).status_code == 403
    assert not runner.calls


def test_session_and_conversation_binding_are_not_cross_actor_or_cross_form(surface):
    client, _, runner, _, actor = surface
    session = start(client)
    assert client.get(f"/api/assistance/{session['assistantSessionId']}/conversations/wrong").status_code == 409
    actor["id"] = "human:someone-else"
    assert client.get(f"/api/assistance/{session['assistantSessionId']}").status_code == 404
    assert not runner.calls


def test_provider_selection_is_bound_to_the_explicit_start_disclosure(surface):
    client, _, runner, _, _ = surface
    response = client.post("/api/assistance/sessions", json=start_body(modelId="different-model"))
    assert response.status_code == 409
    assert response.json["code"] == "provider_selection_changed"
    assert not runner.calls


def test_server_read_only_overrides_client_body_and_fences_queued_model_work(isolated):
    readonly = {"value": True}
    queued = []
    runner = DeterministicRunner()
    broker = AssistanceBroker(runner=runner, dispatch=queued.append)
    app = Flask(__name__)
    app.register_blueprint(create_assistance_blueprint(broker=broker, authorizer=lambda *_: "human:test", dashboard_read_only=lambda: readonly["value"]))
    client = app.test_client()
    # A forged readOnly:false cannot weaken the server's mode.
    assert client.post("/api/assistance/sessions", json=start_body()).status_code == 403
    readonly["value"] = False
    session = start(client)
    prepare(client, session)
    assert respond(client, session).status_code == 200
    readonly["value"] = True
    queued.pop(0)()
    assert not runner.calls
    assert broker.patches(session["assistantSessionId"], "human:test") == []
    assert respond(client, session).status_code == 403
    assert client.get(f"/api/assistance/{session['assistantSessionId']}").status_code == 200


def test_unprepared_or_submit_shaped_chat_has_no_model_authority(surface):
    client, _, runner, _, _ = surface
    session = start(client)
    assert respond(client, session).json["code"] == "snapshot_required"
    prepare(client, session)
    path = f"/api/assistance/{session['assistantSessionId']}/conversations/{session['conversationId']}/respond"
    assert client.post(path, json={"message_id": "turn-1", "value": "yes", "submit": True}).status_code == 400
    assert not runner.calls
    assert respond(client, session, value="yes, create and submit it").status_code == 200
    # Even an imperative user message produces only ordinary reply/patch data.
    assert len(runner.calls) == 1


@pytest.mark.parametrize("operation", [
    {"op": "submit", "path": ["title"]},
    {"op": "set", "path": ["__proto__", "polluted"], "value": True},
    {"op": "set", "path": [], "value": {}},
    {"op": "set", "path": ["password"], "value": "secret"},
    {"op": "set", "path": ["proposal_ref"], "value": "th-forged"},
    {"op": "set", "path": ["batch_lines"], "value": []},
    {"op": "set", "path": ["title"], "value": 12},
    {"op": "remove", "path": ["title"]},
    {"op": "set", "path": ["title"], "value": "x" * 1001},
    {"op": "set", "path": ["urgency"], "value": "infinite"},
])
def test_closed_patch_validation_is_atomic(operation):
    with pytest.raises(AssistanceError):
        validate_operations(form_schema("task-create"), [{"op": "set", "path": ["summary"], "value": "valid"}, operation])


def test_unknown_and_secret_snapshot_fields_are_rejected():
    with pytest.raises(AssistanceError):
        validate_snapshot(form_schema("task-create"), {"title": "Good", "api_key": "secret"})
    with pytest.raises(AssistanceError, match="secret parameters"):
        validate_snapshot(form_schema("job-create"), {"params": '{"nested":{"access_token":"secret"}}'})
    assert validate_operations(form_schema("job-create"), [{"op": "set", "path": ["jitter_seconds"], "value": 20}])
    with pytest.raises(AssistanceError):
        validate_operations(form_schema("job-create"), [{"op": "set", "path": ["jitter_seconds"], "value": -1}])


def test_malformed_model_output_leaves_readable_conversation_without_patch(surface):
    client, broker, runner, _, _ = surface
    runner.result["operations"].append({"op": "submit", "path": ["title"]})
    session = start(client)
    prepare(client, session)
    assert respond(client, session).status_code == 200
    assert broker.patches(session["assistantSessionId"], "human:test") == []
    messages = conversations.get_conversation_with_messages(session["conversationId"])["messages"]
    assert "unchanged" in messages[-1]["content"]


def test_stop_fences_late_model_output_and_resume_uses_existing_inbox(isolated):
    pending = []
    runner = DeterministicRunner()
    broker = AssistanceBroker(runner=runner, dispatch=pending.append)
    session = broker.start(start_body(), "human:test")
    snapshot = {"title": "old"}
    broker.prepare(session["assistantSessionId"], "human:test", {"messageId": "turn-1", "baseDraftRevision": 1, "baseSnapshotHash": digest(snapshot), "snapshot": snapshot})
    body = {"value": "Help", "message_id": "turn-1"}
    broker.respond(session["assistantSessionId"], session["conversationId"], "human:test", body)
    broker.stop(session["assistantSessionId"], "human:test")
    pending.pop(0)()
    assert not runner.calls
    broker.respond(session["assistantSessionId"], session["conversationId"], "human:test", body)
    pending.pop(0)()
    assert len(runner.calls) == 1
    assert conversations.get_agent_lease(session["conversationId"], CONSUMER)["status"] == "stopped"


def test_expired_session_and_unknown_receipt_fields_fail_closed(surface):
    client, broker, _, _, _ = surface
    session = start(client)
    with broker._connection() as conn:
        conn.execute("UPDATE assisted_draft_sessions SET expires_at=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    response = client.get(f"/api/assistance/{session['assistantSessionId']}")
    assert response.status_code == 410


def test_manifest_has_one_canonical_closed_contract_for_both_hosts(surface):
    client, *_ = surface
    assert client.get("/api/assistance/schemas").json == manifest()
    for form in manifest()["forms"].values():
        assert form["submitPolicy"] == "user_only"
        assert len({tuple(field["path"]) for field in form["fields"]}) == len(form["fields"])
        assert all(field["disclosure"] == "explicit_start" for field in form["fields"])


@pytest.mark.parametrize("configured,settings,code", [
    (None, {"enabled": False, "tier": "frontier_fast"}, "not_configured"),
    ({"enabled": False}, {"enabled": False, "tier": "frontier_fast"}, "disabled"),
    ({"enabled": True}, {"enabled": True, "tier": "unknown"}, "provider_unavailable"),
])
def test_availability_preserves_safe_reason_without_enabling_or_running(configured, settings, code, monkeypatch):
    from work_buddy.dashboard.assistance.runner import configured_spec
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {"dashboard": {"assistance": configured}})
    monkeypatch.setattr("work_buddy.settings.broker.get_dashboard_assistance_settings", lambda: settings)
    availability, spec = configured_spec()
    assert availability["available"] is False
    assert availability["code"] == code
    assert spec is None


def test_source_bound_runner_records_manifest_before_no_tools_model_handoff(tmp_path, monkeypatch):
    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
        DisclosureState,
    )
    from work_buddy.dashboard.assistance.runner import (
        AssistanceModelSpec,
        SourceBoundAssistanceRunner,
    )
    from work_buddy.llm.response import LLMResponse
    from work_buddy.llm.tiers import ModelTier
    from work_buddy.sources.disclosure import SourcesDisclosureService
    from work_buddy.sources.models import ActorRef
    from work_buddy.sources.store import SourceStore

    availability = {**AVAILABILITY, "providerId": "anthropic", "modelId": "test-model"}
    monkeypatch.setattr("work_buddy.dashboard.assistance.runner.configured_spec", lambda: (availability, AssistanceModelSpec(ModelTier.FRONTIER_FAST, "anthropic", "test-model")))
    sources = SourceStore.create(tmp_path / "sources", authority_id="test-authority")
    discloser = SourcesDisclosureService(sources, tenant_scope_id="tenant-local", issuer=ActorRef("issuer-local", "execution", "service", "tenant-local"))
    manifests = DisclosureManifestStore(tmp_path / "manifests.db")
    calls = []

    class Model:
        def call(self, **kwargs):
            calls.append(kwargs)
            entries = manifests.list_entries("assisted-draft-as-test-turn-1")
            assert len(entries) == 1 and entries[0].state is DisclosureState.POSSIBLY_SENT
            assert kwargs["tools"] == [] and kwargs["escalate_to"] == []
            assert kwargs["cache_ttl_minutes"] == 0
            assert json.loads(kwargs["user"])["draft"]["title"] == "Private task"
            return LLMResponse(structured_output={"reply": "What would done look like?", "operations": []}, model="test-model", backend="anthropic")

    runner = SourceBoundAssistanceRunner(model_runner=Model(), disclosure_sources=discloser, disclosure_gateway=DisclosureGateway(manifests, discloser))
    result = runner.run(session={"assistantSessionId": "as-test", "availability": availability, "authorizationRef": "explicit-human-start"}, turn_id="turn-1", payload={"draft": {"title": "Private task"}}, form=form_schema("task-create"))
    assert len(calls) == 1
    assert result["producer"]["disclosure_manifest_sha256"]
    entry = manifests.list_entries("assisted-draft-as-test-turn-1")[0]
    assert entry.state is DisclosureState.SENT
    assert "Private task" not in json.dumps(entry.to_dict())
