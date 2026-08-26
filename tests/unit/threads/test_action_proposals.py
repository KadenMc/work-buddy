from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from types import SimpleNamespace

import pytest

from work_buddy.threads import engine, store
from work_buddy.threads.action_proposals import ActionProposalService, ProposalError
from work_buddy.threads.events import (
    KIND_ACTION_EXECUTION_INTENT,
    KIND_ACTION_INFERRED,
    KIND_ACTION_REALIZED,
    KIND_ACTION_RECOVERY_CHECKED,
    KIND_EXECUTION_FINISHED,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import Task, Thread


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from work_buddy.tasks import events, runtime
    from work_buddy.tasks import store as task_store_module
    from work_buddy.tasks.documents import TaskDocumentService, TaskKnowledgeDocument
    from work_buddy.tasks.store import TaskStore

    thread_path, task_path = tmp_path / "threads.db", tmp_path / "tasks.db"
    monkeypatch.setattr(store, "_db_path", lambda: thread_path)
    monkeypatch.setattr(task_store_module, "default_task_db_path", lambda: task_path)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: task_path)
    monkeypatch.setattr(
        runtime, "_canonical_default_latch_path", lambda: tmp_path / "task-latch.json"
    )
    monkeypatch.setattr(events, "publish_pending_async", lambda *_args: None)
    documents = []

    def document_create(self, **kwargs):
        documents.append(kwargs)
        return TaskKnowledgeDocument(
            task_id=kwargs["task_id"],
            store_id="isolated-store",
            document_id="isolated-document",
            binding_id="isolated-binding",
            title=kwargs["title"],
            lifecycle="active",
        )

    monkeypatch.setattr(TaskDocumentService, "create", document_create)
    tasks = TaskStore(task_path)
    tasks.initialize()
    old = tasks.system_state()
    runtime.arm_native_authority_latch(
        task_path,
        cohort_id="proposal-test",
        target_authority_epoch="native:proposal-test",
        cutover_receipt_id="proposal-test-cutover",
        armed_at="2026-08-25T12:00:00+00:00",
    )
    tasks.set_system_state(
        expected_authority_epoch=old.authority_epoch,
        authority_epoch="native:proposal-test",
        updated_at="2026-08-25T12:00:00+00:00",
        cutover_receipt_id="proposal-test-cutover",
        process_generation=1,
    )
    calls = []

    def execute(**parameters):
        calls.append(parameters)
        return Task.create(**parameters)

    # Initialize once before concurrent workers, just as application bootstrap.
    store.get_connection().close()
    service = ActionProposalService(db_path=thread_path, executor=execute)
    return SimpleNamespace(
        service=service,
        tasks=tasks,
        calls=calls,
        documents=documents,
        execute=execute,
        thread_path=thread_path,
    )


def create(stack, *, mutation_id="capture-1", **parameters):
    return stack.service.create_task_proposal(
        client_mutation_id=mutation_id,
        parameters={"task_text": "Write the proposal tests", **parameters},
        origin={"kind": "journal_capture", "id": "capture-1", "source_ref": "source-1"},
    )["proposal"]


def accept(stack, proposal, *, mutation_id="accept-1"):
    return stack.service.accept(
        proposal["thread_id"],
        client_mutation_id=mutation_id,
        expected_proposal_event_id=proposal["proposal_event_id"],
        actor="user:owner",
    )["proposal"]


def table_count(table):
    with closing(store.get_connection()) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_ingress_is_hash_bound_and_creates_only_one_thread_zero_tasks(stack):
    first = create(stack)
    replay = create(stack)
    assert first == replay
    assert first["status"] == "ready"
    assert first["href"] == f"/app/tasks?proposal={first['thread_id']}"
    assert first["realization"] is None
    assert table_count("threads") == 1
    assert table_count("thread_proposal_mutations") == 1
    assert stack.tasks.list() == []
    assert stack.calls == []
    with pytest.raises(ProposalError, match="different request") as error:
        create(stack, task_text="A different request with the same key")
    assert error.value.code == "proposal_idempotency_conflict"
    assert table_count("threads") == 1


def test_duplicate_ingress_is_atomic_across_connections(stack):
    barrier = Barrier(2)

    def ingress(_):
        barrier.wait(timeout=5)
        return create(stack)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(ingress, range(2)))
    assert first["thread_id"] == second["thread_id"]
    assert table_count("threads") == 1
    assert table_count("thread_proposal_mutations") == 1


def test_creation_failure_rolls_back_thread_events_and_receipt(stack, monkeypatch):
    original = stack.service._record_receipt

    def fail_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulate interrupted ingress")

    monkeypatch.setattr(stack.service, "_record_receipt", fail_receipt)
    with pytest.raises(RuntimeError):
        create(stack)
    assert table_count("threads") == 0
    assert table_count("thread_events") == 0
    assert table_count("thread_proposal_mutations") == 0


