from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from flask import Flask

from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCaptureConflict,
    JournalSmartAvailability,
)
from work_buddy.journal_capture.projection import capture_view
from work_buddy.journal_capture.service import (
    CommittedIngress,
    JournalCaptureService,
    SmartCaptureResult,
    TaskProposalFollowUp,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import get_journal_day_window
from work_buddy.sources.store import SourceStore
from work_buddy.tasks.store import TaskStore
from work_buddy.threads.action_proposals import ActionProposalService


@pytest.fixture
def stack(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "journal").mkdir(parents=True)
    (vault / "journal" / "2026-08-25.md").write_bytes(
        b"# **Log**\n\n# **Running Notes / Considerations**\n\n% RUNNING END\n"
    )
    def write(_relative, path, content, **_kwargs):
        path.write_bytes(content.encode("utf-8"))
        return True
    monkeypatch.setattr("work_buddy.obsidian.vault_writer.vault_write", write)
    sources = SourceStore.create(tmp_path / "sources")
    exact = "  Remember to fix the parser\nKeep the original context.  "
    source = sources.capture_source(content=exact, source_role="human_input",
                                    tenant_scope_id="test", originating_surface="journal-test")
    store = JournalCaptureStore(tmp_path / "journal.db")
    tasks = TaskStore(tmp_path / "tasks.db")
    tasks.initialize()
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Capturing must never execute a task")
    proposals = ActionProposalService(db_path=tmp_path / "threads.db", executor=execute)
    window = get_journal_day_window("2026-08-25")
    kwargs = {"ingress": CommittedIngress(
        source_ref=source.source_ref.uri, representation_id=source.primary_representation_id,
        submission_id="submission", command_id="command", effect_id="source-effect",
        authorization_fingerprint="explicit-gesture",
    ), "client_mutation_id": "capture-one", "day_id": f"journal-day:2026-08-25:{window.timezone}:{window.boundary}",
        "exact_text": exact, "input_mode": "paste", "stated_at": None}
    return SimpleNamespace(sources=sources, source=source, exact=exact, store=store, tasks=tasks,
        calls=calls, proposals=proposals, kwargs=kwargs, vault=vault, thread_path=tmp_path / "threads.db")


def test_smart_source_is_saved_before_inference_and_one_proposal_creates_zero_tasks(stack):
    model_calls = []
    class Smart:
        def process(self, *, capture, exact_text):
            assert stack.sources.get_item(stack.source.source_ref) is not None
            assert stack.store.get_capture(capture.capture_id).persistence_status == "persisted"
            assert exact_text == stack.exact
            model_calls.append(exact_text)
            return SmartCaptureResult(target=CaptureTarget.LOG, summary="A task-like intention.", effects=(),
                follow_up=TaskProposalFollowUp(task_text="Fix the parser", rationale="Explicit open intention."))
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault),
        smart_processor=Smart(), proposal_service=stack.proposals)
    capture = service.accept(**stack.kwargs, target=CaptureTarget.AUTO, mode=CaptureMode.SMART, run_smart=True)
    assert capture.resolved_target is CaptureTarget.RUNNING_NOTES
    note = stack.store.get_entry(capture.entry_id)
    assert note.markdown == stack.exact and note.resolution_state == "open"
    follow_up = service.proposal_follow_ups(capture.capture_id)[0]
    assert follow_up["href"].startswith("/app/tasks?proposal=th-")
    assert stack.tasks.list() == [] and stack.calls == []
    service.process_smart(capture.capture_id, exact_text=stack.exact)
    assert model_calls == [stack.exact]
    with sqlite3.connect(stack.thread_path) as conn:
        assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    proposal = stack.proposals.get(follow_up["referenceId"])["proposal"]
    stack.proposals.reject(proposal["thread_id"], client_mutation_id="reject-1",
                           expected_proposal_event_id=proposal["proposal_event_id"])
    assert "dismissed" in service.proposal_follow_ups(capture.capture_id)[0]["description"]
    service.reconcile_proposals()
    assert stack.store.get_entry(capture.entry_id).resolution_state == "open"
    assert stack.store.proposal_resolution_effects() == []


