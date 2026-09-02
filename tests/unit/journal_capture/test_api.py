from __future__ import annotations

import hashlib
import shutil
from types import SimpleNamespace

import pytest
from flask import Flask

from work_buddy.cowork import api as cowork_api
from work_buddy.dashboard import local_identity_api
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.document_kernel.domain_service import (
    DomainContentStoreManager,
    RunningNoteDocumentService,
)
from work_buddy.document_kernel.redaction_dispatch import (
    CoworkDocumentSourceDispatcher,
)
from work_buddy.journal_capture import api as journal_api
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.configuration import JournalProfileConfigurationService
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.ingress import JournalIngressQueued
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.projection import view_snapshot
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.security.local_identity import (
    DEFAULT_AUDIENCE,
    SESSION_COOKIE_NAME,
    BoundaryRequest,
    LocalIdentityAuthority,
)
from work_buddy.settings import get_journal_day_window
from work_buddy.sources import SourceOutbox, redact_source
from work_buddy.sources.models import ActorRef, SourceRef
from work_buddy.sources.resolve import resolve_source
from work_buddy.sources.store import SourceStore
from work_buddy.truth import documents as truth_documents
from work_buddy.truth.registry import TruthStoreRegistry
from work_buddy.cowork.truth_activation import (
    WORKING_DOCUMENT_CONTRACT,
    resolve_document_truth_policy,
)


ORIGIN = "http://127.0.0.1:5127"


def _boundary() -> BoundaryRequest:
    return BoundaryRequest(
        remote_addr="127.0.0.1",
        scheme="http",
        host="127.0.0.1:5127",
        origin=ORIGIN,
    )


def _session(authority: LocalIdentityAuthority):
    bootstrap = authority.mint_bootstrap(origin=ORIGIN, audience=DEFAULT_AUDIENCE)
    return authority.redeem_bootstrap(
        token=bootstrap.token,
        boundary=_boundary(),
        audience=DEFAULT_AUDIENCE,
    )


def _day_id() -> str:
    window = get_journal_day_window("2026-08-09")
    return f"journal-day:2026-08-09:{window.timezone}:{window.boundary}"


def _write(_rel, abs_path, content, **_kw):
    # Match the production bridge's exact UTF-8 replacement semantics.  Using
    # Path.write_text(newline=None) on Windows would translate existing CRLF a
    # second time and manufacture CRCRLF marker boundaries.
    abs_path.write_bytes(content.encode("utf-8"))
    return True