def test_explicit_transaction_suppresses_inner_commits_including_search(stack):
    thread = Thread()
    with pytest.raises(RuntimeError), store.transaction() as conn:
        store.insert_thread(thread, conn=conn)
        event = store.append_event(
            ThreadEvent(
                thread_id=thread.thread_id,
                kind=KIND_THREAD_CREATED,
                actor="user",
            ),
            conn=conn,
        )
        store.update_thread_state(
            thread.thread_id, fsm_state="done", parent_event_id=event.id, conn=conn
        )
        with closing(store.get_connection()) as other:
            assert store.get_thread(thread.thread_id, conn=other) is None
        raise RuntimeError("rollback")
    assert store.get_thread(thread.thread_id) is None
    assert table_count("thread_events") == 0
    # Supplied connections outside the new transaction preserve legacy behavior.
    with closing(store.get_connection()) as conn:
        store.insert_thread(thread, conn=conn)
        assert store.get_thread(thread.thread_id) is not None


def test_revision_receipt_replays_and_stale_decisions_fail(stack):
    first = create(stack)
    body = {
        "client_mutation_id": "revise-1",
        "expected_proposal_event_id": first["proposal_event_id"],
        "parameters": {
            "task_text": "Revised intention",
            "tags": ["projects/work-buddy"],
        },
    }
    revised = stack.service.revise(first["thread_id"], **body)
    assert revised["proposal"]["proposal_event_id"] != first["proposal_event_id"]
    assert stack.service.revise(first["thread_id"], **body)["replayed"] is True
    for method in (stack.service.accept, stack.service.reject):
        with pytest.raises(ProposalError) as error:
            method(
                first["thread_id"],
                client_mutation_id=f"stale-{method.__name__}",
                expected_proposal_event_id=first["proposal_event_id"],
            )
        assert error.value.code == "proposal_revision_conflict"
    with pytest.raises(ProposalError) as error:
        stack.service.revise(
            first["thread_id"], **{**body, "client_mutation_id": "stale-revise"}
        )
    assert error.value.code == "proposal_revision_conflict"
    assert stack.tasks.list() == []


@pytest.mark.parametrize("version", [None, True, 0, -1, "1"])
def test_decisions_require_a_real_reviewed_event_version(stack, version):
    proposal = create(stack)
    with pytest.raises(ProposalError) as error:
        stack.service.accept(
            proposal["thread_id"],
            client_mutation_id="accept",
            expected_proposal_event_id=version,
        )
    assert error.value.code == "proposal_version_required"
    assert stack.calls == []


def test_rejection_preserves_proposal_and_never_creates_a_task(stack):
    proposal = create(stack)
    rejected = stack.service.reject(
        proposal["thread_id"],
        client_mutation_id="reject-1",
        expected_proposal_event_id=proposal["proposal_event_id"],
    )
    assert rejected["proposal"]["status"] == "rejected"
    assert stack.service.get(proposal["thread_id"])["proposal"]["status"] == "rejected"
    assert (
        stack.service.reconcile(proposal["thread_id"])["proposal"]["status"]
        == "rejected"
    )
    with pytest.raises(ProposalError) as error:
        accept(stack, proposal)
    assert error.value.code == "proposal_rejected"
    assert stack.tasks.list() == []
    assert stack.calls == []


def test_double_accept_replays_the_same_structured_realization(stack):
    proposal = create(stack)
    first = accept(stack, proposal)
    duplicate = accept(stack, proposal, mutation_id="accept-another-tab")
    assert first["status"] == "realized"
    assert first["realization"] == duplicate["realization"]
    reference = first["realization"]
    assert reference["href"] == f"/app/tasks?task={reference['task_id']}"
    assert first["href"] == reference["href"]
    assert reference["receipt_id"].startswith("tmr_")
    assert reference["task_revision"] == 1
    assert len(stack.tasks.list()) == 1
    assert len(stack.calls) == 1
    assert (
        stack.calls[0]["client_mutation_id"] == f"task-proposal:{proposal['thread_id']}"
    )
    events = store.list_events(proposal["thread_id"])
    assert (
        len([event for event in events if event.kind == KIND_ACTION_EXECUTION_INTENT])
        == 1
    )
    assert len([event for event in events if event.kind == KIND_ACTION_REALIZED]) == 1


