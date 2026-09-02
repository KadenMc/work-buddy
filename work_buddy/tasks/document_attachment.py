"""Durable, crash-safe admission for documents attached to existing tasks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from work_buddy.cowork.truth_activation import (
    abort_document_admission,
    commit_document_admission,
    resolve_document_truth_policy,
)
from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.truth import documents as truth_documents
from work_buddy.truth.contracts import Actor

from .documents import TaskDocumentService
from .errors import (
    TaskDeletedError,
    TaskDomainError,
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskRevisionConflict,
)
from .models import MutationResult, TaskDocumentLink
from .service import TaskApplicationService
from .store import TaskStore


_REQUEST_SCHEMA = "wb.task-document-attachment-intent/v1"
_ORDER = {
    "prepared": 0,
    "document_prepared": 1,
    "linked": 2,
    "admitted": 3,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _stable_id(kind: str, value: Any) -> str:
    return hashlib.sha256(
        f"wb.{kind}/v1\0{_canonical_json(value)}".encode("utf-8")
    ).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class TaskDocumentAttachmentDecision:
    intent_id: str
    status: str
    task_id: str
    client_mutation_id: str
    expected_task_revision: int
    actor: str
    title: str
    domain_revision: str
    generation: str
    store_id: str
    document_id: str
    binding_id: str
    note_uuid: str
    link_created_at: str
    coordinator_decision_id: str
    coordinator_decision_sha256: str
    task_receipt_id: str | None = None
    recovery_attempts: int = 0
    error_code: str | None = None

    def link(self) -> TaskDocumentLink:
        return TaskDocumentLink(
            task_id=self.task_id,
            note_uuid=self.note_uuid,
            store_id=self.store_id,
            document_id=self.document_id,
            binding_id=self.binding_id,
            lifecycle="active",
            created_at=self.link_created_at,
            updated_at=self.link_created_at,
        )


@dataclass(frozen=True, slots=True)
class TaskDocumentAttachmentOutcome:
    intent: TaskDocumentAttachmentDecision
    result: MutationResult | None


class TaskDocumentAttachmentCoordinator:
    """Own the TaskStore half of one existing-task attachment state machine."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def prepare(
        self,
        service: TaskDocumentService,
        *,
        task_id: str,
        client_mutation_id: str,
        expected_task_revision: int,
        actor: str,
        title: str,
    ) -> TaskDocumentAttachmentDecision:
        """Persist the full request before the first document reservation."""

        if not all(
            isinstance(value, str) and value.strip()
            for value in (task_id, client_mutation_id, actor, title)
        ):
            raise TaskDomainError("Task document attachment fields must be non-empty.")
        if isinstance(expected_task_revision, bool) or int(expected_task_revision) < 1:
            raise TaskDomainError("Task document attachment revision is invalid.")

        # Store bootstrap may precede the intent, but document/binding reservation
        # never does. A crash here has created no domain document to reconcile.
        cowork = service.stores.ensure()
        generation = f"attach:{client_mutation_id}"
        identity_ref = f"{task_id}\0{generation}"
        document_id = service.document_id(identity_ref)
        binding_id = service.binding_id(
            task_id=task_id,
            store_id=cowork.store_id,
            document_id=document_id,
        )
        requested = {
            "task_id": task_id,
            "client_mutation_id": client_mutation_id,
            "expected_task_revision": int(expected_task_revision),
            "actor": actor,
            "title": title,
            "domain_revision": str(expected_task_revision),
            "generation": generation,
            "store_id": cowork.store_id,
            "document_id": document_id,
            "binding_id": binding_id,
            "note_uuid": str(uuid.UUID(hex=document_id)),
            "interaction_contract_id": "working_document",
            "truth_activation": "disabled",
            "task_admission_kind": "document_attachment/v1",
        }
        intent_id = _stable_id(
            "task-document-attachment-intent",
            {"client_mutation_id": client_mutation_id},
        )
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM task_document_attachment_intents "
                "WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                existing = self._from_row(prior)
                if any(
                    (
                        existing.task_id != task_id,
                        existing.expected_task_revision != int(expected_task_revision),
                        existing.actor != actor,
                        existing.title != title,
                        existing.domain_revision != str(expected_task_revision),
                        existing.generation != generation,
                        existing.store_id != cowork.store_id,
                        existing.document_id != document_id,
                        existing.binding_id != binding_id,
                    )
                ):
                    raise TaskIdempotencyConflict(client_mutation_id)
                return existing

            task = self.store.get_in_connection(conn, task_id, include_deleted=True)
            if task is None:
                raise TaskNotFound(task_id)
            if task.revision != int(expected_task_revision):
                raise TaskRevisionConflict(
                    expected=int(expected_task_revision),
                    current=task.revision,
                    current_task=task.to_dict(),
                )
            if task.deleted_at is not None:
                raise TaskDeletedError(task_id)
            if conn.execute(
                "SELECT 1 FROM task_document_links WHERE task_id=?", (task_id,)
            ).fetchone() is not None:
                raise TaskDomainError("This task already has a knowledge document.")
            if conn.execute(
                "SELECT 1 FROM task_mutation_receipts WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone() is not None:
                raise TaskIdempotencyConflict(client_mutation_id)

            timestamp = _now()
            decision_id = _stable_id(
                "task-document-attachment-decision",
                {
                    **requested,
                    "intent_id": intent_id,
                    "link_created_at": timestamp,
                },
            )
            payload = {
                "schema": _REQUEST_SCHEMA,
                **requested,
                "intent_id": intent_id,
                "link_created_at": timestamp,
                "coordinator_decision_id": decision_id,
            }
            payload_json = _canonical_json(payload)
            digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            try:
                conn.execute(
                    """
                    INSERT INTO task_document_attachment_intents (
                        intent_id, client_mutation_id, task_id,
                        expected_task_revision, actor, request_hash, request_json,
                        status, title, domain_revision, generation, store_id,
                        document_id, binding_id, note_uuid, link_created_at,
                        coordinator_decision_id, coordinator_decision_sha256,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        client_mutation_id,
                        task_id,
                        int(expected_task_revision),
                        actor,
                        digest,
                        payload_json,
                        title,
                        str(expected_task_revision),
                        generation,
                        cowork.store_id,
                        document_id,
                        binding_id,
                        str(uuid.UUID(hex=document_id)),
                        timestamp,
                        decision_id,
                        digest,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                active = conn.execute(
                    "SELECT client_mutation_id FROM "
                    "task_document_attachment_intents WHERE task_id=? "
                    "AND status IN ('prepared','document_prepared','linked',"
                    "'recovery_required')",
                    (task_id,),
                ).fetchone()
                if active is not None:
                    raise TaskDomainError(
                        "This task already has a document attachment in progress."
                    ) from exc
                raise
            return self._require(conn, intent_id)

    def get(self, intent_id: str) -> TaskDocumentAttachmentDecision | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_document_attachment_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            conn.close()

    def get_by_client_mutation(
        self, client_mutation_id: str
    ) -> TaskDocumentAttachmentDecision | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_document_attachment_intents "
                "WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            conn.close()

    def recovery_queue(
        self, *, limit: int = 100
    ) -> tuple[TaskDocumentAttachmentDecision, ...]:
        bounded = max(1, min(int(limit), 500))
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM task_document_attachment_intents "
                "WHERE status IN ('prepared','document_prepared','linked',"
                "'recovery_required') ORDER BY updated_at,intent_id LIMIT ?",
                (bounded,),
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)
        finally:
            conn.close()

    def record_document_prepared(
        self, intent_id: str
    ) -> TaskDocumentAttachmentDecision:
        return self._advance(
            intent_id,
            target="document_prepared",
            allowed={"prepared"},
            timestamp_column="document_prepared_at",
        )

    def record_linked(
        self, intent_id: str, *, task_receipt_id: str
    ) -> TaskDocumentAttachmentDecision:
        if not task_receipt_id.strip():
            raise TaskDomainError("Task attachment receipt is required.")
        now = _now()
        with self.store.transaction() as conn:
            current = self._require(conn, intent_id)
            if current.status in {"linked", "admitted"}:
                if current.task_receipt_id != task_receipt_id:
                    raise TaskDomainError(
                        "Task attachment receipt changed during replay."
                    )
                return current
            if current.status != "document_prepared":
                raise TaskDomainError(
                    f"Task attachment cannot link from {current.status!r}."
                )
            conn.execute(
                "UPDATE task_document_attachment_intents SET status='linked',"
                "task_receipt_id=?,linked_at=?,updated_at=?,error_code=NULL "
                "WHERE intent_id=? AND status='document_prepared'",
                (task_receipt_id, now, now, intent_id),
            )
            return self._require(conn, intent_id)

    def record_admitted(self, intent_id: str) -> TaskDocumentAttachmentDecision:
        return self._advance(
            intent_id,
            target="admitted",
            allowed={"linked"},
            timestamp_column="admitted_at",
        )

    def record_aborted(
        self, intent_id: str, *, error_code: str
    ) -> TaskDocumentAttachmentDecision:
        now = _now()
        with self.store.transaction() as conn:
            current = self._require(conn, intent_id)
            if current.status == "aborted":
                return current
            if current.status not in {"prepared", "document_prepared"}:
                raise TaskDomainError(
                    f"Task attachment cannot abort from {current.status!r}."
                )
            conn.execute(
                "UPDATE task_document_attachment_intents SET status='aborted',"
                "error_code=?,aborted_at=?,updated_at=? WHERE intent_id=? "
                "AND status IN ('prepared','document_prepared')",
                (error_code, now, now, intent_id),
            )
            return self._require(conn, intent_id)

    def record_recovery_attempt(
        self,
        intent_id: str,
        *,
        error_code: str | None = None,
        increment: bool = True,
    ) -> TaskDocumentAttachmentDecision:
        now = _now()
        with self.store.transaction() as conn:
            self._require(conn, intent_id)
            conn.execute(
                "UPDATE task_document_attachment_intents SET "
                "recovery_attempts=recovery_attempts+?,last_recovery_at=?,"
                "error_code=?,updated_at=? WHERE intent_id=?",
                (int(increment), now, error_code, now, intent_id),
            )
            return self._require(conn, intent_id)

    def _advance(
        self,
        intent_id: str,
        *,
        target: str,
        allowed: set[str],
        timestamp_column: str,
    ) -> TaskDocumentAttachmentDecision:
        now = _now()
        with self.store.transaction() as conn:
            current = self._require(conn, intent_id)
            if current.status in _ORDER and _ORDER[current.status] >= _ORDER[target]:
                return current
            if current.status not in allowed:
                raise TaskDomainError(
                    f"Task attachment cannot enter {target!r} from "
                    f"{current.status!r}."
                )
            conn.execute(
                f"UPDATE task_document_attachment_intents SET status=?,"
                f"{timestamp_column}=?,updated_at=?,error_code=NULL "
                "WHERE intent_id=? AND status=?",
                (target, now, now, intent_id, current.status),
            )
            return self._require(conn, intent_id)

    def _require(
        self, conn: sqlite3.Connection, intent_id: str
    ) -> TaskDocumentAttachmentDecision:
        row = conn.execute(
            "SELECT * FROM task_document_attachment_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise TaskDomainError("Task document attachment intent was not found.")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskDocumentAttachmentDecision:
        raw = str(row["request_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if (
            digest != str(row["request_hash"])
            or digest != str(row["coordinator_decision_sha256"])
        ):
            raise TaskDomainError("Task document attachment intent digest is invalid.")
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskDomainError(
                "Task document attachment intent payload is invalid."
            ) from exc
        expected = {
            "schema": _REQUEST_SCHEMA,
            "intent_id": str(row["intent_id"]),
            "task_id": str(row["task_id"]),
            "client_mutation_id": str(row["client_mutation_id"]),
            "expected_task_revision": int(row["expected_task_revision"]),
            "actor": str(row["actor"]),
            "title": str(row["title"]),
            "domain_revision": str(row["domain_revision"]),
            "generation": str(row["generation"]),
            "store_id": str(row["store_id"]),
            "document_id": str(row["document_id"]),
            "binding_id": str(row["binding_id"]),
            "note_uuid": str(row["note_uuid"]),
            "link_created_at": str(row["link_created_at"]),
            "coordinator_decision_id": str(row["coordinator_decision_id"]),
            "interaction_contract_id": "working_document",
            "truth_activation": "disabled",
            "task_admission_kind": "document_attachment/v1",
        }
        if payload != expected:
            raise TaskDomainError("Task document attachment intent payload changed.")
        return TaskDocumentAttachmentDecision(
            intent_id=str(row["intent_id"]),
            status=str(row["status"]),
            task_id=str(row["task_id"]),
            client_mutation_id=str(row["client_mutation_id"]),
            expected_task_revision=int(row["expected_task_revision"]),
            actor=str(row["actor"]),
            title=str(row["title"]),
            domain_revision=str(row["domain_revision"]),
            generation=str(row["generation"]),
            store_id=str(row["store_id"]),
            document_id=str(row["document_id"]),
            binding_id=str(row["binding_id"]),
            note_uuid=str(row["note_uuid"]),
            link_created_at=str(row["link_created_at"]),
            coordinator_decision_id=str(row["coordinator_decision_id"]),
            coordinator_decision_sha256=str(
                row["coordinator_decision_sha256"]
            ),
            task_receipt_id=(
                str(row["task_receipt_id"])
                if row["task_receipt_id"] is not None
                else None
            ),
            recovery_attempts=int(row["recovery_attempts"]),
            error_code=(
                str(row["error_code"]) if row["error_code"] is not None else None
            ),
        )


def prepare_task_document_attachment(
    task_store: TaskStore,
    service: TaskDocumentService,
    *,
    task_id: str,
    client_mutation_id: str,
    expected_task_revision: int,
    actor: str,
    title: str,
) -> TaskDocumentAttachmentDecision:
    """Freeze and persist one attachment before reserving its document."""

    return TaskDocumentAttachmentCoordinator(task_store).prepare(
        service,
        task_id=task_id,
        client_mutation_id=client_mutation_id,
        expected_task_revision=expected_task_revision,
        actor=actor,
        title=title,
    )


def abort_unlinked_task_document_attachment(
    service: TaskDocumentService,
    decision: TaskDocumentAttachmentDecision,
    *,
    actor: str,
) -> None:
    """Abort and retire a reserved document that did not win TaskStore CAS."""

    cowork = service.stores.open_existing()
    policy = resolve_document_truth_policy(cowork, decision.document_id)
    if policy.admission_state == "committed":
        raise RuntimeError("task_document_attachment_orphan_already_committed")
    if policy.admission_state == "pending":
        policy = abort_document_admission(
            cowork,
            document_id=decision.document_id,
            expected_seal_revision=int(policy.admission_seal_revision or 1),
            coordinator_decision_id=decision.coordinator_decision_id,
            coordinator_decision_sha256=decision.coordinator_decision_sha256,
            actor=actor,
        )
    if policy.admission_state != "aborted":
        raise RuntimeError("task_document_attachment_abort_incomplete")

    causality = DocumentCausalityStore(cowork.paths.sidecar)
    binding = causality.get_binding(decision.binding_id)
    if binding is None or any(
        (
            binding.domain_namespace != "tasks",
            binding.domain_kind != "task_knowledge",
            binding.domain_entity_id != decision.task_id,
            binding.role != "task_knowledge",
            binding.store_id != decision.store_id,
            binding.document_id != decision.document_id,
        )
    ):
        raise RuntimeError("task_document_attachment_binding_mismatch")
    # Retire the uniqueness-bearing binding first. A crash between the two
    # append-only operations cannot block a successor, and the document is
    # already admission-aborted.
    if binding.lifecycle == "current":
        causality.retire_binding(binding.binding_id)
    elif binding.lifecycle != "retired":
        raise RuntimeError("task_document_attachment_binding_lifecycle_invalid")
    if truth_documents.current_lifecycle(cowork, decision.document_id) == "active":
        truth_documents.retire_document(
            cowork,
            document_id=decision.document_id,
            actor=Actor(
                kind="system",
                ref=f"task-document-attachment-abort:{actor}",
            ),
        )


class TaskDocumentAttachmentRunner:
    """Resume a durable attachment through every cross-store crash boundary."""

    def __init__(
        self,
        task_store: TaskStore,
        document_service: TaskDocumentService,
        *,
        task_service: TaskApplicationService | None = None,
    ) -> None:
        self.store = task_store
        self.document_service = document_service
        self.task_service = task_service or TaskApplicationService(task_store)
        self.coordinator = TaskDocumentAttachmentCoordinator(task_store)

    def resume(self, intent_id: str) -> TaskDocumentAttachmentOutcome:
        intent = self.coordinator.get(intent_id)
        if intent is None:
            raise TaskDomainError("Task document attachment intent was not found.")
        result: MutationResult | None = None
        if intent.status in {"admitted", "aborted"}:
            return TaskDocumentAttachmentOutcome(intent, None)
        if intent.status == "recovery_required":
            raise TaskDomainError("Task document attachment requires operator recovery.")

        if intent.status == "prepared":
            created = self.document_service.create(
                task_id=intent.task_id,
                title=intent.title,
                domain_revision=intent.domain_revision,
                created_by=intent.actor,
                document_generation=intent.generation,
                interaction_contract_id="working_document",
                initial_truth_activation="disabled",
                policy_intent_id=intent.intent_id,
                coordinator_decision_id=intent.coordinator_decision_id,
                coordinator_decision_sha256=intent.coordinator_decision_sha256,
                commit_admission=False,
                task_admission_kind="document_attachment/v1",
            )
            if any(
                (
                    created.store_id != intent.store_id,
                    created.document_id != intent.document_id,
                    created.binding_id != intent.binding_id,
                )
            ):
                raise TaskDomainError(
                    "The reserved task document changed attachment identity."
                )
            intent = self.coordinator.record_document_prepared(intent.intent_id)

        if intent.status == "document_prepared":
            link = intent.link()
            current_link = self.store.get_task_document_link(intent.task_id)
            if current_link is not None and current_link != link:
                self._abort(intent, "task_document_link_conflict")
                raise TaskDomainError(
                    "This task acquired another knowledge document."
                )
            try:
                result = self.task_service.attach_document(
                    intent.task_id,
                    link=link,
                    expected_revision=intent.expected_task_revision,
                    client_mutation_id=intent.client_mutation_id,
                    actor=intent.actor,
                )
            except Exception as exc:
                current_task = self.store.get(intent.task_id, include_deleted=True)
                current_link = self.store.get_task_document_link(intent.task_id)
                deterministic_conflict = (
                    isinstance(
                        exc,
                        (
                            TaskRevisionConflict,
                            TaskDeletedError,
                            TaskNotFound,
                            TaskIdempotencyConflict,
                        ),
                    )
                    or current_task is None
                    or current_task.deleted_at is not None
                    or current_task.revision != intent.expected_task_revision
                    or (current_link is not None and current_link != link)
                )
                if deterministic_conflict and current_link != link:
                    self._abort(
                        intent,
                        str(getattr(exc, "code", type(exc).__name__)),
                    )
                raise
            exact_link = self.store.get_task_document_link(intent.task_id)
            if exact_link != link:
                raise TaskDomainError(
                    "Task document attachment receipt has no exact link."
                )
            intent = self.coordinator.record_linked(
                intent.intent_id,
                task_receipt_id=result.receipt.receipt_id,
            )

        if intent.status == "linked":
            link = intent.link()
            if self.store.get_task_document_link(intent.task_id) != link:
                raise TaskDomainError("Linked task document identity changed.")
            self._validate_linked_document(intent)
            cowork = self.document_service.stores.open_existing()
            policy = resolve_document_truth_policy(cowork, intent.document_id)
            if policy.admission_state == "pending":
                policy = commit_document_admission(
                    cowork,
                    document_id=intent.document_id,
                    expected_seal_revision=int(policy.admission_seal_revision or 1),
                    coordinator_decision_id=intent.coordinator_decision_id,
                    coordinator_decision_sha256=(
                        intent.coordinator_decision_sha256
                    ),
                    actor=intent.actor,
                )
            if policy.admission_state != "committed":
                raise TaskDomainError(
                    "The linked task document admission did not commit."
                )
            intent = self.coordinator.record_admitted(intent.intent_id)

        return TaskDocumentAttachmentOutcome(intent, result)

    def _abort(self, intent: TaskDocumentAttachmentDecision, error_code: str) -> None:
        abort_unlinked_task_document_attachment(
            self.document_service,
            intent,
            actor=intent.actor,
        )
        self.coordinator.record_aborted(intent.intent_id, error_code=error_code)

    def _validate_linked_document(
        self, intent: TaskDocumentAttachmentDecision
    ) -> None:
        cowork = self.document_service.stores.open_existing()
        if cowork.store_id != intent.store_id:
            raise TaskDomainError("Task document store identity changed.")
        document = truth_documents.get_document(cowork, intent.document_id)
        try:
            meta = json.loads(document.meta_json or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskDomainError("Task document admission marker is invalid.") from exc
        expected_marker = {
            "schema": "wb.task-document-admission/v1",
            "kind": "document_attachment/v1",
            "task_id": intent.task_id,
        }
        if not isinstance(meta, dict) or meta.get("task_admission") != expected_marker:
            raise TaskDomainError("Task document admission marker changed.")
        binding = DocumentCausalityStore(cowork.paths.sidecar).get_binding(
            intent.binding_id
        )
        if binding is None or any(
            (
                binding.domain_namespace != "tasks",
                binding.domain_kind != "task_knowledge",
                binding.domain_entity_id != intent.task_id,
                binding.role != "task_knowledge",
                binding.store_id != intent.store_id,
                binding.document_id != intent.document_id,
                binding.lifecycle != "current",
            )
        ):
            raise TaskDomainError("Task document attachment binding changed.")


def reconcile_task_document_attachment_intents(
    task_store: TaskStore,
    document_service: TaskDocumentService,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Boundedly resume every durable nonterminal attachment intent."""

    bounded = max(1, min(int(limit), 500))
    coordinator = TaskDocumentAttachmentCoordinator(task_store)
    runner = TaskDocumentAttachmentRunner(task_store, document_service)
    queue = coordinator.recovery_queue(limit=bounded)
    admitted: list[str] = []
    aborted: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[str] = []
    for pending in queue:
        if pending.status == "recovery_required":
            skipped.append(pending.intent_id)
            continue
        coordinator.record_recovery_attempt(pending.intent_id)
        try:
            outcome = runner.resume(pending.intent_id)
        except Exception as exc:
            current = coordinator.get(pending.intent_id)
            code = str(getattr(exc, "code", type(exc).__name__))
            if current is not None and current.status == "aborted":
                aborted.append(current.intent_id)
                continue
            coordinator.record_recovery_attempt(
                pending.intent_id,
                error_code=code,
                increment=False,
            )
            failed.append({"intent_id": pending.intent_id, "error_code": code})
        else:
            if outcome.intent.status == "admitted":
                admitted.append(outcome.intent.intent_id)
            elif outcome.intent.status == "aborted":
                aborted.append(outcome.intent.intent_id)
            else:
                failed.append(
                    {
                        "intent_id": outcome.intent.intent_id,
                        "error_code": "task_document_attachment_incomplete",
                    }
                )
    remaining = len(coordinator.recovery_queue(limit=500))
    return {
        "schema": "wb.task-document-attachment-recovery/v1",
        "examined": len(queue),
        "admitted": admitted,
        "aborted": aborted,
        "failed": failed,
        "skipped": skipped,
        "remaining": remaining,
    }


def reconcile_linked_task_document_admissions(
    task_store: TaskStore,
    document_service: TaskDocumentService,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Compatibility name for the durable attachment-intent reconciler."""

    return reconcile_task_document_attachment_intents(
        task_store,
        document_service,
        limit=limit,
    )


__all__ = [
    "TaskDocumentAttachmentCoordinator",
    "TaskDocumentAttachmentDecision",
    "TaskDocumentAttachmentOutcome",
    "TaskDocumentAttachmentRunner",
    "abort_unlinked_task_document_attachment",
    "prepare_task_document_attachment",
    "reconcile_linked_task_document_admissions",
    "reconcile_task_document_attachment_intents",
]