def test_restart_reconciles_mixed_dependency_before_redaction_consumers(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    authority.enrolled_actor()
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(
        store,
        JournalContentAdapter(tmp_path / "vault"),
    )
    events: list[str] = []

    def reconcile(**_kwargs):
        events.append("dependency")
        return ()

    class JournalDispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def drain(self):
            events.append("journal-redaction")
            return SimpleNamespace(delivered=0, failed=0, deferred=0)

    class DocumentDispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def drain(self):
            events.append("document-redaction")
            return SimpleNamespace(completed=0, failed=0, deferred=0)

    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", False)
    monkeypatch.setattr(journal_api, "reconcile_journal_documents", reconcile)
    monkeypatch.setattr(journal_api, "JournalSourceDispatcher", JournalDispatcher)
    monkeypatch.setattr(
        journal_api,
        "CoworkDocumentSourceDispatcher",
        DocumentDispatcher,
    )

    assert journal_api._services() == (sources, store, service)
    assert events == ["dependency", "journal-redaction", "document-redaction"]


@pytest.mark.parametrize("mode", ["database_only", "recovery_fenced"])
def test_restart_skips_legacy_document_reconciliation_after_authority_retirement(
    tmp_path, monkeypatch, mode
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    authority.enrolled_actor()
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    with store.transaction() as conn:
        if mode == "database_only":
            conn.execute(
                "UPDATE journal_authority_control SET mode='database_only' "
                "WHERE singleton=1"
            )
        else:
            conn.execute(
                "UPDATE journal_authority_control SET mode='recovery_fenced',"
                "prior_mode='legacy_compatibility',fence_code='test',"
                "fenced_at='2026-08-27T00:00:00+00:00' WHERE singleton=1"
            )
        conn.execute(
            "UPDATE journal_domain_state SET value=? WHERE key='content_authority'",
            (mode,),
        )

    class Dispatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def drain(self):
            return SimpleNamespace(
                delivered=0, failed=0, deferred=0, completed=0
            )

    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", False)
    monkeypatch.setattr(
        journal_api,
        "reconcile_journal_documents",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy Journal reconciliation ran")
        ),
    )
    monkeypatch.setattr(journal_api, "JournalSourceDispatcher", Dispatcher)
    monkeypatch.setattr(journal_api, "CoworkDocumentSourceDispatcher", Dispatcher)

    assert journal_api._services() == (sources, store, service)


def test_authenticated_capture_commits_exact_source_before_journal_effect(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    (vault / "journal" / "2026-08-09.md").write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))

    app = Flask("journal-capture-api")
    journal_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    body = {
        "client_mutation_id": "capture-mutation-1",
        "day_id": _day_id(),
        "target_id": "running_notes",
        "mode": "dumb",
        "exact_text": "  exact\ntext  ",
        "input_mode": "paste",
        "stated_at": "2026-08-09T15:15:00-04:00",
    }
    context_sha = journal_api._canonical_gesture_context(body)
    _, gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject="journal-capture:capture-mutation-1",
        context_sha256=context_sha,
    )

    response = client.post(
        "/api/journal/captures",
        json={**body, "actor": {"subject": "attacker-selected"}},
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": gesture.token,
            "X-WB-User-Ref": "attacker-selected",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 201, response.json
    assert response.json["persisted"] is True
    capture_id = response.json["capture"]["captureId"]
    capture = store.get_capture(capture_id)
    assert capture is not None
    enrolled = authority.enrolled_actor()
    service_principal = ActorRef(
        issuer_authority_id=enrolled.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled.tenant_scope_id,
    )
    resolved = resolve_source(
        sources,
        source_ref=SourceRef.parse(capture.source_ref),
        representation_id=capture.representation_id,
        principal=service_principal,
        purpose="journal.materialize",
    )
    assert resolved.content == b"  exact\ntext  "
    assert "attacker-selected" not in str(resolved.attributions)
    assert capture.entry_id is not None

    # A lost-response retry necessarily uses a fresh one-time gesture.  The
    # semantic mutation identity still deduplicates to the same source/capture.
    _, retry_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject="journal-capture:capture-mutation-1",
        context_sha256=context_sha,
    )
    retry = client.post(
        "/api/journal/captures",
        json=body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": retry_gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert retry.status_code == 200, retry.json
    assert retry.json["deduplicated"] is True
    assert retry.json["capture"]["captureId"] == capture_id
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1


def test_authenticated_capture_reports_durable_cutover_queue(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    service = JournalCaptureService(store, JournalContentAdapter(tmp_path / "vault"))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))

    def queued(_self, **_kwargs):
        raise JournalIngressQueued(SimpleNamespace(deduplicated=False))

    monkeypatch.setattr(
        "work_buddy.journal_capture.ingress.JournalCaptureIngress.submit",
        queued,
    )
    app = Flask("journal-capture-queued-api")
    journal_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    body = {
        "client_mutation_id": "capture-queued-mutation-1",
        "day_id": _day_id(),
        "target_id": "running_notes",
        "mode": "dumb",
        "exact_text": "queued exact text",
        "input_mode": "direct_entry",
    }
    context_sha = journal_api._canonical_gesture_context(body)
    _, gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject="journal-capture:capture-queued-mutation-1",
        context_sha256=context_sha,
    )
    response = client.post(
        "/api/journal/captures",
        json=body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 202
    assert response.json == {
        "ok": True,
        "persisted": True,
        "queued": True,
        "deduplicated": False,
        "capture": None,
        "message": (
            "The capture is saved and queued while Journal cutover "
            "maintenance finishes."
        ),
    }


def test_smart_retry_is_locked_before_source_or_model_work_during_postseal(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    calls: list[str] = []

    class Processor:
        def process(self, **_kwargs):
            calls.append("model")
            raise RuntimeError("synthetic provider failure")

    service = JournalCaptureService(
        store,
        JournalContentAdapter(tmp_path / "vault"),
        smart_processor=Processor(),
    )
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    app = Flask("journal-smart-retry-fence-api")
    journal_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    capture_body = {
        "client_mutation_id": "capture-smart-retry-1",
        "day_id": _day_id(),
        "target_id": "running_notes",
        "mode": "smart",
        "exact_text": "contextualize this entry",
        "input_mode": "direct_entry",
        "smart_disclosure_sha256": service.smart_disclosure_sha256,
    }
    capture_context = journal_api._canonical_gesture_context(capture_body)
    _, capture_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject="journal-capture:capture-smart-retry-1",
        context_sha256=capture_context,
    )
    captured = client.post(
        "/api/journal/captures",
        json=capture_body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": capture_gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert captured.status_code == 201
    assert calls == ["model"]
    capture_id = captured.json["capture"]["captureId"]
    capture = store.get_capture(capture_id)
    assert capture is not None

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO journal_import_cohorts("
            "cohort_id,client_mutation_id,request_sha256,inventory_sha256,"
            "parser_version,mapping_version,mapping_sha256,parse_report_sha256,"
            "state,state_revision,expected_file_count,expected_byte_count,"
            "expected_span_count,expected_item_count,actor_json,created_at,"
            "updated_at,verified_at,sealed_at,seal_sha256) "
            "VALUES('cohort-api-retry','cohort-api-retry-prepare',?,?,?,?,?,?,"
            "'sealed',1,0,0,0,0,'{}',?,?,?,?,?)",
            (
                "1" * 64,
                "e" * 64,
                "test-parser/v1",
                "test-mapping/v1",
                "2" * 64,
                "3" * 64,
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
                "4" * 64,
            ),
        )
        count, high_water = conn.execute(
            "SELECT COUNT(*),MAX(rowid) FROM journal_captures"
        ).fetchone()
        conn.execute(
            "UPDATE journal_cutover_gate SET state='paused',gate_revision=2,"
            "cohort_id='cohort-api-retry',request_sha256=?,capture_row_count=?,"
            "capture_row_high_water=?,entry_row_count=(SELECT COUNT(*) FROM journal_entries),"
            "entry_row_high_water=COALESCE((SELECT MAX(rowid) FROM journal_entries),0),"
            "paused_at=?,updated_at=? WHERE singleton=1",
            (
                "d" * 64,
                count,
                high_water,
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE cutover_maintenance SET state='postseal_pending',"
            "cohort_id='cohort-api-retry',inventory_sha256=?,fence_id='fence-api-retry',"
            "pause_request_sha256=?,paused_at=?,updated_at=? WHERE singleton=1",
            (
                "e" * 64,
                "d" * 64,
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only',"
            "activated_cohort_id='cohort-api-retry' WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )

    retry_body = {"smart_disclosure_sha256": service.smart_disclosure_sha256}

    def retry_gesture():
        context = (
            f"wb.journal-capture-retry/v1:{capture_id}:{capture.revision}:"
            f"{service.smart_disclosure_sha256}"
        )
        context_sha = hashlib.sha256(context.encode()).hexdigest()
        return authority.issue_gesture(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            boundary=_boundary(),
            action="journal.capture.retry",
            subject=f"journal-capture:{capture_id}",
            context_sha256=context_sha,
        )[1]

    blocked = client.post(
        f"/api/journal/captures/{capture_id}/retry",
        json=retry_body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": retry_gesture().token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert blocked.status_code == 423
    assert blocked.json["error"]["code"] == "journal_cutover_ingress_paused"
    assert calls == ["model"]

    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_cutover_gate SET state='open',gate_revision=3,"
            "cohort_id=NULL,request_sha256=NULL,capture_row_count=NULL,"
            "capture_row_high_water=NULL,entry_row_count=NULL,"
            "entry_row_high_water=NULL,paused_at=NULL,updated_at=? WHERE singleton=1",
            ("2026-08-27T00:01:00+00:00",),
        )
        conn.execute(
            "UPDATE cutover_maintenance SET state='open',released_at=?,updated_at=? "
            "WHERE singleton=1",
            (
                "2026-08-27T00:01:00+00:00",
                "2026-08-27T00:01:00+00:00",
            ),
        )
    retried = client.post(
        f"/api/journal/captures/{capture_id}/retry",
        json=retry_body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": retry_gesture().token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert retried.status_code == 200
    assert calls == ["model", "model"]


def test_direct_http_without_bound_gesture_cannot_mint_source(tmp_path, monkeypatch):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    app = Flask("journal-capture-no-gesture")
    journal_api.register_routes(app)

    response = app.test_client().post(
        "/api/journal/captures",
        json={
            "client_mutation_id": "capture-mutation-2",
            "day_id": _day_id(),
            "target_id": "log",
            "mode": "dumb",
            "exact_text": "must not persist",
            "input_mode": "direct_entry",
        },
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-User-Ref": "dashboard-user",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code in {401, 403}
    with sources.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_document_module_opens_shared_cowork_target_with_truth_disabled(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    vault = tmp_path / "vault"
    vault.mkdir()
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    kernel = DocumentKernelClient()
    manager = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    document_service = RunningNoteDocumentService(kernel=kernel, stores=manager)
    monkeypatch.setattr(journal_api, "_kernel", lambda: kernel)
    monkeypatch.setattr(
        journal_api,
        "RunningNoteDocumentService",
        lambda **_kwargs: document_service,
    )
    draft = {
        "profileId": "user.documents",
        "expectedRevision": 0,
        "name": "Document Journal",
        "description": "",
        "modules": [{
            "slotId": "reflection",
            "moduleInstanceId": "user.documents/reflection",
            "expectedVersion": 0,
            "moduleTypeId": "document",
            "moduleTypeVersion": 1,
            "label": "Daily reflection",
            "settings": {"documentRole": "daily_reflection"},
            "behaviorId": "provenance_only",
            "behaviorVersion": 1,
            "scheduleKind": "always",
            "schedule": {},
            "fields": [],
        }],
    }
    JournalProfileConfigurationService(store).save(
        draft,
        client_mutation_id="save-document-module-profile",
        actor={"subject": "person:test"},
    )
    domain = JournalDomainService(store)
    activation_revision = domain.activate_profile(
        profile_id="user.documents",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=1,
        client_mutation_id="activate-document-module-profile",
        actor={"subject": "person:test"},
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only' WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )

    app = Flask("journal-document-module")
    journal_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    unknown_body = {
        "clientMutationId": "open-unknown-journal-document-module",
        "moduleInstanceVersion": 1,
    }
    _, unknown_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.document.open",
        subject="journal-document:2026-08-28:user.documents.unknown",
        context_sha256=journal_api._profile_gesture_context(unknown_body),
    )
    unknown = client.post(
        "/api/journal/document-modules/2026-08-28/user.documents.unknown/open",
        json=unknown_body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": unknown_gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert unknown.status_code == 409, unknown.json
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_days WHERE local_date=?",
            ("2026-08-28",),
        ).fetchone()[0] == 0

    body = {
        "clientMutationId": "open-journal-document-module-0001",
        "moduleInstanceVersion": 1,
    }
    context_sha = journal_api._profile_gesture_context(body)
    _, gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.document.open",
        subject="journal-document:2026-08-27:user.documents/reflection",
        context_sha256=context_sha,
    )
    headers = {
        "Origin": ORIGIN,
        "Host": "127.0.0.1:5127",
        "X-WB-CSRF": session.csrf_token,
        "X-WB-Gesture": gesture.token,
    }
    opened = client.post(
        "/api/journal/document-modules/2026-08-27/user.documents%2Freflection/open",
        json=body,
        headers=headers,
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert opened.status_code == 201, opened.json
    reference = opened.json["document"]
    cowork_store = manager.open_existing(vault)
    policy = resolve_document_truth_policy(cowork_store, reference["documentId"])
    assert policy.interaction_contract_id == WORKING_DOCUMENT_CONTRACT
    assert policy.provenance_enabled is True
    assert policy.activation_state == "disabled"
    assert policy.truth_observable is False
    assert policy.truth_mutable is False
    with store._connect() as conn:
        day_row = conn.execute(
            "SELECT day_id FROM journal_days WHERE local_date=?",
            ("2026-08-27",),
        ).fetchone()
        assert day_row is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_day_composition_snapshots WHERE day_id=?",
            (day_row["day_id"],),
        ).fetchone()[0] == 1

    replacement = {
        "profileId": "user.documents.replacement",
        "expectedRevision": 0,
        "name": "Replacement Journal",
        "description": "",
        "modules": [],
    }
    JournalProfileConfigurationService(store).save(
        replacement,
        client_mutation_id="save-replacement-document-profile",
        actor={"subject": "person:test"},
    )
    domain.activate_profile(
        profile_id="user.documents.replacement",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=activation_revision,
        client_mutation_id="activate-replacement-document-profile",
        actor={"subject": "person:test"},
    )
    projected = view_snapshot(store, local_date="2026-08-27")
    assert projected["effectiveComposition"]["persisted"] is True
    assert projected["effectiveComposition"]["profile"]["profileId"] == "user.documents"
    module = projected["effectiveComposition"]["modules"][0]
    assert module["document"] == {
        "state": "current",
        "role": "daily_reflection",
        "truthEligibility": "allowed",
        "truthStartsDisabled": True,
        "href": reference["href"],
        "storeId": reference["storeId"],
        "documentId": reference["documentId"],
        "bindingId": reference["bindingId"],
        "domainEntityId": reference["domainEntityId"],
        "contentAuthorityEpoch": 1,
        "canOpenFull": True,
    }

    _, replay_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.document.open",
        subject="journal-document:2026-08-27:user.documents/reflection",
        context_sha256=context_sha,
    )
    replay = client.post(
        "/api/journal/document-modules/2026-08-27/user.documents%2Freflection/open",
        json=body,
        headers={**headers, "X-WB-Gesture": replay_gesture.token},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert replay.status_code == 200, replay.json
    assert replay.json["deduplicated"] is True
    assert replay.json["document"] == reference


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_running_note_open_in_cowork_is_exact_gesture_bound_and_idempotent(
    tmp_path, monkeypatch
):
    authority = LocalIdentityAuthority(tmp_path / "identity.db")
    monkeypatch.setattr(local_identity_api, "_authority", lambda: authority)
    sources = SourceStore.create(tmp_path / "sources")
    store = JournalCaptureStore(tmp_path / "journal.db")
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    (vault / "journal" / "2026-08-09.md").write_text(
        "# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", _write)
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))
    monkeypatch.setattr(journal_api, "_recovery_complete", True)
    kernel = DocumentKernelClient()
    manager = DomainContentStoreManager(
        root=tmp_path / "domain-content",
        registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    documents = RunningNoteDocumentService(kernel=kernel, stores=manager)
    monkeypatch.setattr(journal_api, "_kernel", lambda: kernel)
    monkeypatch.setattr(
        journal_api,
        "RunningNoteDocumentService",
        lambda **_kwargs: documents,
    )
    monkeypatch.setattr(cowork_api, "_registry", lambda: manager.registry)
    original_resolve = cowork_api.resolve
    monkeypatch.setattr(
        cowork_api,
        "resolve",
        lambda key: sources.paths.root
        if key == "stores/sources"
        else original_resolve(key),
    )

    app = Flask("journal-running-note-cowork")
    journal_api.register_routes(app)
    cowork_api.register_routes(app)
    client = app.test_client()
    session = _session(authority)
    client.set_cookie(SESSION_COOKIE_NAME, session.cookie_token, domain="127.0.0.1")
    capture_body = {
        "client_mutation_id": "capture-cowork-1",
        "day_id": _day_id(),
        "target_id": "running_notes",
        "mode": "dumb",
        "exact_text": "Exact Running Note café 🧭.",
        "input_mode": "direct_entry",
    }
    capture_context = journal_api._canonical_gesture_context(capture_body)
    _, capture_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.capture.submit",
        subject="journal-capture:capture-cowork-1",
        context_sha256=capture_context,
    )
    captured = client.post(
        "/api/journal/captures",
        json=capture_body,
        headers={
            "Origin": ORIGIN,
            "Host": "127.0.0.1:5127",
            "X-WB-CSRF": session.csrf_token,
            "X-WB-Gesture": capture_gesture.token,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert captured.status_code == 201, captured.json
    entry_id = captured.json["capture"]["entryId"]
    entry = store.get_entry(entry_id)
    assert entry is not None
    context_sha = journal_api.running_note_document_gesture_context(entry)
    _, open_gesture = authority.issue_gesture(
        cookie_token=session.cookie_token,
        csrf_token=session.csrf_token,
        boundary=_boundary(),
        action="journal.running_note.open_in_cowork",
        subject=f"journal-running-note:{entry_id}",
        context_sha256=context_sha,
    )
    open_headers = {
        "Origin": ORIGIN,
        "Host": "127.0.0.1:5127",
        "X-WB-CSRF": session.csrf_token,
        "X-WB-Gesture": open_gesture.token,
        "X-WB-User-Ref": "attacker-selected",
    }
    try:
        opened = client.post(
            f"/api/journal/running-notes/{entry_id}/open-in-cowork",
            json={"expected_version": entry.version, "actor": "attacker-selected"},
            headers=open_headers,
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert opened.status_code == 201, opened.json
        assert opened.json["coworkHref"].startswith("/app/cowork?")
        mirror = store.get_document_binding(entry_id)
        assert mirror is not None
        assert mirror.state == "current"
        assert "attacker-selected" not in str(mirror.inspection)
        inspected = client.get(
            f"/api/truth/doc/{mirror.document_id}/changes/{mirror.change_id}"
            f"?store_id={mirror.store_id}",
            headers={"Host": "127.0.0.1:5127"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert inspected.status_code == 200, inspected.json
        assert inspected.json["schema"] == "wb.cowork-document-change-inspection/v1"
        assert inspected.json["source"]["copy_relation"] == "exact_copy"
        assert inspected.json["source"]["source_role"] == "human_input"
        assert inspected.json["binding"]["domain_namespace"] == "journal"
        assert inspected.json["binding"]["domain_entity_id"] == entry_id
        assert inspected.json["assurance"]["persistence"] == "persistence_verified"
        assert inspected.json["actors"]["selected_by"] is not None
        projected = (vault / "journal" / "2026-08-09.md").read_text(encoding="utf-8")
        assert "wb:cowork-projection/v1" in projected
        assert "Exact Running Note café 🧭." in projected

        _, retry_gesture = authority.issue_gesture(
            cookie_token=session.cookie_token,
            csrf_token=session.csrf_token,
            boundary=_boundary(),
            action="journal.running_note.open_in_cowork",
            subject=f"journal-running-note:{entry_id}",
            context_sha256=context_sha,
        )
        retry = client.post(
            f"/api/journal/running-notes/{entry_id}/open-in-cowork",
            json={"expected_version": entry.version},
            headers={**open_headers, "X-WB-Gesture": retry_gesture.token},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert retry.status_code == 200, retry.json
        assert retry.json["deduplicated"] is True
        assert retry.json["coworkHref"] == opened.json["coworkHref"]

        # Redacting the retained source scrubs both managed readable copies:
        # the Journal entry/projection and the Co-work document.  Neither
        # consumer may report completion before its own durable removal path
        # succeeds.
        capture = store.get_capture(captured.json["capture"]["captureId"])
        assert capture is not None
        source_ref = SourceRef.parse(capture.source_ref)
        enrolled = authority.enrolled_actor()
        human = ActorRef.from_dict(enrolled.to_dict())
        service_principal = ActorRef(
            issuer_authority_id=enrolled.issuer_authority_id,
            subject="work-buddy-journal-service",
            kind="service",
            tenant_scope_id=enrolled.tenant_scope_id,
        )
        authorization = "d" * 64
        sources.grant_access(
            source_ref=source_ref,
            principal=human,
            purpose="redaction",
            access_mode="metadata",
            authorization_fingerprint=authorization,
        )
        redaction = redact_source(
            sources,
            source_ref=source_ref,
            actor=human,
            authorization_fingerprint=authorization,
            reason_code="user_requested",
        )
        assert redaction.managed_copy_state == "pending"
        assert len(redaction.pending_effect_ids) == 2

        journal_summary = JournalSourceDispatcher(
            sources,
            service,
            service_principal=service_principal,
            document_registry=manager.registry,
        ).drain()
        document_summary = CoworkDocumentSourceDispatcher(
            sources,
            store,
            service_principal=service_principal,
            registry=manager.registry,
            vault_root=vault,
        ).drain()
        assert journal_summary.delivered == 1
        assert document_summary.completed == 1
        assert all(
            SourceOutbox(sources).get(effect_id).status == "succeeded"
            for effect_id in redaction.pending_effect_ids
        )

        retired_mirror = store.get_document_binding(entry_id)
        assert retired_mirror is not None
        assert retired_mirror.state == "retired"
        cowork_store = manager.registry.open_store(retired_mirror.store_id)
        cowork_document = truth_documents.get_document(
            cowork_store, retired_mirror.document_id
        )
        projection = cowork_store.resolve_blob_path(
            f"blobs/{cowork_document.content_sha256}"
        ).read_bytes()
        # Markdown serialization escapes brackets so the tombstone remains
        # literal prose rather than a link label.
        assert projection == b"\\[redacted\\]\n"
        assert b"Exact Running Note" not in projection
        assert "Exact Running Note" not in (
            vault / "journal" / "2026-08-09.md"
        ).read_text(encoding="utf-8")

        completed = redact_source(
            sources,
            source_ref=source_ref,
            actor=human,
            authorization_fingerprint=authorization,
            reason_code="user_requested",
        )
        assert completed.managed_copy_state == "complete"
    finally:
        kernel.close()