def test_concurrent_accepts_create_one_task_and_one_realization(stack):
    proposal = create(stack)
    barrier = Barrier(2)

    def execute(**parameters):
        barrier.wait(timeout=10)
        return stack.execute(**parameters)

    stack.service = ActionProposalService(db_path=stack.thread_path, executor=execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(accept, stack, proposal, mutation_id=f"accept-{n}")
            for n in range(2)
        ]
        first, second = [future.result(timeout=20) for future in futures]
    assert first["realization"] == second["realization"]
    assert first["status"] == second["status"] == "realized"
    assert len(stack.tasks.list()) == 1
    assert (
        len(
            [
                event
                for event in store.list_events(proposal["thread_id"])
                if event.kind == KIND_ACTION_REALIZED
            ]
        )
        == 1
    )
    assert {call["client_mutation_id"] for call in stack.calls} == {
        f"task-proposal:{proposal['thread_id']}"
    }


def test_process_crash_after_task_commit_recovers_with_the_frozen_key(stack):
    class SimulatedProcessDeath(BaseException):
        pass

    proposal = create(stack)

    def crash_after_commit(**parameters):
        stack.execute(**parameters)
        raise SimulatedProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_after_commit
    )
    with pytest.raises(SimulatedProcessDeath):
        accept(stack, proposal)
    assert len(stack.tasks.list()) == 1
    assert stack.service.get(proposal["thread_id"])["proposal"]["status"] == "executing"
    with pytest.raises(ProposalError) as error:
        stack.service.revise(
            proposal["thread_id"],
            client_mutation_id="edit-after-crash",
            expected_proposal_event_id=proposal["proposal_event_id"],
            parameters={"task_text": "Do not create another task"},
        )
    assert error.value.code == "proposal_locked"
    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=stack.execute
    )
    results = stack.service.reconcile_pending()
    assert len(results) == 1
    recovered = results[0]["proposal"]
    assert recovered["status"] == "realized"
    assert len(stack.tasks.list()) == 1
    assert recovered["realization"]["task_id"] == stack.tasks.list()[0].task_id
    assert len(stack.calls) == 2
    assert stack.calls[0] == stack.calls[1]
    assert accept(stack, proposal)["realization"] == recovered["realization"]


@pytest.mark.parametrize("failure", ["execution_error", "unavailable", "process_death"])
def test_bounded_recovery_durably_rotates_failure_before_later_approval(stack, failure):
    class ProcessDeath(BaseException):
        pass

    def crash_before_task(**_):
        raise ProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_before_task
    )
    older = create(stack, mutation_id="older-proposal", task_text="Permanent failure")
    later = create(stack, mutation_id="later-proposal", task_text="Recover this task")
    for proposal in (older, later):
        with pytest.raises(ProcessDeath):
            accept(stack, proposal, mutation_id=f"accept-{proposal['thread_id']}")
    ready = create(stack, mutation_id="not-approved", task_text="Still awaiting review")
    if failure == "unavailable":
        store.append_event(
            ThreadEvent(
                thread_id=older["thread_id"],
                kind=KIND_ACTION_EXECUTION_INTENT,
                actor="agent",
                data=["malformed intent must never dispatch"],
            )
        )
    attempts = []

    def recover(**parameters):
        attempts.append(parameters["task_text"])
        if parameters["task_text"] == older["parameters"]["task_text"]:
            if failure == "process_death":
                raise ProcessDeath()
            raise RuntimeError("permanent execution failure")
        return stack.execute(**parameters)

    def sweep():
        # No in-memory cursor may be required across sidecar restarts.
        return ActionProposalService(
            db_path=stack.thread_path, executor=recover
        ).reconcile_pending(limit=1)

    def check_older():
        if failure == "process_death":
            with pytest.raises(ProcessDeath):
                sweep()
        else:
            result = sweep()
            assert len(result) == 1
            assert result[0]["proposal"]["thread_id"] == older["thread_id"]
            assert result[0]["proposal"]["status"] == (
                "unavailable" if failure == "unavailable" else "needs_attention"
            )

    check_older()
    assert stack.tasks.list() == []
    recovered = sweep()
    assert len(recovered) == 1
    assert recovered[0]["proposal"]["thread_id"] == later["thread_id"]
    assert recovered[0]["proposal"]["status"] == "realized"
    assert len(stack.tasks.list()) == 1
    assert stack.calls[0]["client_mutation_id"] == f"task-proposal:{later['thread_id']}"
    # Failure remains retryable, but it can no longer monopolize every sweep.
    check_older()
    assert len(stack.tasks.list()) == 1
    assert attempts == (
        ["Recover this task"]
        if failure == "unavailable"
        else ["Permanent failure", "Recover this task", "Permanent failure"]
    )
    for proposal, expected_checks in ((older, 2), (later, 1), (ready, 0)):
        checks = [
            event
            for event in store.list_events(proposal["thread_id"])
            if event.kind == KIND_ACTION_RECOVERY_CHECKED
        ]
        assert len(checks) == expected_checks
        assert all(event.actor == "sidecar" for event in checks)
    assert stack.service.get(ready["thread_id"])["proposal"]["status"] == "ready"