def test_model_free_proposal_replays_after_thread_commit_and_never_runs_smart(stack):
    class CrashOnce:
        attempts = 0
        def create_task_proposal(self, **kwargs):
            self.attempts += 1
            result = stack.proposals.create_task_proposal(**kwargs)
            if self.attempts == 1:
                raise RuntimeError("uncertain cross-store delivery")
            return result
        def get(self, thread_id):
            return stack.proposals.get(thread_id)
    delivery = CrashOnce()
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault), proposal_service=delivery)
    assert not service.smart_processing_available
    capture = service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB, follow_up_action="task_proposal")
    assert capture.processing_status.value == "not_requested"
    assert stack.store.get_entry(capture.entry_id).markdown == stack.exact
    assert service.proposal_follow_ups(capture.capture_id)[0]["status"] == "failed"
    projected = capture_view(stack.store, capture, follow_ups=service.proposal_follow_ups(capture.capture_id))
    assert projected["persistenceStatus"] == "persisted" and projected["retryable"] is True
    service.reconcile_proposals()
    first = service.proposal_follow_ups(capture.capture_id)[0]
    service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB, follow_up_action="task_proposal")
    assert service.proposal_follow_ups(capture.capture_id)[0] == first
    with sqlite3.connect(stack.thread_path) as conn:
        assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    assert stack.tasks.list() == [] and stack.calls == []
    assert delivery.attempts == 2
    with pytest.raises(JournalCaptureConflict):
        service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES, mode=CaptureMode.DUMB)


@pytest.mark.parametrize("cancel_at", ["before_ingress", "after_success", "after_failure"])
def test_source_cancellation_cannot_be_reopened_by_an_inflight_delivery(stack, monkeypatch, cancel_at):
    attempts = []

    def cancel():
        stack.store.pause_source_proposals(
            source_effect_id=stack.kwargs["ingress"].effect_id,
            source_ref=stack.source.source_ref.uri,
        )

    class Delivery:
        def create_task_proposal(self, **kwargs):
            attempts.append(kwargs["client_mutation_id"])
            if cancel_at == "after_failure":
                cancel()
                raise RuntimeError("delivery failed while source removal settled")
            result = stack.proposals.create_task_proposal(**kwargs)
            # A committed Thread is an independent retained derivative. Its
            # late reply still must not revive Journal's now-canceled command.
            cancel()
            return result

        def get(self, thread_id):
            return stack.proposals.get(thread_id)

    if cancel_at == "before_ingress":
        lease = stack.store.lease_effect

        def lease_then_cancel(*args, **kwargs):
            result = lease(*args, **kwargs)
            cancel()
            return result

        monkeypatch.setattr(stack.store, "lease_effect", lease_then_cancel)

    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault), proposal_service=Delivery())
    capture = service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB, follow_up_action="task_proposal")
    effect = next(item for item in stack.store.effects_for_capture(capture.capture_id)
                  if item.effect_type == "task_proposal")
    assert effect.state.value == "paused"
    assert effect.payload is None and effect.result is None
    assert effect.error_code == "journal_proposal_source_withdrawn"
    follow_ups = service.proposal_follow_ups(capture.capture_id)
    assert "canceled" in follow_ups[0]["label"]
    assert capture_view(stack.store, capture, follow_ups=follow_ups)["retryable"] is False
    service.deliver_proposal(capture.capture_id)
    service.reconcile_proposals()
    assert len(attempts) == (0 if cancel_at == "before_ingress" else 1)
    assert stack.tasks.list() == [] and stack.calls == []
    assert stack.store.get_entry(capture.entry_id).resolution_state == "open"
    with pytest.raises(JournalCaptureConflict, match="removed"):
        stack.store.reauthorize_effect(capture.capture_id, "task_proposal",
            authorization_fingerprint="new-gesture", authorization_expires_at=None)
    if cancel_at == "after_success":
        with sqlite3.connect(stack.thread_path) as conn:
            assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    else:
        assert not stack.thread_path.exists()


def test_settled_smart_outbox_survives_restart_without_a_second_model_call(stack):
    class Smart:
        def process(self, **_kwargs):
            return SmartCaptureResult(CaptureTarget.RUNNING_NOTES, "Saved intention", (),
                follow_up=TaskProposalFollowUp("Fix parser", "Retain this intention."))
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault), smart_processor=Smart())
    capture = service.accept(**stack.kwargs, target=CaptureTarget.AUTO, mode=CaptureMode.SMART, run_smart=True)
    assert capture.processing_status.value == "succeeded"
    restarted = JournalCaptureService(JournalCaptureStore(stack.store.path), JournalContentAdapter(stack.vault),
                                     proposal_service=stack.proposals)
    replay = restarted.process_smart(capture.capture_id, exact_text=stack.exact)
    assert replay.processing_status.value == "succeeded"
    assert restarted.proposal_follow_ups(capture.capture_id)[0]["kind"] == "app_link"
    assert stack.tasks.list() == []


