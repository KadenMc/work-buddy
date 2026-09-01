"""One recoverable owner for task-plus-Co-work-document creation.

The public adapters submit a canonical aggregate request here.  TaskStore owns
the intent and final publication decision; the scoped Co-work store owns the
document, binding, policy, and local admission seal.  Every step is idempotent,
so the maintenance worker can roll any committed or partially prepared intent
forward without exposing a partial task or duplicating a participant.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from work_buddy.document_kernel.causality import DocumentCausalityStore
from work_buddy.document_kernel.protocol import structured_head_sha256
from work_buddy.truth import documents as truth_documents
from work_buddy.truth import ydoc_store

from .creation import FieldDerivation, TaskCreationCoordinator, TaskCreationIntent
from .documents import TaskDocumentService
from .errors import TaskDomainError, TaskValidationError
from .models import MutationResult, Tag, TaskDocumentLink
from .service import TaskApplicationService
from .store import TaskStore


_REQUEST_SCHEMA = "wb.task-aggregate-create/v2"
_NOTE_ROLE = "working_document/v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Tag):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TaskValidationError(
        {"task": f"Aggregate task value {type(value).__name__} is not serializable."}
    )


def _normalize_tags(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values or ():
        if isinstance(value, Tag):
            result.append(value.to_dict())
        elif isinstance(value, Mapping):
            result.append(
                {
                    "name": str(value.get("name") or ""),
                    "is_namespace": bool(value.get("is_namespace", False)),
                }
            )
        elif isinstance(value, tuple) and len(value) == 2:
            result.append({"name": str(value[0]), "is_namespace": bool(value[1])})
        else:
            text = str(value)
            result.append({"name": text, "is_namespace": "/" in text})
    return result


def _rehydrate_tags(values: Any) -> tuple[Tag, ...]:
    result: list[Tag] = []
    for value in values or ():
        if not isinstance(value, Mapping):
            raise TaskDomainError("Stored aggregate task tags are invalid.")
        result.append(Tag(str(value.get("name") or ""), bool(value.get("is_namespace"))))
    return tuple(result)


def _rehydrate_derivations(values: Any) -> tuple[FieldDerivation, ...]:
    result: list[FieldDerivation] = []
    for value in values or ():
        if not isinstance(value, Mapping):
            raise TaskDomainError("Stored aggregate field derivations are invalid.")
        result.append(
            FieldDerivation(
                field_name=str(value.get("field_name") or ""),
                value_sha256=str(value.get("value_sha256") or ""),
                authorship=str(value.get("authorship") or ""),
                review_state=str(value.get("review_state") or "unreviewed"),
                source_ref=(
                    None if value.get("source_ref") is None else str(value["source_ref"])
                ),
                detail=(
                    dict(value["detail"])
                    if isinstance(value.get("detail"), Mapping)
                    else None
                ),
            )
        )
    return tuple(result)


class TaskAggregateCreationService:
    """Submit, resume, and recover a task/document aggregate."""

    def __init__(
        self,
        store: TaskStore,
        *,
        task_service: TaskApplicationService | None = None,
        document_service: TaskDocumentService | None = None,
    ) -> None:
        self.store = store
        self.task_service = task_service or TaskApplicationService(store)
        self.document_service = document_service or TaskDocumentService()
        self.coordinator = TaskCreationCoordinator(store)

    def create(
        self,
        *,
        client_mutation_id: str,
        actor: str,
        session_id: str | None,
        task_values: Mapping[str, Any],
        initial_note: str,
        requested_truth_policy_resolution: str = "disabled",
        field_derivations: Sequence[FieldDerivation] = (),
    ) -> MutationResult:
        values = dict(task_values)
        task_id = str(values.pop("task_id", "") or "").strip()
        if not task_id:
            task_id = "t-" + hashlib.sha256(
                client_mutation_id.encode("utf-8")
            ).hexdigest()[:12]
        values["tags"] = _normalize_tags(values.get("tags"))
        document = {
            "requested_note_role": _NOTE_ROLE,
            "requested_truth_policy_resolution": requested_truth_policy_resolution,
            "initial_markdown": str(initial_note),
            "field_derivations": [asdict(item) for item in field_derivations],
        }
        request = {
            "schema": _REQUEST_SCHEMA,
            "task": _json_value({**values, "task_id": task_id}),
            "document": _json_value(document),
        }
        create_values = dict(request["task"])
        create_values.pop("task_id", None)
        create_values["tags"] = _rehydrate_tags(create_values.get("tags"))
        try:
            self.task_service.validate_create(
                **create_values,
                task_id=task_id,
                client_mutation_id=client_mutation_id,
                actor=actor,
                session_id=session_id,
                creation_intent_id=f"preflight:{client_mutation_id}",
                field_derivations=tuple(field_derivations),
            )
        except TypeError as exc:
            raise TaskValidationError(
                {"task": "Aggregate task fields do not match the task create schema."}
            ) from exc
        intent = self.coordinator.prepare(
            client_mutation_id=client_mutation_id,
            task_id=task_id,
            actor=actor,
            session_id=session_id,
            request=request,
            requested_note_role=_NOTE_ROLE,
            requested_truth_policy_resolution=requested_truth_policy_resolution,
        )
        return self.resume(intent.intent_id)

    def resume(self, intent_id: str) -> MutationResult:
        intent = self.coordinator.get(intent_id)
        if intent is None:
            raise TaskDomainError("Task creation intent was not found.")
        request = self._request(intent)
        task_values = dict(request["task"])
        document_request = dict(request["document"])
        derivations = _rehydrate_derivations(document_request.get("field_derivations"))

        if intent.status == "prepared":
            created = self.document_service.create(
                task_id=intent.task_id,
                title=str(task_values.get("description") or ""),
                domain_revision=f"creation-intent:{intent.request_hash}",
                created_by=intent.actor,
                initial_markdown=str(document_request.get("initial_markdown") or ""),
                interaction_contract_id="working_document",
                initial_truth_activation=str(
                    document_request["requested_truth_policy_resolution"]
                ),
                explicit_truth_acknowledged=(
                    document_request["requested_truth_policy_resolution"] == "enabled"
                ),
                policy_intent_id=intent.intent_id,
                coordinator_decision_id=intent.coordinator_decision_id,
                coordinator_decision_sha256=(
                    intent.provisional_coordinator_decision_sha256
                ),
                commit_admission=False,
                task_admission_kind="aggregate_creation/v2",
            )
            prepared = self._prepared_document_values(
                intent,
                created=created,
                document_request=document_request,
                derivations=derivations,
            )
            intent = self.coordinator.record_document_prepared(
                intent.intent_id,
                document=prepared["link"],
                interaction_contract_id=prepared["interaction_contract_id"],
                interaction_contract_revision=prepared[
                    "interaction_contract_revision"
                ],
                interaction_contract_digest=prepared[
                    "interaction_contract_digest"
                ],
                activation_state=prepared["activation_state"],
                activation_revision=prepared["activation_revision"],
                document_content_sha256=prepared["document_content_sha256"],
                document_head_sha256=prepared["document_head_sha256"],
                document_provenance_sha256=prepared[
                    "document_provenance_sha256"
                ],
                document_admission_prepare_receipt_id=prepared[
                    "document_admission_prepare_receipt_id"
                ],
            )

        if intent.status == "document_prepared":
            intent = self.coordinator.commit_decision(intent.intent_id)

        if intent.status == "decision_committed":
            intent = self._admit_document(intent)

        link = self._link(intent)
        create_values = dict(task_values)
        create_values.pop("task_id", None)
        create_values["tags"] = _rehydrate_tags(create_values.get("tags"))
        return self.task_service.create(
            **create_values,
            task_id=intent.task_id,
            client_mutation_id=intent.client_mutation_id,
            actor=intent.actor,
            session_id=intent.session_id,
            creation_intent_id=intent.intent_id,
            initial_document=link,
            field_derivations=derivations,
        )

    def reconcile_pending(self, *, limit: int = 25) -> dict[str, Any]:
        """Boundedly roll every recoverable nonterminal intent forward."""

        bounded = max(1, min(int(limit), 100))
        queue = self.coordinator.recovery_queue()[:bounded]
        recovered: list[str] = []
        failed: list[dict[str, str]] = []
        skipped: list[str] = []
        for pending in queue:
            if pending.status == "recovery_required":
                skipped.append(pending.intent_id)
                continue
            self.coordinator.record_recovery_attempt(pending.intent_id)
            try:
                self.resume(pending.intent_id)
            except Exception as exc:
                code = str(getattr(exc, "code", type(exc).__name__))
                self.coordinator.record_recovery_attempt(
                    pending.intent_id,
                    error_code=code,
                    increment=False,
                )
                failed.append({"intent_id": pending.intent_id, "error_code": code})
            else:
                recovered.append(pending.intent_id)
        from .document_attachment import reconcile_linked_task_document_admissions

        attachments = reconcile_linked_task_document_admissions(
            self.store,
            self.document_service,
            limit=bounded,
        )
        return {
            "schema": "wb.task-creation-recovery/v1",
            "examined": len(queue),
            "recovered": recovered,
            "failed": failed,
            "skipped": skipped,
            "remaining": len(self.coordinator.recovery_queue()),
            "document_attachments": attachments,
        }

    def _request(self, intent: TaskCreationIntent) -> dict[str, Any]:
        # Read from the same canonical payload whose hash is frozen in TaskStore.
        # A private query is preferable to duplicating prose fields on the
        # public intent projection.
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT request_json FROM task_creation_intents WHERE intent_id=?",
                (intent.intent_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise TaskDomainError("Task creation intent was not found.")
        raw = str(row["request_json"])
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != intent.request_hash:
            raise TaskDomainError("Stored aggregate request hash does not match its intent.")
        try:
            request = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskDomainError("Stored aggregate request is invalid.") from exc
        if (
            not isinstance(request, dict)
            or request.get("schema") != _REQUEST_SCHEMA
            or not isinstance(request.get("task"), dict)
            or not isinstance(request.get("document"), dict)
        ):
            raise TaskDomainError("Stored aggregate request has an unsupported schema.")
        return request

    def _prepared_document_values(
        self,
        intent: TaskCreationIntent,
        *,
        created: Any,
        document_request: Mapping[str, Any],
        derivations: Sequence[FieldDerivation],
    ) -> dict[str, Any]:
        store = self.document_service.stores.open_existing()
        document = truth_documents.get_document(store, created.document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise TaskDomainError("Prepared task document has no durable snapshot.")
        snapshot = ydoc_store.read_snapshot(
            store,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        updates, _cursor = ydoc_store.read_updates(store, document_id=document.id)
        head = structured_head_sha256(snapshot, updates)
        causality = DocumentCausalityStore(store.paths.sidecar)
        binding = causality.get_binding(created.binding_id)
        if binding is None or any(
            (
                binding.domain_namespace != "tasks",
                binding.domain_kind != "task_knowledge",
                binding.domain_entity_id != intent.task_id,
                binding.role != "task_knowledge",
                binding.store_id != store.store_id,
                binding.document_id != document.id,
                binding.lifecycle != "current",
            )
        ):
            raise TaskDomainError("Prepared task document binding is invalid.")
        conn = store.connect()
        try:
            assignment = conn.execute(
                "SELECT interaction_contract_id,interaction_contract_version,"
                "interaction_contract_sha256 FROM "
                "document_interaction_contract_assignments WHERE document_id=?",
                (document.id,),
            ).fetchone()
            activation = conn.execute(
                "SELECT state,activation_revision FROM "
                "document_truth_activation_current WHERE document_id=?",
                (document.id,),
            ).fetchone()
            seal = conn.execute(
                "SELECT state,intent_id,seal_revision,coordinator_decision_id,"
                "event_id,"
                "coordinator_decision_sha256 FROM "
                "document_truth_admission_seals_current WHERE document_id=?",
                (document.id,),
            ).fetchone()
        finally:
            conn.close()
        if assignment is None or activation is None or seal is None:
            raise TaskDomainError("Prepared task document policy is incomplete.")
        if any(
            (
                str(seal["state"]) != "pending",
                str(seal["intent_id"]) != intent.intent_id,
                str(seal["coordinator_decision_id"])
                != intent.coordinator_decision_id,
                str(seal["coordinator_decision_sha256"])
                != intent.provisional_coordinator_decision_sha256,
            )
        ):
            raise TaskDomainError("Prepared task document seal is not provisional.")
        provenance = {
            "schema": "wb.task-document-initial-provenance/v1",
            "intent_id": intent.intent_id,
            "actor": intent.actor,
            "session_id": intent.session_id,
            "initial_markdown_sha256": hashlib.sha256(
                str(document_request.get("initial_markdown") or "").encode("utf-8")
            ).hexdigest(),
            "field_derivations": [asdict(item) for item in derivations],
        }
        link = TaskDocumentLink(
            task_id=intent.task_id,
            note_uuid=self._note_uuid(document.id),
            store_id=store.store_id,
            document_id=document.id,
            binding_id=binding.binding_id,
            lifecycle="active",
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )
        return {
            "link": link,
            "document_content_sha256": document.content_sha256,
            "document_head_sha256": head,
            "document_provenance_sha256": _sha256_json(provenance),
            "document_admission_prepare_receipt_id": str(seal["event_id"]),
            "interaction_contract_id": str(assignment["interaction_contract_id"]),
            "interaction_contract_revision": int(
                assignment["interaction_contract_version"]
            ),
            "interaction_contract_digest": str(
                assignment["interaction_contract_sha256"]
            ),
            "activation_state": str(activation["state"]),
            "activation_revision": int(activation["activation_revision"]),
        }

    def _admit_document(self, intent: TaskCreationIntent) -> TaskCreationIntent:
        from work_buddy.cowork.truth_activation import (
            bind_pending_document_admission_decision,
            commit_document_admission,
        )

        store = self.document_service.stores.open_existing()
        conn = store.connect()
        try:
            seal = conn.execute(
                "SELECT state,seal_revision,coordinator_decision_id,"
                "coordinator_decision_sha256 FROM "
                "document_truth_admission_seals_current WHERE document_id=?",
                (intent.document_id,),
            ).fetchone()
        finally:
            conn.close()
        if seal is None:
            raise TaskDomainError("Prepared task document admission seal is missing.")
        seal_state = str(seal["state"])
        seal_decision_id = str(seal["coordinator_decision_id"])
        seal_digest = str(seal["coordinator_decision_sha256"])
        if seal_state == "pending" and (
            seal_decision_id == intent.coordinator_decision_id
            and seal_digest == intent.provisional_coordinator_decision_sha256
        ):
            policy = bind_pending_document_admission_decision(
                store,
                document_id=str(intent.document_id),
                intent_id=intent.intent_id,
                expected_seal_revision=int(seal["seal_revision"]),
                provisional_coordinator_decision_id=intent.coordinator_decision_id,
                provisional_coordinator_decision_sha256=(
                    intent.provisional_coordinator_decision_sha256
                ),
                coordinator_decision_id=intent.coordinator_decision_id,
                coordinator_decision_sha256=intent.coordinator_decision_sha256,
                actor=intent.actor,
            )
        elif seal_state == "pending" and (
            seal_decision_id == intent.coordinator_decision_id
            and seal_digest == intent.coordinator_decision_sha256
        ):
            # Exact replay after the bind but before the terminal commit.
            policy = bind_pending_document_admission_decision(
                store,
                document_id=str(intent.document_id),
                intent_id=intent.intent_id,
                expected_seal_revision=max(1, int(seal["seal_revision"]) - 1),
                provisional_coordinator_decision_id=intent.coordinator_decision_id,
                provisional_coordinator_decision_sha256=(
                    intent.provisional_coordinator_decision_sha256
                ),
                coordinator_decision_id=intent.coordinator_decision_id,
                coordinator_decision_sha256=intent.coordinator_decision_sha256,
                actor=intent.actor,
            )
        elif seal_state == "committed" and (
            seal_decision_id == intent.coordinator_decision_id
            and seal_digest == intent.coordinator_decision_sha256
        ):
            from work_buddy.cowork.truth_activation import (
                resolve_document_truth_policy,
            )

            policy = resolve_document_truth_policy(store, str(intent.document_id))
        else:
            raise TaskDomainError(
                "The task document admission seal changed during recovery."
            )
        policy = commit_document_admission(
            store,
            document_id=str(intent.document_id),
            expected_seal_revision=int(policy.admission_seal_revision or 0),
            coordinator_decision_id=intent.coordinator_decision_id,
            coordinator_decision_sha256=intent.coordinator_decision_sha256,
            actor=intent.actor,
        )
        if policy.admission_state != "committed":
            raise TaskDomainError("The task document admission seal did not commit.")
        receipt = "task-note-admission:" + _sha256_json(
            {
                "intent_id": intent.intent_id,
                "document_id": intent.document_id,
                "coordinator_decision_id": intent.coordinator_decision_id,
                "coordinator_decision_sha256": intent.coordinator_decision_sha256,
                "activation_revision": intent.activation_revision,
            }
        )
        return self.coordinator.acknowledge_document_admission(
            intent.intent_id,
            coordinator_decision_id=intent.coordinator_decision_id,
            coordinator_decision_sha256=intent.coordinator_decision_sha256,
            admission_receipt_id=receipt,
            activation_revision=intent.activation_revision,
        )

    @staticmethod
    def _note_uuid(document_id: str) -> str:
        import uuid

        return str(uuid.UUID(hex=document_id))

    @staticmethod
    def _link(intent: TaskCreationIntent) -> TaskDocumentLink:
        required = (
            intent.store_id,
            intent.document_id,
            intent.binding_id,
        )
        if not all(required):
            raise TaskDomainError("Task aggregate has no prepared document link.")
        return TaskDocumentLink(
            task_id=intent.task_id,
            note_uuid=TaskAggregateCreationService._note_uuid(str(intent.document_id)),
            store_id=str(intent.store_id),
            document_id=str(intent.document_id),
            binding_id=str(intent.binding_id),
            lifecycle="active",
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )

def reconcile_task_creation_intents(*, limit: int = 25) -> dict[str, Any]:
    """Production maintenance entry point used by the scheduled owner."""

    return TaskAggregateCreationService(TaskStore()).reconcile_pending(limit=limit)


__all__ = [
    "TaskAggregateCreationService",
    "reconcile_task_creation_intents",
]