def test_concurrent_bounded_sweeps_atomically_rotate_selected_intents(stack):
    class ProcessDeath(BaseException):
        pass

    def crash_before_task(**_):
        raise ProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_before_task
    )
    proposals = [create(stack, mutation_id=f"proposal-{index}") for index in range(2)]
    for proposal in proposals:
        with pytest.raises(ProcessDeath):
            accept(stack, proposal, mutation_id=f"accept-{proposal['thread_id']}")
    dispatch_barrier = Barrier(2)

    def recover(**parameters):
        # Keep both intents unresolved until both workers select their batch.
        dispatch_barrier.wait(timeout=10)
        return stack.execute(**parameters)

    def sweep(_):
        return ActionProposalService(
            db_path=stack.thread_path, executor=recover
        ).reconcile_pending(limit=1)[0]["proposal"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sweep, range(2)))
    assert {result["thread_id"] for result in results} == {
        proposal["thread_id"] for proposal in proposals
    }
    assert all(result["status"] == "realized" for result in results)
    assert len(stack.tasks.list()) == 2


@pytest.mark.parametrize("summary", [None, "A summary whose document must also replay"])
def test_cross_process_recovery_retains_original_approver_not_current_session(
    stack, monkeypatch, summary
):
    from work_buddy.tasks import runtime
    from work_buddy.work_item import task_adapter

    class ProcessDeath(BaseException):
        pass

    monkeypatch.setattr(runtime, "originating_session", lambda: "dashboard-session-a")
    proposal = create(stack, **({"summary": summary} if summary else {}))
    committed = []

    def crash_after_commit(**parameters):
        committed.append(stack.execute(**parameters))
        raise ProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_after_commit
    )
    with pytest.raises(ProcessDeath):
        accept(stack, proposal)
    assert committed[0]["receipt"]["actor"] == "user:owner"
    # The trusted create-attribution context must reset even on process death.
    assert task_adapter._native_authority("create", "unrelated")["actor"] == (
        "agent:dashboard-session-a"
    )
    monkeypatch.setattr(runtime, "originating_session", lambda: "sidecar-session-b")
    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=stack.execute
    )
    recovered = stack.service.reconcile_pending()[0]["proposal"]
    assert recovered["status"] == "realized"
    assert recovered["realization"]["task_id"] == committed[0]["task_id"]
    assert (
        recovered["realization"]["receipt_id"] == committed[0]["receipt"]["receipt_id"]
    )
    assert len(stack.tasks.list()) == 1
    assert task_adapter._native_authority("create", "unrelated")["actor"] == (
        "agent:sidecar-session-b"
    )


def test_uncertain_exception_is_safe_retry_not_editable_failure(stack):
    proposal = create(stack)

    def fail_after_commit(**parameters):
        stack.execute(**parameters)
        raise RuntimeError("private secret in provider error")

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=fail_after_commit
    )
    failed = accept(stack, proposal)
    assert failed["status"] == "needs_attention"
    assert "private secret" not in str(failed)
    assert len(stack.tasks.list()) == 1
    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=stack.execute
    )
    assert accept(stack, proposal)["status"] == "realized"
    assert len(stack.tasks.list()) == 1


def test_rich_task_fields_and_summary_attachment_replay(stack):
    from work_buddy.work_item import task_adapter

    parameters = {
        "task_text": "Ship full draft fidelity",
        "state": "active",
        "urgency": "high",
        "summary": "Context for the task",
        "outcome_text": "Safe proposals ship",
        "next_action_text": "Run failure tests",
        "definition_of_done": "All tests pass",
        "dependencies": ["Confirm the design"],
        "has_dependency": True,
        "due_date": "2026-08-30",
        "deadline_date": "2026-08-31",
        "project": "work-buddy",
        "tags": ["systems/tasks"],
    }
    proposal = create(stack, **parameters)
    first = accept(stack, proposal)
    # Replay the standard action with its original approver, including the
    # summary-document step; callers may not relabel another actor's receipt.
    with task_adapter.task_creation_attribution(actor="user:owner"):
        replay = task_adapter.create(
            **parameters, client_mutation_id=f"task-proposal:{proposal['thread_id']}"
        )
    assert replay["task_id"] == first["realization"]["task_id"]
    assert replay["receipt"]["receipt_id"] == first["realization"]["receipt_id"]
    assert replay["replayed"] is True
    assert len(stack.tasks.list()) == 1
    task = stack.tasks.list()[0]
    assert task.state == "active"
    assert task.summary_text == parameters["summary"]
    assert task.outcome_text == parameters["outcome_text"]
    assert task.next_action_text == parameters["next_action_text"]
    assert task.definition_of_done == parameters["definition_of_done"]
    assert list(task.dependencies) == parameters["dependencies"]
    assert task.due_date == parameters["due_date"]
    assert task.deadline_date == parameters["deadline_date"]
    assert set(task.namespace_tags) == {"systems/tasks", "projects/work-buddy"}
    assert task.revision == 2