def test_only_realization_changes_the_running_note_resolution(stack, monkeypatch):
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault), proposal_service=stack.proposals)
    capture = service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.DUMB, follow_up_action="task_proposal")
    assert stack.store.get_entry(capture.entry_id).resolution_state == "open"
    class RealizedProjection:
        def get(self, thread_id):
            return {"ok": True, "proposal": {"thread_id": thread_id, "status": "realized", "realization": {
                "task_id": "t-0123abcd", "receipt_id": "receipt", "task_revision": 1,
            }}}
    service.proposal_service = RealizedProjection()
    original_version = stack.store.get_entry(capture.entry_id).version
    assert service.proposal_follow_ups(capture.capture_id)[0]["href"] == "/app/tasks?task=t-0123abcd"
    from work_buddy.journal_capture import api as journal_api
    monkeypatch.setattr(journal_api, "_services", lambda: (stack.sources, stack.store, service))
    app = Flask("journal-proposal-read-purity")
    journal_api.register_routes(app)
    response = app.test_client().get(f"/api/journal/captures/{capture.capture_id}")
    assert response.status_code == 200
    assert response.json["capture"]["followUps"][0]["href"] == "/app/tasks?task=t-0123abcd"
    assert stack.store.get_entry(capture.entry_id).resolution_state == "open"
    assert stack.store.get_entry(capture.entry_id).version == original_version
    service.reconcile_proposals()
    assert stack.store.get_entry(capture.entry_id).resolution_state == "routed_to_task"
    assert stack.store.get_entry(capture.entry_id).version == original_version + 1
    service.reconcile_proposals()
    assert stack.store.get_entry(capture.entry_id).version == original_version + 1
    assert stack.store.proposal_resolution_effects() == []


def test_proposal_maintenance_filters_unrelated_effects_and_bounds_work(stack, monkeypatch):
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault))
    unrelated = service.accept(**stack.kwargs, target=CaptureTarget.AUTO, mode=CaptureMode.SMART)
    assert stack.store.pending_effects(limit=1)[0].effect_type == "auto_route"
    kwargs = {**stack.kwargs, "client_mutation_id": "explicit-two",
              "ingress": replace(stack.kwargs["ingress"], effect_id="other-effect", command_id="other-command", submission_id="other-submission")}
    capture = service.accept(**kwargs, target=CaptureTarget.RUNNING_NOTES,
                             mode=CaptureMode.DUMB, follow_up_action="task_proposal")
    service.proposal_service = stack.proposals
    from work_buddy.journal_capture import api as journal_api
    monkeypatch.setattr(journal_api, "_services", lambda: (stack.sources, stack.store, service))
    app = Flask("journal-proposal-no-get-ingress")
    journal_api.register_routes(app)
    response = app.test_client().get(f"/api/journal/captures/{capture.capture_id}")
    assert response.json["capture"]["followUps"][0]["status"] == "failed"
    report = service.reconcile_proposals(limit=1)
    assert report == {"delivery_checked": 1, "resolution_checked": 1, "resolution_synced": 0}
    assert service.proposal_follow_ups(capture.capture_id)[0]["kind"] == "app_link"
    assert stack.store.get_capture(unrelated.capture_id).processing_status.value == "pending"
    with sqlite3.connect(stack.thread_path) as conn:
        assert conn.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    for limit in (0, -1, 101, True, "1"):
        with pytest.raises(ValueError, match="between 1 and 100"):
            service.reconcile_proposals(limit=limit)


def test_changed_or_missing_model_disclosure_never_sends_the_saved_source(stack):
    class Smart:
        def __init__(self):
            self.inputs: list[str] = []

        def process(self, *, capture, exact_text):
            self.inputs.append(exact_text)
            return SmartCaptureResult(CaptureTarget.RUNNING_NOTES, "Intention", ())
    processor = Smart()
    availability = JournalSmartAvailability(state="ready", code="ready", provider="test", model="new-model")
    service = JournalCaptureService(stack.store, JournalContentAdapter(stack.vault),
        smart_processor=processor, smart_availability=availability,
        smart_configuration=lambda: (processor, availability))
    capture = service.accept(**stack.kwargs, target=CaptureTarget.RUNNING_NOTES,
        mode=CaptureMode.SMART, run_smart=True, smart_disclosure_sha256="0" * 64)
    assert capture.persistence_status == "persisted"
    assert stack.store.get_entry(capture.entry_id).markdown == stack.exact
    assert capture.processing_error_code == "smart_disclosure_changed"
    assert processor.inputs == []
    stack.store.bind_smart_disclosure(capture.capture_id, service.smart_disclosure_sha256, retry=True)
    stack.store.reauthorize_effect(capture.capture_id, "smart_annotate", authorization_fingerprint="new-gesture", authorization_expires_at=None)
    service.process_smart(capture.capture_id, exact_text=stack.exact)
    assert processor.inputs == [stack.exact]
