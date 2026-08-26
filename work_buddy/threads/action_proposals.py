"""Thread-owned, reviewed-event-fenced task proposals.

There is deliberately no pending-task or proposal-content table. The latest
action event is the proposal; execution intent and realization are events in
that same Thread. Only command-key receipts are indexed separately. TaskStore
starts at explicit acceptance and is invoked through the standard Task.create
write port with one permanent mutation key, including after an uncertain crash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_ACTION_APPROVED,
    KIND_ACTION_CORRECTED,
    KIND_ACTION_EXECUTION_INTENT,
    KIND_ACTION_INFERRED,
    KIND_ACTION_REALIZED,
    KIND_ACTION_RECOVERY_CHECKED,
    KIND_ACTION_REJECTED,
    KIND_EXECUTION_FINISHED,
    KIND_EXECUTION_STARTED,
    KIND_STATE_TRANSITION,
    KIND_THREAD_COMPLETED,
    KIND_THREAD_CREATED,
    KIND_THREAD_DISMISSED,
    ThreadEvent,
)
from work_buddy.threads.models import Thread

logger = logging.getLogger(__name__)
PROPOSAL_SCHEMA = "wb.action-proposal/v1"
_THREAD_ID = re.compile(r"th-[A-Za-z0-9_-]{1,96}\Z")
_TASK_ID = re.compile(r"t-[A-Za-z0-9_-]{1,96}\Z")
_MAX_PARAMETERS_BYTES = 65_536
_MAX_ORIGIN_BYTES = 8_192
_TEXT_FIELDS = frozenset(
    {
        "task_text",
        "urgency",
        "state",
        "project",
        "due_date",
        "deadline_date",
        "contract",
        "summary",
        "outcome_text",
        "next_action_text",
        "definition_of_done",
        "complexity",
        "task_kind",
        "density",
        "creation_effort",
        "user_involvement",
        "creation_provenance",
        "dependency_hint",
        "risk_profile_json",
        "required_contexts_source",
    }
)
_LIST_FIELDS = frozenset(
    {
        "tags",
        "dependencies",
        "agent_required_contexts",
        "user_required_contexts",
    }
)
_PARAMETER_FIELDS = (
    _TEXT_FIELDS
    | _LIST_FIELDS
    | {
        "has_dependency",
        "automation_tier_achievable",
    }
)


class ProposalError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProposalError("proposal_invalid", "Use finite JSON values.") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProposalError("proposal_invalid", f"A valid {field} is required.")
    return value.strip()


def _thread_id(value: Any) -> str:
    if not isinstance(value, str) or not _THREAD_ID.fullmatch(value):
        raise ProposalError("proposal_invalid_id", "Use a proposal Thread ID.")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ProposalError(
            "proposal_version_required",
            "The reviewed proposal event ID is required.",
        )
    return value


def validate_task_parameters(value: Any) -> dict[str, Any]:
    """Validate standard task_create parameters, never silently discard fields."""
    if not isinstance(value, Mapping):
        raise ProposalError(
            "proposal_invalid_parameters", "Task parameters must be an object."
        )
    parameters = json.loads(_json(dict(value)))
    if len(_json(parameters).encode("utf-8")) > _MAX_PARAMETERS_BYTES:
        raise ProposalError(
            "proposal_too_large", "The task proposal is too large.", 413
        )
    unknown = parameters.keys() - _PARAMETER_FIELDS
    if unknown:
        raise ProposalError(
            "proposal_invalid_parameters",
            "Unsupported task parameters: " + ", ".join(sorted(unknown)),
        )
    parameters["task_text"] = _text(
        parameters.get("task_text"), "task text", maximum=16_384
    )
    for key in _TEXT_FIELDS - {"task_text"}:
        if key in parameters and parameters[key] is not None:
            if not isinstance(parameters[key], str):
                raise ProposalError(
                    "proposal_invalid_parameters", f"{key} must be text or null."
                )
            parameters[key] = parameters[key].strip() or None
    for key in _LIST_FIELDS:
        if key not in parameters:
            continue
        entries = parameters[key]
        if (
            not isinstance(entries, list)
            or len(entries) > 100
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 512
                for item in entries
            )
        ):
            raise ProposalError(
                "proposal_invalid_parameters", f"{key} must be a bounded list of text."
            )
        parameters[key] = [item.strip() for item in entries]
    from work_buddy.tasks.models import VALID_ATTENTION_STATES, VALID_URGENCIES

    for key, allowed, default in (
        ("state", VALID_ATTENTION_STATES, "inbox"),
        ("urgency", VALID_URGENCIES, "medium"),
    ):
        if parameters.get(key, default) not in allowed:
            raise ProposalError(
                "proposal_invalid_parameters", f"Unsupported task {key}."
            )
    for key in ("due_date", "deadline_date"):
        if parameters.get(key) is not None:
            try:
                date.fromisoformat(parameters[key])
            except ValueError as exc:
                raise ProposalError(
                    "proposal_invalid_parameters", f"{key} must be a calendar date."
                ) from exc
    if (
        "has_dependency" in parameters
        and type(parameters["has_dependency"]) is not bool
    ):
        raise ProposalError(
            "proposal_invalid_parameters", "has_dependency must be true or false."
        )
    tier = parameters.get("automation_tier_achievable")
    if tier is not None and (type(tier) is not int or not 0 <= tier <= 5):
        raise ProposalError(
            "proposal_invalid_parameters", "Unsupported automation tier."
        )
    return parameters


def _origin(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProposalError(
            "proposal_invalid_origin", "Proposal provenance is required."
        )
    origin = json.loads(_json(dict(value)))
    if len(_json(origin).encode("utf-8")) > _MAX_ORIGIN_BYTES:
        raise ProposalError(
            "proposal_too_large", "Proposal provenance is too large.", 413
        )
    origin["kind"] = _text(origin.get("kind"), "origin kind", maximum=80)
    origin["id"] = _text(origin.get("id"), "origin ID", maximum=1024)
    return origin


def is_managed_proposal(thread: Thread) -> bool:
    return (
        isinstance(thread.inciting_event_summary, dict)
        and thread.inciting_event_summary.get("action_proposal_schema")
        == PROPOSAL_SCHEMA
    )


def _standard_task_create(**parameters: Any) -> dict[str, Any]:
    # The pre-cutover compatibility path cannot provide native replay receipts.
    # Fail before dispatch instead of silently downgrading this guarantee.
    from work_buddy.tasks.runtime import native_task_mutation_authority
    from work_buddy.threads.models import Task

    if not native_task_mutation_authority():
        raise ProposalError(
            "proposal_task_authority_unavailable",
            "Task creation is temporarily unavailable.",
            503,
            retryable=True,
        )
    return Task.create(**parameters)


class ActionProposalService:
    """Atomic Thread application boundary; injectable DB and executor for tests.

    ``executor`` has the same keyword arguments as standard task_create and
    must honor its client_mutation_id contract. Reads never reconcile/execute.
    Every write returns ``{ok, proposal, replayed}``; projection keys are stable
    snake_case across Journal and HTTP consumers.
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        executor: Callable[..., Any] | None = None,
    ) -> None:
        if db_path is not None and connection_factory is not None:
            raise ValueError("Choose db_path or connection_factory, not both")
        self._connect = connection_factory or (
            (lambda: store.get_connection(path=db_path))
            if db_path is not None
            else store.get_connection
        )
        self._executor = executor or _standard_task_create

    def _transaction(self):
        from work_buddy.backups.source_foundation_restore import (
            require_source_foundation_writable,
        )

        require_source_foundation_writable("threads.action_proposals")
        return store.transaction(connection_factory=self._connect)

    @staticmethod
    def _append(
        conn, thread_id: str, kind: str, actor: str, data: dict[str, Any]
    ) -> ThreadEvent:
        event = store.append_event(
            ThreadEvent(
                thread_id=thread_id,
                kind=kind,
                actor=actor,
                data=data,
                parent_event_id=store.latest_event_id(thread_id, conn=conn),
            ),
            conn=conn,
        )
        store.update_thread_state(thread_id, parent_event_id=event.id, conn=conn)
        return event

    def _transition(
        self, conn, thread_id: str, state: FSMState, actor: str, trigger: str
    ) -> None:
        current = store.get_thread(thread_id, conn=conn)
        assert current is not None
        self._append(
            conn,
            thread_id,
            KIND_STATE_TRANSITION,
            actor,
            {
                "from": current.fsm_state.value,
                "to": state.value,
                "trigger": trigger,
                "action_proposal_schema": PROPOSAL_SCHEMA,
            },
        )
        store.update_thread_state(thread_id, fsm_state=state.value, conn=conn)

    @staticmethod
    def _receipt(conn, key: str, request_hash: str):
        receipt = conn.execute(
            "SELECT * FROM thread_proposal_mutations WHERE client_mutation_id = ?",
            (key,),
        ).fetchone()
        if receipt is not None and receipt["request_sha256"] != request_hash:
            raise ProposalError(
                "proposal_idempotency_conflict",
                "That mutation ID was used for a different request.",
                409,
            )
        return receipt

    @staticmethod
    def _record_receipt(
        conn, key: str, request_hash: str, operation: str, thread_id: str, event_id: int
    ) -> None:
        conn.execute(
            "INSERT INTO thread_proposal_mutations "
            "(client_mutation_id, request_sha256, operation, thread_id, event_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, request_hash, operation, thread_id, event_id, store._now_iso()),
        )

    @staticmethod
    def _latest(events: list[ThreadEvent], kind: str) -> ThreadEvent | None:
        return next((event for event in reversed(events) if event.kind == kind), None)

    def _projection(self, thread_id: str, conn) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "thread_id": thread_id,
            "proposal_event_id": None,
            "status": "unavailable",
            "parameters": {},
            "origin": {},
            "realization": None,
            "href": f"/app/tasks?proposal={thread_id}",
        }

        def unavailable(code: str, message: str):
            projection["error"] = {"code": code, "message": message, "retryable": False}
            return projection

        try:
            thread = store.get_thread(thread_id, conn=conn)
        except (ValueError, TypeError, KeyError):
            return unavailable(
                "proposal_malformed", "The proposal Thread is malformed."
            )
        if thread is None:
            return unavailable(
                "proposal_not_found", "That proposal could not be found."
            )
        if not is_managed_proposal(thread):
            return unavailable(
                "proposal_foreign_scope",
                "This Thread is not a reviewable task proposal.",
            )
        projection["origin"] = thread.inciting_event_summary.get("origin", {})
        try:
            events = store.list_events(thread_id, conn=conn)
        except (ValueError, TypeError, KeyError):
            return unavailable(
                "proposal_malformed", "The proposal history is malformed."
            )
        action = self._latest(events, KIND_ACTION_INFERRED)
        if action is None:
            return unavailable("proposal_malformed", "The proposal action is missing.")
        projection["proposal_event_id"] = action.id
        if not isinstance(action.data, dict):
            return unavailable(
                "proposal_malformed", "The proposal action is malformed."
            )
        # A cleared latest event is a tombstone. Never resurrect an older action.
        if action.data.get("cleared"):
            return unavailable(
                "proposal_superseded", "That proposal has been superseded."
            )
        payload = action.data.get("payload")
        if not isinstance(payload, dict):
            return unavailable(
                "proposal_malformed", "The proposal action is malformed."
            )
        if payload.get("kind") != "standard" or payload.get("name") != "task_create":
            return unavailable(
                "proposal_wrong_kind", "This Thread no longer proposes creating a task."
            )
        try:
            projection["parameters"] = validate_task_parameters(
                payload.get("parameters")
            )
        except ProposalError:
            return unavailable(
                "proposal_malformed", "The task proposal fields are malformed."
            )
        intent = self._latest(events, KIND_ACTION_EXECUTION_INTENT)
        realized = self._latest(events, KIND_ACTION_REALIZED)
        if intent is not None and not isinstance(intent.data, dict):
            return unavailable(
                "proposal_malformed", "The approved execution intent is malformed."
            )
        if intent is not None and (
            intent.data.get("proposal_event_id") != action.id
            or intent.data.get("name") != "task_create"
            or intent.data.get("parameters_sha256") != _hash(projection["parameters"])
            or _hash(intent.data.get("parameters")) != _hash(projection["parameters"])
            or intent.data.get("client_mutation_id") != f"task-proposal:{thread_id}"
        ):
            return unavailable(
                "proposal_superseded",
                "The approved proposal was changed outside its review boundary.",
            )
        if intent is not None:
            approval = self._latest(events, KIND_ACTION_APPROVED)
            accept_key = intent.data.get("accept_client_mutation_id")
            accepted = (
                conn.execute(
                    "SELECT * FROM thread_proposal_mutations WHERE client_mutation_id = ?",
                    (accept_key,),
                ).fetchone()
                if isinstance(accept_key, str)
                else None
            )
            if (
                approval is None
                or not isinstance(approval.data, dict)
                or approval.data.get("proposal_event_id") != action.id
                or intent.parent_event_id != approval.id
                or intent.actor != approval.actor
                or accepted is None
                or accepted["operation"] != "accept"
                or accepted["thread_id"] != thread_id
                or accepted["event_id"] < intent.id
                or accepted["request_sha256"]
                != _hash(
                    {
                        "operation": "accept",
                        "thread_id": thread_id,
                        "expected": action.id,
                    }
                )
            ):
                return unavailable(
                    "proposal_malformed",
                    "The execution intent has no recorded acceptance.",
                )
        if realized is not None:
            try:
                realization = self._validate_realization(
                    realized.data.get("realization")
                    if isinstance(realized.data, dict)
                    else None,
                )
            except ProposalError:
                return unavailable(
                    "proposal_malformed", "The task realization reference is malformed."
                )
            if intent is None or realized.data.get("intent_event_id") != intent.id:
                return unavailable(
                    "proposal_malformed",
                    "The task realization is not bound to its approval.",
                )
            projection.update(
                status="realized", realization=realization, href=realization["href"]
            )
        elif intent is not None:
            finish = self._latest(events, KIND_EXECUTION_FINISHED)
            if finish is not None and not isinstance(finish.data, dict):
                return unavailable(
                    "proposal_malformed", "The execution result is malformed."
                )
            failed = finish is not None and finish.data.get("success") is False
            projection["status"] = "needs_attention" if failed else "executing"
            if failed:
                projection["error"] = finish.data.get(
                    "error",
                    {
                        "code": "proposal_execution_uncertain",
                        "message": "Task creation needs a safe retry.",
                        "retryable": True,
                    },
                )
        elif (
            self._latest(events, KIND_ACTION_REJECTED) is not None
            or thread.fsm_state == FSMState.DISMISSED
        ):
            projection["status"] = "rejected"
        elif (
            thread.fsm_state == FSMState.AWAITING_CONFIRMATION
            and not thread.archived_at
        ):
            projection["status"] = "ready"
        else:
            return unavailable(
                "proposal_unavailable", "That proposal is not awaiting review."
            )
        return projection

    def _envelope(
        self, thread_id: str, conn, *, replayed: bool = False
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "proposal": self._projection(thread_id, conn),
            "replayed": replayed,
        }

    def get(self, thread_id: str) -> dict[str, Any]:
        thread_id = _thread_id(thread_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            return self._envelope(thread_id, conn)

    def create_task_proposal(
        self,
        *,
        client_mutation_id: str,
        parameters: dict[str, Any],
        origin: dict[str, Any],
        actor: str = "user",
    ) -> dict[str, Any]:
        key = _text(client_mutation_id, "client mutation ID", maximum=512)
        parameters = validate_task_parameters(parameters)
        origin = _origin(origin)
        actor = _text(actor, "actor")
        digest = _hash(
            {"operation": "create", "parameters": parameters, "origin": origin}
        )
        with self._transaction() as conn:
            receipt = self._receipt(conn, key, digest)
            if receipt is not None:
                return self._envelope(receipt["thread_id"], conn, replayed=True)
            thread = Thread(
                inciting_event_summary={
                    "title": parameters["task_text"][:200],
                    "action_proposal_schema": PROPOSAL_SCHEMA,
                    "origin": origin,
                }
            )
            store.insert_thread(thread, conn=conn)
            self._append(
                conn,
                thread.thread_id,
                KIND_THREAD_CREATED,
                actor,
                {
                    "action_proposal_schema": PROPOSAL_SCHEMA,
                    "origin": origin,
                },
            )
            action = self._append(
                conn,
                thread.thread_id,
                KIND_ACTION_INFERRED,
                actor,
                {
                    "payload": {
                        "kind": "standard",
                        "name": "task_create",
                        "parameters": parameters,
                    },
                    "action_proposal_schema": PROPOSAL_SCHEMA,
                },
            )
            self._transition(
                conn,
                thread.thread_id,
                FSMState.AWAITING_CONFIRMATION,
                actor,
                "proposal_created",
            )
            self._record_receipt(
                conn, key, digest, "create", thread.thread_id, action.id
            )
            return self._envelope(thread.thread_id, conn)

    @staticmethod
    def _reviewed(projection: dict[str, Any], expected: int) -> None:
        if projection["status"] == "unavailable":
            error = projection["error"]
            raise ProposalError(
                error["code"],
                error["message"],
                404 if error["code"] == "proposal_not_found" else 409,
            )
        if projection["proposal_event_id"] != expected:
            raise ProposalError(
                "proposal_revision_conflict",
                "This proposal changed. Review its latest version before continuing.",
                409,
            )

    def revise(
        self,
        thread_id: str,
        *,
        client_mutation_id: str,
        expected_proposal_event_id: int,
        parameters: dict[str, Any],
        actor: str = "user",
    ) -> dict[str, Any]:
        thread_id, expected = (
            _thread_id(thread_id),
            _version(expected_proposal_event_id),
        )
        key = _text(client_mutation_id, "client mutation ID", maximum=512)
        actor = _text(actor, "actor")
        parameters = validate_task_parameters(parameters)
        digest = _hash(
            {
                "operation": "revise",
                "thread_id": thread_id,
                "expected": expected,
                "parameters": parameters,
            }
        )
        with self._transaction() as conn:
            if self._receipt(conn, key, digest) is not None:
                return self._envelope(thread_id, conn, replayed=True)
            projection = self._projection(thread_id, conn)
            self._reviewed(projection, expected)
            if projection["status"] != "ready":
                raise ProposalError(
                    "proposal_locked",
                    "An accepted or rejected proposal cannot be revised.",
                    409,
                )
            self._append(
                conn,
                thread_id,
                KIND_ACTION_CORRECTED,
                actor,
                {"proposal_event_id": expected},
            )
            action = self._append(
                conn,
                thread_id,
                KIND_ACTION_INFERRED,
                actor,
                {
                    "payload": {
                        "kind": "standard",
                        "name": "task_create",
                        "parameters": parameters,
                    },
                    "revision_of": expected,
                    "action_proposal_schema": PROPOSAL_SCHEMA,
                },
            )
            self._record_receipt(conn, key, digest, "revise", thread_id, action.id)
            return self._envelope(thread_id, conn)

    def reject(
        self,
        thread_id: str,
        *,
        client_mutation_id: str,
        expected_proposal_event_id: int,
        actor: str = "user",
    ) -> dict[str, Any]:
        thread_id, expected = (
            _thread_id(thread_id),
            _version(expected_proposal_event_id),
        )
        key = _text(client_mutation_id, "client mutation ID", maximum=512)
        actor = _text(actor, "actor")
        digest = _hash(
            {"operation": "reject", "thread_id": thread_id, "expected": expected}
        )
        with self._transaction() as conn:
            if self._receipt(conn, key, digest) is not None:
                return self._envelope(thread_id, conn, replayed=True)
            projection = self._projection(thread_id, conn)
            self._reviewed(projection, expected)
            if projection["status"] not in {"ready", "rejected"}:
                raise ProposalError(
                    "proposal_locked",
                    "Task creation has started; this proposal cannot be rejected.",
                    409,
                )
            replayed = projection["status"] == "rejected"
            if not replayed:
                self._append(
                    conn,
                    thread_id,
                    KIND_ACTION_REJECTED,
                    actor,
                    {"proposal_event_id": expected},
                )
                self._append(
                    conn,
                    thread_id,
                    KIND_THREAD_DISMISSED,
                    actor,
                    {"proposal_event_id": expected},
                )
                self._transition(
                    conn, thread_id, FSMState.DISMISSED, actor, "proposal_rejected"
                )
            self._record_receipt(
                conn,
                key,
                digest,
                "reject",
                thread_id,
                store.latest_event_id(thread_id, conn=conn),
            )
            return self._envelope(thread_id, conn, replayed=replayed)

    def accept(
        self,
        thread_id: str,
        *,
        client_mutation_id: str,
        expected_proposal_event_id: int,
        actor: str = "user",
    ) -> dict[str, Any]:
        thread_id, expected = (
            _thread_id(thread_id),
            _version(expected_proposal_event_id),
        )
        key = _text(client_mutation_id, "client mutation ID", maximum=512)
        actor = _text(actor, "human actor")
        if actor.casefold().split(":", 1)[0] in {
            "agent",
            "sidecar",
            "conductor",
            "fsm_engine",
            "inciting",
            "system",
            "service",
        }:
            raise ProposalError(
                "proposal_human_required",
                "Only the human review action can accept a proposal.",
                403,
            )
        digest = _hash(
            {"operation": "accept", "thread_id": thread_id, "expected": expected}
        )
        with self._transaction() as conn:
            receipt = self._receipt(conn, key, digest)
            projection = self._projection(thread_id, conn)
            self._reviewed(projection, expected)
            if projection["status"] == "rejected":
                raise ProposalError(
                    "proposal_rejected", "This proposal was rejected.", 409
                )
            replayed = receipt is not None or projection["status"] != "ready"
            if projection["status"] == "ready":
                self._append(
                    conn,
                    thread_id,
                    KIND_ACTION_APPROVED,
                    actor,
                    {"proposal_event_id": expected},
                )
                self._append(
                    conn,
                    thread_id,
                    KIND_ACTION_EXECUTION_INTENT,
                    actor,
                    {
                        "proposal_event_id": expected,
                        "name": "task_create",
                        "client_mutation_id": f"task-proposal:{thread_id}",
                        "parameters": projection["parameters"],
                        "parameters_sha256": _hash(projection["parameters"]),
                        "accept_client_mutation_id": key,
                    },
                )
                self._transition(
                    conn, thread_id, FSMState.EXECUTING, actor, "proposal_accepted"
                )
            if receipt is None:
                self._record_receipt(
                    conn,
                    key,
                    digest,
                    "accept",
                    thread_id,
                    store.latest_event_id(thread_id, conn=conn),
                )
            if projection["status"] == "realized":
                return self._envelope(thread_id, conn, replayed=True)
        # Commit approval + frozen intent before crossing the TaskStore boundary.
        result = self.reconcile(thread_id)
        result["replayed"] = replayed
        return result

    @staticmethod
    def _validate_realization(value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("task_id"), str)
            or not _TASK_ID.fullmatch(value["task_id"])
        ):
            raise ProposalError(
                "proposal_realization_unverifiable",
                "Task creation did not return a valid task reference.",
                503,
                retryable=True,
            )
        receipt = value.get("receipt_id")
        revision = value.get("task_revision")
        if (
            not isinstance(receipt, str)
            or not receipt
            or len(receipt) > 256
            or type(revision) is not int
            or revision < 1
        ):
            raise ProposalError(
                "proposal_realization_unverifiable",
                "Task creation did not return a durable receipt.",
                503,
                retryable=True,
            )
        return {
            "task_id": value["task_id"],
            "receipt_id": receipt,
            "task_revision": revision,
            "href": f"/app/tasks?task={value['task_id']}",
        }

    def _realization_from_result(self, result: Any, mutation_id: str) -> dict[str, Any]:
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        if (
            not isinstance(result, Mapping)
            or result.get("success") is False
            or result.get("ok") is False
        ):
            raise ProposalError(
                "proposal_execution_uncertain",
                "Task creation needs a safe retry.",
                503,
                retryable=True,
            )
        task = result.get("task") or {}
        receipt = result.get("receipt") or {}
        if (
            not isinstance(task, Mapping)
            or not isinstance(receipt, Mapping)
            or receipt.get("client_mutation_id")
            not in {mutation_id, f"{mutation_id}:document"}
            or receipt.get("status") != "completed"
        ):
            raise ProposalError(
                "proposal_realization_unverifiable",
                "Task creation did not return the expected receipt.",
                503,
                retryable=True,
            )
        return self._validate_realization(
            {
                "task_id": result.get("task_id") or task.get("task_id"),
                "task_revision": result.get("revision") or task.get("revision"),
                "receipt_id": receipt.get("receipt_id"),
            }
        )

    def reconcile(self, thread_id: str) -> dict[str, Any]:
        """Finish an already-approved intent; never approve or infer an action.

        A process death after TaskStore commit leaves the intent intact. Replaying
        exactly its parameters and deterministic key returns the original task
        receipt, then atomically records one realization. Concurrent workers may
        dispatch the same key; TaskStore's receipt transaction serializes them.
        """
        thread_id = _thread_id(thread_id)
        with self._transaction() as conn:
            projection = self._projection(thread_id, conn)
            if projection["status"] not in {"executing", "needs_attention"}:
                return self._envelope(
                    thread_id, conn, replayed=projection["status"] == "realized"
                )
            intent = self._latest(
                store.list_events(thread_id, conn=conn), KIND_ACTION_EXECUTION_INTENT
            )
            assert intent is not None
            parameters = dict(intent.data["parameters"])
            mutation_id = intent.data["client_mutation_id"]
            self._append(
                conn,
                thread_id,
                KIND_EXECUTION_STARTED,
                "sidecar",
                {
                    "intent_event_id": intent.id,
                    "capability_name": "task_create",
                    "client_mutation_id": mutation_id,
                },
            )
        try:
            from work_buddy.work_item.task_adapter import task_creation_attribution

            # The TaskStore receipt binds actor as well as request hash/key.
            # A sidecar retry must retain the original recorded human approver,
            # never borrow the recovery process's unrelated agent session.
            with task_creation_attribution(actor=intent.actor):
                result = self._executor(**parameters, client_mutation_id=mutation_id)
            realization = self._realization_from_result(result, mutation_id)
        except Exception as exc:  # noqa: BLE001 - any dispatch error can follow a commit
            # Do not expose arbitrary provider/SQL error text or task content.
            error = (
                exc
                if isinstance(exc, ProposalError)
                else ProposalError(
                    "proposal_execution_uncertain",
                    "Task creation could not be confirmed. Retry safely from this proposal.",
                    503,
                    retryable=True,
                )
            )
            logger.warning(
                "Task proposal execution needs reconciliation: %s (%s)",
                thread_id,
                type(exc).__name__,
            )
            with self._transaction() as conn:
                current = self._projection(thread_id, conn)
                if current["status"] != "realized":
                    self._append(
                        conn,
                        thread_id,
                        KIND_EXECUTION_FINISHED,
                        "sidecar",
                        {
                            "intent_event_id": intent.id,
                            "capability_name": "task_create",
                            "success": False,
                            "error": error.to_dict(),
                        },
                    )
                    self._transition(
                        conn,
                        thread_id,
                        FSMState.AWAITING_REDIRECT,
                        "sidecar",
                        "proposal_execution_uncertain",
                    )
                return self._envelope(thread_id, conn)
        with self._transaction() as conn:
            current = self._projection(thread_id, conn)
            if current["status"] == "realized":
                return self._envelope(thread_id, conn, replayed=True)
            if current["status"] == "unavailable":
                return self._envelope(thread_id, conn)
            self._append(
                conn,
                thread_id,
                KIND_EXECUTION_FINISHED,
                "sidecar",
                {
                    "intent_event_id": intent.id,
                    "capability_name": "task_create",
                    "success": True,
                },
            )
            self._append(
                conn,
                thread_id,
                KIND_ACTION_REALIZED,
                "sidecar",
                {
                    "intent_event_id": intent.id,
                    "proposal_event_id": intent.data["proposal_event_id"],
                    "realization": realization,
                },
            )
            self._append(
                conn,
                thread_id,
                KIND_THREAD_COMPLETED,
                "sidecar",
                {"realization": realization},
            )
            self._transition(
                conn, thread_id, FSMState.DONE, "sidecar", "proposal_realized"
            )
            return self._envelope(thread_id, conn)

    def reconcile_pending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Fair, bounded recovery for committed intents after a process restart.

        Select and durably rotate checked entries before dispatch, including
        malformed/unavailable ones. A permanent failure or process death must
        not keep later approved intents outside every bounded recovery batch.
        This event is scheduling metadata, never evidence of human acceptance.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT intent.thread_id, MAX(intent.id) AS intent_event_id, "
                "COALESCE((SELECT MAX(checked.id) FROM thread_events checked "
                "WHERE checked.thread_id = intent.thread_id AND checked.kind = ?), "
                "MIN(intent.id)) AS recovery_order FROM thread_events intent "
                "WHERE intent.kind = ? AND NOT EXISTS ("
                "SELECT 1 FROM thread_events done WHERE done.thread_id = intent.thread_id AND done.kind = ?) "
                "GROUP BY intent.thread_id ORDER BY recovery_order, intent.thread_id LIMIT ?",
                (
                    KIND_ACTION_RECOVERY_CHECKED,
                    KIND_ACTION_EXECUTION_INTENT,
                    KIND_ACTION_REALIZED,
                    max(1, min(limit, 100)),
                ),
            ).fetchall()
            for row in rows:
                self._append(
                    conn,
                    row["thread_id"],
                    KIND_ACTION_RECOVERY_CHECKED,
                    "sidecar",
                    {"intent_event_id": row["intent_event_id"]},
                )
        return [self.reconcile(row["thread_id"]) for row in rows]


def get_action_proposal_service() -> ActionProposalService:
    """Resolve the configured Thread store lazily (no import-time writes)."""
    return ActionProposalService()


__all__ = [
    "ActionProposalService",
    "ProposalError",
    "get_action_proposal_service",
    "is_managed_proposal",
    "validate_task_parameters",
]