@pytest.mark.parametrize(
    "payload,cleared,code",
    [
        (
            {"kind": "standard", "name": "email_send", "parameters": {}},
            False,
            "proposal_wrong_kind",
        ),
        (
            {"kind": "improvised", "name": "task_create", "parameters": {}},
            False,
            "proposal_wrong_kind",
        ),
        (
            {
                "kind": "standard",
                "name": "task_create",
                "parameters": {"task_text": 123},
            },
            False,
            "proposal_malformed",
        ),
        (None, False, "proposal_malformed"),
        ({}, True, "proposal_superseded"),
    ],
)
def test_latest_action_projection_never_falls_back_to_older_task_action(
    stack, payload, cleared, code
):
    proposal = create(stack)
    store.append_event(
        ThreadEvent(
            thread_id=proposal["thread_id"],
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": payload, "cleared": cleared},
        )
    )
    result = stack.service.get(proposal["thread_id"])["proposal"]
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == code
    assert (
        stack.service.reconcile(proposal["thread_id"])["proposal"]["status"]
        == "unavailable"
    )
    assert stack.tasks.list() == []


def test_missing_foreign_scope_and_invalid_ids_are_typed(stack):
    assert (
        stack.service.get("th-missing")["proposal"]["error"]["code"]
        == "proposal_not_found"
    )
    other = Thread()
    store.insert_thread(other)
    assert (
        stack.service.get(other.thread_id)["proposal"]["error"]["code"]
        == "proposal_foreign_scope"
    )
    for bad in ("t-12345678", "th-<script>", "th-../../etc"):
        with pytest.raises(ProposalError) as error:
            stack.service.get(bad)
        assert error.value.code == "proposal_invalid_id"


def test_no_task_on_read_reconcile_or_unfenced_legacy_transition(stack):
    proposal = create(stack)
    assert (
        stack.service.reconcile(proposal["thread_id"])["proposal"]["status"] == "ready"
    )
    with pytest.raises(engine.InvalidTransition, match="version-fenced"):
        engine.transition(proposal["thread_id"], "approve", fire_side_effects=True)
    with pytest.raises(ProposalError) as error:
        stack.service.accept(
            proposal["thread_id"],
            client_mutation_id="agent-accept",
            expected_proposal_event_id=proposal["proposal_event_id"],
            actor="agent",
        )
    assert error.value.code == "proposal_human_required"
    assert stack.tasks.list() == []
    assert stack.calls == []


def test_native_authority_is_required_before_standard_execution(stack, monkeypatch):
    from work_buddy.tasks import runtime

    proposal = create(stack)
    monkeypatch.setattr(runtime, "native_task_mutation_authority", lambda: False)
    stack.service = ActionProposalService(db_path=stack.thread_path)
    result = accept(stack, proposal)
    assert result["status"] == "needs_attention"
    assert result["error"]["code"] == "proposal_task_authority_unavailable"
    assert stack.tasks.list() == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"task_text": "Valid", "client_mutation_id": "injected"},
        {"task_text": "Valid", "task_id": "t-injected"},
        {"task_text": "Valid", "unknown_field": True},
        {"task_text": "Valid", "tags": "not-a-list"},
        {"task_text": "Valid", "due_date": "2026-02-30"},
        {"task_text": "Valid", "urgency": "urgent"},
    ],
)
def test_ingress_rejects_malformed_or_runtime_bound_parameters(stack, parameters):
    with pytest.raises(ProposalError):
        create(stack, **parameters)
    assert table_count("threads") == 0
    assert stack.calls == []


def test_http_create_review_revise_and_accept_preserve_human_authority_contract(stack):
    from flask import Flask

    from work_buddy.dashboard.action_proposals_api import create_blueprint

    calls = []

    def authorizer(operation, subject, method, path, body):
        calls.append((operation, subject, method, path, body))
        return "user:owner"

    app = Flask(__name__)
    app.register_blueprint(create_blueprint(stack.service, authorizer=authorizer))
    client = app.test_client()
    body = {
        "client_mutation_id": "api-create",
        "action": {
            "name": "task_create",
            "parameters": {"task_text": "Review this draft"},
        },
        "origin": {"kind": "task_draft", "id": "draft-1", "revision": 4},
    }
    response = client.post("/api/threads/action-proposals", json=body)
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    proposal = response.json["proposal"]
    assert calls[-1][:4] == (
        "create",
        "proposal:new:api-create",
        "POST",
        "/api/threads/action-proposals",
    )
    assert stack.tasks.list() == []
    base = f"/api/threads/{proposal['thread_id']}/proposal"
    assert client.get(base).json["proposal"] == proposal
    assert (
        client.post(base + "/accept", json={"client_mutation_id": "api-accept"}).json[
            "error"
        ]["code"]
        == "proposal_version_required"
    )
    revised = client.post(
        base + "/revise",
        json={
            "client_mutation_id": "api-revise",
            "expected_proposal_event_id": proposal["proposal_event_id"],
            "parameters": {"task_text": "Reviewed draft"},
        },
    ).json["proposal"]
    stale = client.post(
        base + "/accept",
        json={
            "client_mutation_id": "api-accept",
            "expected_proposal_event_id": proposal["proposal_event_id"],
        },
    )
    assert stale.status_code == 409
    assert stale.json["error"]["code"] == "proposal_revision_conflict"
    accepted = client.post(
        base + "/accept",
        json={
            "client_mutation_id": "api-accept-current",
            "expected_proposal_event_id": revised["proposal_event_id"],
        },
    )
    assert accepted.status_code == 200
    assert accepted.json["proposal"]["status"] == "realized"
    assert calls[-1][:4] == (
        "accept",
        f"proposal:{proposal['thread_id']}",
        "POST",
        base + "/accept",
    )
    assert len(stack.tasks.list()) == 1


def test_http_final_action_cannot_include_unreviewed_edits(stack):
    from flask import Flask

    from work_buddy.dashboard.action_proposals_api import create_blueprint

    proposal = create(stack)
    app = Flask(__name__)
    app.register_blueprint(
        create_blueprint(stack.service, authorizer=lambda *_: "user:owner")
    )
    client = app.test_client()
    response = client.post(
        f"/api/threads/{proposal['thread_id']}/proposal/accept",
        json={
            "client_mutation_id": "accept",
            "expected_proposal_event_id": proposal["proposal_event_id"],
            "parameters": {"task_text": "Not what was reviewed"},
        },
    )
    assert response.status_code == 400
    assert stack.calls == []


def test_http_authority_context_is_bound_to_exact_action_body_and_path(
    stack, monkeypatch
):
    from flask import Flask

    from work_buddy.dashboard import local_identity_api
    from work_buddy.dashboard.action_proposals_api import create_blueprint
    from work_buddy.truth.identity import canonical_json, sha256_text

    checked = []

    def require(**kwargs):
        checked.append(kwargs)
        return SimpleNamespace(
            principal=SimpleNamespace(actor=SimpleNamespace(canonical_id="user:owner"))
        )

    monkeypatch.setattr(local_identity_api, "require_human_authority_request", require)
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(stack.service))
    body = {
        "client_mutation_id": "http-auth-create",
        "action": {
            "name": "task_create",
            "parameters": {"task_text": "Bound to my gesture"},
        },
        "origin": {"kind": "task_draft", "id": "draft-1"},
    }
    response = app.test_client().post("/api/threads/action-proposals", json=body)
    assert response.status_code == 201
    assert checked == [
        {
            "action": "dashboard.action_proposals.create",
            "subject": "proposal:new:http-auth-create",
            "context_sha256": sha256_text(
                canonical_json(
                    {
                        "method": "POST",
                        "path": "/api/threads/action-proposals",
                        "body": body,
                    }
                )
            ),
        }
    ]


def test_http_requires_human_authority_and_honors_read_only(stack, monkeypatch):
    from flask import Flask

    from work_buddy.dashboard import local_identity_api
    from work_buddy.dashboard.action_proposals_api import create_blueprint
    from work_buddy.security.local_identity import LocalIdentityError

    def deny(**_):
        raise LocalIdentityError(
            "gesture_required", "A human gesture is required.", status=403
        )

    monkeypatch.setattr(local_identity_api, "require_human_authority_request", deny)
    body = {
        "client_mutation_id": "blocked",
        "action": {
            "name": "task_create",
            "parameters": {"task_text": "No unauthorized write"},
        },
        "origin": {"kind": "task_draft", "id": "draft"},
    }
    for read_only, code in ((False, "gesture_required"), (True, "proposal_read_only")):
        app = Flask(__name__)
        app.register_blueprint(
            create_blueprint(
                stack.service, dashboard_read_only=lambda value=read_only: value
            )
        )
        response = app.test_client().post("/api/threads/action-proposals", json=body)
        assert response.status_code == 403
        assert response.json["error"]["code"] == code
    assert table_count("threads") == 0
    assert stack.tasks.list() == []


def test_boot_recovery_is_sidecar_only_bounded_and_runs_once(stack, monkeypatch):
    from work_buddy.threads import action_proposals, bootstrap

    calls = []
    fake = SimpleNamespace(
        reconcile_pending=lambda **kwargs: calls.append(kwargs) or []
    )
    monkeypatch.setattr(action_proposals, "get_action_proposal_service", lambda: fake)
    monkeypatch.setattr(bootstrap, "bootstrap_threads", lambda: None)
    monkeypatch.setattr(bootstrap, "_PROPOSAL_RECOVERY_ATTEMPTED", False)
    assert bootstrap.bootstrap_for_subprocess(subprocess_name="dashboard") is True
    assert (
        bootstrap.bootstrap_for_subprocess(subprocess_name="mcp-gateway-reload") is True
    )
    assert calls == []
    assert bootstrap.bootstrap_for_subprocess(subprocess_name="sidecar") is True
    assert calls == [{"limit": 50}]
    assert bootstrap.bootstrap_for_subprocess(subprocess_name="sidecar") is True
    assert calls == [{"limit": 50}]


def test_boot_recovery_failure_does_not_break_generic_threads(stack, monkeypatch):
    from work_buddy.threads import action_proposals, bootstrap

    def unavailable(**_):
        raise RuntimeError("temporary store unavailable")

    monkeypatch.setattr(
        action_proposals,
        "get_action_proposal_service",
        lambda: SimpleNamespace(reconcile_pending=unavailable),
    )
    monkeypatch.setattr(bootstrap, "bootstrap_threads", lambda: None)
    monkeypatch.setattr(bootstrap, "_PROPOSAL_RECOVERY_ATTEMPTED", False)
    assert bootstrap.bootstrap_for_subprocess(subprocess_name="sidecar") is True
    assert stack.calls == []


def test_sidecar_boot_does_not_recover_approved_intents_in_read_only_mode(
    stack, monkeypatch
):
    from work_buddy import config
    from work_buddy.threads import action_proposals, bootstrap

    calls = []
    monkeypatch.setattr(
        config, "load_config", lambda: {"dashboard": {"read_only": True}}
    )
    monkeypatch.setattr(
        action_proposals,
        "get_action_proposal_service",
        lambda: calls.append("opened") or stack.service,
    )
    monkeypatch.setattr(bootstrap, "bootstrap_threads", lambda: None)
    monkeypatch.setattr(bootstrap, "_PROPOSAL_RECOVERY_ATTEMPTED", False)
    assert bootstrap.bootstrap_for_subprocess(subprocess_name="sidecar") is True
    assert calls == []


def test_accept_intent_commits_before_task_executor_runs(stack):
    proposal = create(stack)

    def observe(**parameters):
        events = store.list_events(proposal["thread_id"])
        intents = [
            event for event in events if event.kind == KIND_ACTION_EXECUTION_INTENT
        ]
        assert len(intents) == 1
        assert intents[0].data["proposal_event_id"] == proposal["proposal_event_id"]
        assert store.get_thread(proposal["thread_id"]).fsm_state.value == "executing"
        assert stack.tasks.list() == []
        return stack.execute(**parameters)

    stack.service = ActionProposalService(db_path=stack.thread_path, executor=observe)
    assert accept(stack, proposal)["status"] == "realized"


def test_two_tab_revise_vs_accept_is_serialized_by_reviewed_event(stack):
    proposal = create(stack)
    barrier = Barrier(2)

    def revise():
        barrier.wait(timeout=5)
        try:
            return stack.service.revise(
                proposal["thread_id"],
                client_mutation_id="racing-revise",
                expected_proposal_event_id=proposal["proposal_event_id"],
                parameters={"task_text": "A newly reviewed intention"},
            )["proposal"]
        except ProposalError as exc:
            return exc.code

    def approve():
        barrier.wait(timeout=5)
        try:
            return accept(stack, proposal)
        except ProposalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        revision_future, acceptance_future = pool.submit(revise), pool.submit(approve)
        revision, acceptance = (
            revision_future.result(timeout=15),
            acceptance_future.result(timeout=15),
        )
    if isinstance(revision, dict):
        assert acceptance == "proposal_revision_conflict"
        assert stack.tasks.list() == []
    else:
        assert revision == "proposal_locked"
        assert acceptance["status"] == "realized"
        assert len(stack.tasks.list()) == 1
        assert stack.tasks.list()[0].description == proposal["parameters"]["task_text"]


@pytest.mark.parametrize(
    "bad_result",
    [
        {"success": True, "task_id": "t-no-receipt", "revision": 1},
        {"success": False, "error": "private details"},
        {
            "success": True,
            "task_id": "https://evil.test",
            "revision": 1,
            "receipt": {
                "client_mutation_id": "wrong-key",
                "receipt_id": "wrong-receipt",
                "status": "completed",
            },
        },
    ],
)
def test_execution_without_matching_native_receipt_never_claims_realization(
    stack, bad_result
):
    proposal = create(stack)
    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=lambda **_: bad_result
    )
    result = accept(stack, proposal)
    assert result["status"] == "needs_attention"
    assert result["realization"] is None
    assert "private details" not in str(result)


def test_changed_frozen_intent_is_not_dispatched_after_a_crash(stack):
    proposal = create(stack)

    class ProcessDeath(BaseException):
        pass

    def crash_before_task(**_):
        raise ProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_before_task
    )
    with pytest.raises(ProcessDeath):
        accept(stack, proposal)
    intent = next(
        event
        for event in store.list_events(proposal["thread_id"])
        if event.kind == KIND_ACTION_EXECUTION_INTENT
    )
    # Simulate a corrupt or foreign writer trying to alter the frozen request.
    store.append_event(
        ThreadEvent(
            thread_id=proposal["thread_id"],
            kind=KIND_ACTION_EXECUTION_INTENT,
            actor="agent",
            data={
                **intent.data,
                "parameters": {"task_text": "An unapproved replacement"},
            },
        )
    )
    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=stack.execute
    )
    result = stack.service.reconcile(proposal["thread_id"])["proposal"]
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "proposal_superseded"
    assert stack.tasks.list() == []
    assert stack.calls == []


def test_stray_intent_without_atomic_acceptance_is_never_recovered(stack):
    from work_buddy.threads.action_proposals import _hash

    proposal = create(stack)
    store.append_event(
        ThreadEvent(
            thread_id=proposal["thread_id"],
            kind=KIND_ACTION_EXECUTION_INTENT,
            actor="user",
            data={
                "proposal_event_id": proposal["proposal_event_id"],
                "name": "task_create",
                "parameters": proposal["parameters"],
                "parameters_sha256": _hash(proposal["parameters"]),
                "client_mutation_id": f"task-proposal:{proposal['thread_id']}",
                "accept_client_mutation_id": "acceptance-that-never-committed",
            },
        )
    )
    result = stack.service.reconcile_pending()[0]["proposal"]
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "proposal_malformed"
    assert stack.tasks.list() == []
    assert stack.calls == []


def test_malformed_execution_result_is_typed_unavailable(stack):
    proposal = create(stack)

    class ProcessDeath(BaseException):
        pass

    def crash_before_task(**_):
        raise ProcessDeath()

    stack.service = ActionProposalService(
        db_path=stack.thread_path, executor=crash_before_task
    )
    with pytest.raises(ProcessDeath):
        accept(stack, proposal)
    store.append_event(
        ThreadEvent(
            thread_id=proposal["thread_id"],
            kind=KIND_EXECUTION_FINISHED,
            actor="sidecar",
            data=["malformed result"],
        )
    )
    result = stack.service.get(proposal["thread_id"])["proposal"]
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "proposal_malformed"
    assert stack.tasks.list() == []


@pytest.mark.parametrize(
    "endpoint,body",
    [
        (
            "accept",
            {"action_overrides": {"task_create": {"task_text": "Unreviewed edit"}}},
        ),
        (
            "set_action_proposal",
            {
                "capability_name": "task_create",
                "parameters": {"task_text": "Unreviewed replacement"},
            },
        ),
        (
            "redirect_action",
            {
                "feedback": "Change the action without its reviewed version",
                "seed_params": {"task_text": "Unreviewed redirect"},
            },
        ),
    ],
)
def test_legacy_dashboard_routes_cannot_edit_or_execute_managed_proposals(
    stack, endpoint, body
):
    from work_buddy.dashboard import service as dashboard

    proposal = create(stack)
    before = [event.to_dict() for event in store.list_events(proposal["thread_id"])]
    response = dashboard.app.test_client().post(
        f"/api/threads/{proposal['thread_id']}/{endpoint}",
        json=body,
    )
    assert response.status_code == 409
    assert response.json["code"] == "proposal_api_required"
    assert response.json["href"] == proposal["href"]
    assert [
        event.to_dict() for event in store.list_events(proposal["thread_id"])
    ] == before
    assert stack.tasks.list() == []
    assert stack.calls == []
