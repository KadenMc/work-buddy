"""Crash-recoverable coordination for task-plus-document creation.

The TaskStore is the coordinator authority.  A requested note is prepared in
the task-owned Co-work store under the deterministic decision identity from
this module.  The task row is published only after that store acknowledges
the same decision with a local admission seal.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .errors import TaskDomainError, TaskIdempotencyConflict, TaskValidationError
from .models import TaskDocumentLink
from .store import TaskStore


_NOTE_ROLE = "working_document/v1"
_TRUTH_POLICIES = frozenset({"disabled", "enabled"})
_AUTHORSHIP = frozenset({"human", "ai", "mixed", "unknown"})
_REVIEW_STATES = frozenset({"unreviewed", "accepted", "reviewed", "unknown"})


class TaskCreationIntentError(TaskDomainError):
    code = "task_creation_intent_error"


class TaskCreationDecisionVerificationError(TaskCreationIntentError):
    """A scoped document could not prove its published TaskStore decision."""

    code = "task_creation_decision_unverified"


@dataclass(frozen=True, slots=True)
class FieldDerivation:
    field_name: str
    value_sha256: str
    authorship: str
    review_state: str = "unreviewed"
    source_ref: str | None = None
    detail: Mapping[str, Any] | None = None

    def validate(self) -> None:
        if not self.field_name.strip():
            raise TaskValidationError({"field_derivations": "Field names are required."})
        if len(self.value_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.value_sha256
        ):
            raise TaskValidationError(
                {"field_derivations": "Value hashes must be lowercase SHA-256 strings."}
            )
        if self.authorship not in _AUTHORSHIP:
            raise TaskValidationError({"field_derivations": "Unknown authorship state."})
        if self.review_state not in _REVIEW_STATES:
            raise TaskValidationError({"field_derivations": "Unknown review state."})


@dataclass(frozen=True, slots=True)
class TaskCreationIntent:
    intent_id: str
    client_mutation_id: str
    task_id: str
    actor: str
    session_id: str | None
    request_hash: str
    status: str
    document_requested: bool
    truth_requested: bool
    coordinator_decision_id: str
    task_prepare_receipt_id: str | None = None
    store_id: str | None = None
    document_id: str | None = None
    binding_id: str | None = None
    document_content_sha256: str | None = None
    document_head_sha256: str | None = None
    document_provenance_sha256: str | None = None
    document_prepare_receipt_id: str | None = None
    document_admission_prepare_receipt_id: str | None = None
    interaction_contract_id: str | None = None
    interaction_contract_revision: int | None = None
    interaction_contract_digest: str | None = None
    activation_state: str | None = None
    activation_revision: int | None = None
    admission_receipt_id: str | None = None
    task_receipt_id: str | None = None
    decision_payload_json: str | None = None
    decision_sha256: str | None = None
    error_code: str | None = None
    recovery_attempts: int = 0
    last_recovery_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    decided_at: str | None = None
    admitted_at: str | None = None
    published_at: str | None = None
    aborted_at: str | None = None

    @property
    def coordinator_decision_sha256(self) -> str:
        if self.decision_sha256:
            return self.decision_sha256
        return self.provisional_coordinator_decision_sha256

    @property
    def provisional_coordinator_decision_sha256(self) -> str:
        """Digest used only to fence the initial pending scoped-store seal.

        It is deliberately distinguishable from the final v2 decision digest.
        The pending seal is rebound under CAS once every participant receipt is
        present; no Truth operation may treat this provisional digest as a
        published coordinator decision.
        """

        return _sha256_json(
            {
                "schema": "wb.task-creation-provisional-decision/v1",
                "intent_id": self.intent_id,
                "task_id": self.task_id,
                "request_hash": self.request_hash,
                "coordinator_decision_id": self.coordinator_decision_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["coordinator_decision_sha256"] = self.coordinator_decision_sha256
        value["provisional_coordinator_decision_sha256"] = (
            self.provisional_coordinator_decision_sha256
        )
        return value


@dataclass(frozen=True, slots=True)
class PublishedTaskCreationDecision:
    intent_id: str
    task_id: str
    coordinator_decision_id: str
    coordinator_decision_sha256: str
    task_receipt_id: str
    published_at: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(kind: str, *parts: str) -> str:
    payload = "\0".join((f"wb.{kind}/v1", *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class TaskCreationCoordinator:
    """Persist and advance one cross-store task creation state machine."""

    def __init__(
        self,
        store: TaskStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(
        self,
        *,
        client_mutation_id: str,
        task_id: str,
        actor: str,
        session_id: str | None,
        request: Mapping[str, Any],
        requested_note_role: str | None,
        requested_truth_policy_resolution: str | None,
    ) -> TaskCreationIntent:
        self._validate_request(
            client_mutation_id=client_mutation_id,
            task_id=task_id,
            actor=actor,
            requested_note_role=requested_note_role,
            requested_truth_policy_resolution=requested_truth_policy_resolution,
        )
        request_json = _canonical_json(dict(request))
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        intent_id = _stable_id("task-creation-intent", client_mutation_id)
        decision_id = _stable_id("task-creation-decision", intent_id, request_hash)
        task_prepare_receipt_id = _stable_id(
            "task-prepare-receipt",
            intent_id,
            task_id,
            actor,
            request_hash,
        )
        now = self._now()
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM task_creation_intents WHERE client_mutation_id = ?",
                (client_mutation_id,),
            ).fetchone()
            if prior is not None:
                if (
                    str(prior["request_hash"]) != request_hash
                    or str(prior["actor"]) != actor
                    or str(prior["task_id"]) != task_id
                ):
                    raise TaskIdempotencyConflict(client_mutation_id)
                return self._from_row(prior)
            # TaskStore transactions use BEGIN IMMEDIATE.  Checking the scalar
            # row here and inserting the intent below therefore reserves this
            # task ID atomically against ordinary task creation.
            if conn.execute(
                "SELECT 1 FROM task_metadata WHERE task_id=?",
                (task_id,),
            ).fetchone():
                raise TaskValidationError(
                    {"task_id": "That task ID already exists."}
                )
            conn.execute(
                """
                INSERT INTO task_creation_intents (
                    intent_id, client_mutation_id, task_id, actor, session_id,
                    request_hash, request_json, status, document_requested,
                    truth_requested, coordinator_decision_id,
                    task_prepare_receipt_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    client_mutation_id,
                    task_id,
                    actor,
                    session_id,
                    request_hash,
                    request_json,
                    int(requested_note_role is not None),
                    int(requested_truth_policy_resolution == "enabled"),
                    decision_id,
                    task_prepare_receipt_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_creation_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            assert row is not None
            return self._from_row(row)

    def record_document_prepared(
        self,
        intent_id: str,
        *,
        document: TaskDocumentLink,
        interaction_contract_id: str,
        interaction_contract_revision: int,
        interaction_contract_digest: str,
        activation_state: str,
        activation_revision: int | None,
        document_content_sha256: str,
        document_head_sha256: str,
        document_provenance_sha256: str,
        document_admission_prepare_receipt_id: str,
        document_prepare_receipt_id: str | None = None,
    ) -> TaskCreationIntent:
        for field_name, digest in (
            ("interaction_contract_digest", interaction_contract_digest),
            ("document_content_sha256", document_content_sha256),
            ("document_head_sha256", document_head_sha256),
            ("document_provenance_sha256", document_provenance_sha256),
        ):
            self._validate_digest(field_name, digest)
        if not document_admission_prepare_receipt_id.strip():
            raise TaskValidationError(
                {
                    "document_admission_prepare_receipt_id": (
                        "The scoped-store pending admission receipt is required."
                    )
                }
            )
        receipt_id = document_prepare_receipt_id or _stable_id(
            "task-document-prepare-receipt",
            intent_id,
            document.store_id,
            document.document_id,
            document.binding_id,
            document_content_sha256,
            document_head_sha256,
            document_provenance_sha256,
            interaction_contract_id,
            str(interaction_contract_revision),
            interaction_contract_digest,
            activation_state,
            str(activation_revision),
            document_admission_prepare_receipt_id,
        )
        now = self._now()
        with self.store.transaction() as conn:
            row = self._require(conn, intent_id)
            intent = self._from_row(row)
            if not intent.document_requested:
                raise TaskCreationIntentError("This creation intent did not request a note.")
            values = {
                "store_id": document.store_id,
                "document_id": document.document_id,
                "binding_id": document.binding_id,
                "document_content_sha256": document_content_sha256,
                "document_head_sha256": document_head_sha256,
                "document_provenance_sha256": document_provenance_sha256,
                "document_prepare_receipt_id": receipt_id,
                "document_admission_prepare_receipt_id": (
                    document_admission_prepare_receipt_id
                ),
                "interaction_contract_id": interaction_contract_id,
                "interaction_contract_revision": int(interaction_contract_revision),
                "interaction_contract_digest": interaction_contract_digest,
                "activation_state": activation_state,
                "activation_revision": activation_revision,
            }
            if intent.status != "prepared":
                self._assert_same(row, values)
                return intent
            conn.execute(
                """
                UPDATE task_creation_intents SET
                    status='document_prepared', store_id=?, document_id=?, binding_id=?,
                    document_content_sha256=?, document_head_sha256=?,
                    document_provenance_sha256=?, document_prepare_receipt_id=?,
                    document_admission_prepare_receipt_id=?,
                    interaction_contract_id=?, interaction_contract_revision=?,
                    interaction_contract_digest=?, activation_state=?, activation_revision=?,
                    updated_at=?
                WHERE intent_id=? AND status='prepared'
                """,
                (
                    *values.values(),
                    now,
                    intent_id,
                ),
            )
            return self._get_in_connection(conn, intent_id)

    def commit_decision(self, intent_id: str) -> TaskCreationIntent:
        now = self._now()
        with self.store.transaction() as conn:
            row = self._require(conn, intent_id)
            intent = self._from_row(row)
            if intent.status in {"decision_committed", "document_admitted", "published"}:
                self._assert_committed_decision(row)
                return intent
            expected = "document_prepared" if intent.document_requested else "prepared"
            if intent.status != expected:
                raise TaskCreationIntentError(
                    f"Creation decision cannot commit from {intent.status!r}."
                )
            if intent.document_requested:
                desired = "enabled" if intent.truth_requested else "disabled"
                if intent.activation_state != desired:
                    raise TaskCreationIntentError(
                        "The prepared document policy does not match the requested Truth setting."
                    )
                if not all(
                    (
                        intent.document_prepare_receipt_id,
                        intent.document_admission_prepare_receipt_id,
                        intent.document_content_sha256,
                        intent.document_head_sha256,
                        intent.document_provenance_sha256,
                        intent.interaction_contract_digest,
                    )
                ):
                    raise TaskCreationIntentError(
                        "The document participant has no complete prepared receipt."
                    )
            if not intent.task_prepare_receipt_id:
                raise TaskCreationIntentError(
                    "The task participant has no prepared receipt."
                )
            decision_payload = self._decision_payload(row)
            decision_payload_json = _canonical_json(decision_payload)
            decision_sha256 = hashlib.sha256(
                decision_payload_json.encode("utf-8")
            ).hexdigest()
            conn.execute(
                "UPDATE task_creation_intents SET status='decision_committed', "
                "decision_payload_json=?, coordinator_decision_sha256=?, "
                "decided_at=?, updated_at=? WHERE intent_id=? AND status=?",
                (
                    decision_payload_json,
                    decision_sha256,
                    now,
                    now,
                    intent_id,
                    expected,
                ),
            )
            return self._get_in_connection(conn, intent_id)

    def acknowledge_document_admission(
        self,
        intent_id: str,
        *,
        coordinator_decision_id: str,
        coordinator_decision_sha256: str,
        admission_receipt_id: str,
        activation_revision: int | None,
    ) -> TaskCreationIntent:
        if not admission_receipt_id.strip():
            raise TaskValidationError({"admission_receipt_id": "A receipt is required."})
        now = self._now()
        with self.store.transaction() as conn:
            row = self._require(conn, intent_id)
            intent = self._from_row(row)
            if coordinator_decision_id != intent.coordinator_decision_id:
                raise TaskCreationIntentError("The document acknowledged another decision.")
            self._assert_committed_decision(row)
            if coordinator_decision_sha256 != intent.coordinator_decision_sha256:
                raise TaskCreationIntentError(
                    "The document acknowledged another decision digest."
                )
            if activation_revision != intent.activation_revision:
                raise TaskCreationIntentError(
                    "The document admission changed its prepared activation revision."
                )
            if intent.status in {"document_admitted", "published"}:
                if intent.admission_receipt_id != admission_receipt_id:
                    raise TaskCreationIntentError("The admission receipt changed during replay.")
                return intent
            if intent.status != "decision_committed":
                raise TaskCreationIntentError(
                    f"Document admission cannot be recorded from {intent.status!r}."
                )
            conn.execute(
                "UPDATE task_creation_intents SET status='document_admitted', "
                "admission_receipt_id=?, admitted_at=?, updated_at=? "
                "WHERE intent_id=? AND status='decision_committed'",
                (admission_receipt_id, now, now, intent_id),
            )
            return self._get_in_connection(conn, intent_id)

    def acknowledge_no_document(self, intent_id: str) -> TaskCreationIntent:
        """Advance a scalar-only intent without inventing a document receipt."""

        committed = self.commit_decision(intent_id)
        if committed.document_requested:
            raise TaskCreationIntentError("A requested document needs a local admission seal.")
        now = self._now()
        with self.store.transaction() as conn:
            row = self._require(conn, intent_id)
            if row["status"] == "document_admitted":
                return self._from_row(row)
            conn.execute(
                "UPDATE task_creation_intents SET status='document_admitted', admitted_at=?, "
                "updated_at=? WHERE intent_id=? AND status='decision_committed'",
                (now, now, intent_id),
            )
            return self._get_in_connection(conn, intent_id)

    def get(self, intent_id: str) -> TaskCreationIntent | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_creation_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            conn.close()

    def recovery_queue(self) -> tuple[TaskCreationIntent, ...]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM task_creation_intents WHERE status NOT IN ('published','aborted') "
                "ORDER BY updated_at, intent_id"
            ).fetchall()
            return tuple(self._from_row(row) for row in rows)
        finally:
            conn.close()

    def record_recovery_attempt(
        self,
        intent_id: str,
        *,
        error_code: str | None = None,
        increment: bool = True,
    ) -> TaskCreationIntent:
        """Record bounded worker ownership without changing saga semantics."""

        now = self._now()
        with self.store.transaction() as conn:
            self._require(conn, intent_id)
            conn.execute(
                "UPDATE task_creation_intents SET recovery_attempts=recovery_attempts+?, "
                "last_recovery_at=?, error_code=?, updated_at=? WHERE intent_id=?",
                (int(increment), now, error_code, now, intent_id),
            )
            return self._get_in_connection(conn, intent_id)

    @staticmethod
    def publish_in_connection(
        conn: sqlite3.Connection,
        *,
        intent_id: str,
        task_id: str,
        actor: str,
        task_receipt_id: str,
        document: TaskDocumentLink | None,
        field_derivations: Sequence[FieldDerivation],
        now: str,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM task_creation_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise TaskCreationIntentError("Task creation intent was not found.")
        if row["status"] == "published":
            if row["task_receipt_id"] != task_receipt_id:
                raise TaskCreationIntentError("Published intent has another task receipt.")
            TaskCreationCoordinator._assert_committed_decision(row)
            return
        if row["status"] != "document_admitted":
            raise TaskCreationIntentError(
                f"Task cannot publish from creation state {row['status']!r}."
            )
        if str(row["task_id"]) != task_id or str(row["actor"]) != actor:
            raise TaskCreationIntentError("Task publication does not match its intent.")
        TaskCreationCoordinator._assert_committed_decision(row)
        requested = bool(row["document_requested"])
        if requested != (document is not None):
            raise TaskCreationIntentError("Task publication changed the requested note shape.")
        if document is not None and any(
            str(row[name]) != getattr(document, name)
            for name in ("store_id", "document_id", "binding_id")
        ):
            raise TaskCreationIntentError("Task publication references another document.")
        for derivation in field_derivations:
            derivation.validate()
            receipt_id = _stable_id(
                "task-field-derivation",
                intent_id,
                derivation.field_name,
                derivation.value_sha256,
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO task_field_derivation_receipts (
                    receipt_id, intent_id, task_id, field_name, value_sha256,
                    authorship, review_state, source_ref, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    intent_id,
                    task_id,
                    derivation.field_name,
                    derivation.value_sha256,
                    derivation.authorship,
                    derivation.review_state,
                    derivation.source_ref,
                    _canonical_json(dict(derivation.detail or {})),
                    now,
                ),
            )
            existing = conn.execute(
                "SELECT authorship,review_state,source_ref,detail_json "
                "FROM task_field_derivation_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            expected = (
                derivation.authorship,
                derivation.review_state,
                derivation.source_ref,
                _canonical_json(dict(derivation.detail or {})),
            )
            if existing is None or tuple(existing) != expected:
                raise TaskCreationIntentError(
                    "A field derivation receipt changed during replay."
                )
        conn.execute(
            "UPDATE task_creation_intents SET status='published', task_receipt_id=?, "
            "published_at=?, updated_at=? WHERE intent_id=? AND status='document_admitted'",
            (task_receipt_id, now, now, intent_id),
        )

    @staticmethod
    def _validate_request(
        *,
        client_mutation_id: str,
        task_id: str,
        actor: str,
        requested_note_role: str | None,
        requested_truth_policy_resolution: str | None,
    ) -> None:
        errors: dict[str, str] = {}
        if not client_mutation_id.strip():
            errors["client_mutation_id"] = "A client mutation ID is required."
        if not task_id.strip():
            errors["task_id"] = "A task ID is required."
        if not actor.strip():
            errors["actor"] = "An actor is required."
        if requested_note_role not in {None, _NOTE_ROLE}:
            errors["requested_note_role"] = "Unsupported task note role."
        if requested_note_role is None and requested_truth_policy_resolution is not None:
            errors["requested_truth_policy_resolution"] = "Truth requires a task note."
        if requested_note_role is not None and requested_truth_policy_resolution not in _TRUTH_POLICIES:
            errors["requested_truth_policy_resolution"] = (
                "A task note must resolve Truth to disabled or enabled."
            )
        if errors:
            raise TaskValidationError(errors)

    @staticmethod
    def _assert_same(row: sqlite3.Row, values: Mapping[str, Any]) -> None:
        if any(row[name] != value for name, value in values.items()):
            raise TaskCreationIntentError("Prepared document metadata changed during replay.")

    @staticmethod
    def _validate_digest(field_name: str, value: str) -> None:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise TaskValidationError(
                {field_name: "A lowercase SHA-256 digest is required."}
            )

    @staticmethod
    def _decision_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            request = json.loads(str(row["request_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskCreationIntentError(
                "The aggregate request is not canonical JSON."
            ) from exc
        if not isinstance(request, dict):
            raise TaskCreationIntentError("The aggregate request must be an object.")
        document_request = request.get("document")
        if isinstance(document_request, dict):
            derivations = document_request.get("field_derivations") or []
        else:
            derivations = request.get("field_derivations") or []
        if not isinstance(derivations, list):
            raise TaskCreationIntentError("The field derivation set is invalid.")
        document = None
        if bool(row["document_requested"]):
            document = {
                "prepare_receipt_id": row["document_prepare_receipt_id"],
                "admission_prepare_receipt_id": row[
                    "document_admission_prepare_receipt_id"
                ],
                "store_id": row["store_id"],
                "document_id": row["document_id"],
                "binding_id": row["binding_id"],
                "content_sha256": row["document_content_sha256"],
                "structured_head_sha256": row["document_head_sha256"],
                "provenance_sha256": row["document_provenance_sha256"],
                "interaction_contract": {
                    "id": row["interaction_contract_id"],
                    "revision": row["interaction_contract_revision"],
                    "digest": row["interaction_contract_digest"],
                },
                "truth_activation": {
                    "state": row["activation_state"],
                    "revision": row["activation_revision"],
                },
            }
        return {
            "schema": "wb.task-creation-decision/v2",
            "intent_id": str(row["intent_id"]),
            "coordinator_decision_id": str(row["coordinator_decision_id"]),
            "task": {
                "prepare_receipt_id": row["task_prepare_receipt_id"],
                "task_id": str(row["task_id"]),
                "actor": str(row["actor"]),
                "session_id": row["session_id"],
                "request_sha256": str(row["request_hash"]),
                "field_derivations_sha256": _sha256_json(derivations),
            },
            "document": document,
        }

    @staticmethod
    def _assert_committed_decision(row: sqlite3.Row) -> None:
        stored_payload = row["decision_payload_json"]
        stored_digest = row["coordinator_decision_sha256"]
        if not stored_payload or not stored_digest:
            raise TaskCreationIntentError(
                "The coordinator decision has no durable participant receipt digest."
            )
        expected_payload = _canonical_json(
            TaskCreationCoordinator._decision_payload(row)
        )
        if str(stored_payload) != expected_payload:
            raise TaskCreationIntentError(
                "The coordinator decision payload changed after commit."
            )
        expected_digest = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        if str(stored_digest) != expected_digest:
            raise TaskCreationIntentError(
                "The coordinator decision digest does not match its participants."
            )

    @staticmethod
    def _require(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM task_creation_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise TaskCreationIntentError("Task creation intent was not found.")
        if row["status"] in {"aborted", "recovery_required"}:
            raise TaskCreationIntentError(
                f"Task creation intent is {row['status'].replace('_', ' ')}."
            )
        return row

    @classmethod
    def _get_in_connection(
        cls,
        conn: sqlite3.Connection,
        intent_id: str,
    ) -> TaskCreationIntent:
        row = conn.execute(
            "SELECT * FROM task_creation_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        assert row is not None
        return cls._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskCreationIntent:
        return TaskCreationIntent(
            intent_id=str(row["intent_id"]),
            client_mutation_id=str(row["client_mutation_id"]),
            task_id=str(row["task_id"]),
            actor=str(row["actor"]),
            session_id=row["session_id"],
            request_hash=str(row["request_hash"]),
            status=str(row["status"]),
            document_requested=bool(row["document_requested"]),
            truth_requested=bool(row["truth_requested"]),
            coordinator_decision_id=str(row["coordinator_decision_id"]),
            task_prepare_receipt_id=row["task_prepare_receipt_id"],
            store_id=row["store_id"],
            document_id=row["document_id"],
            binding_id=row["binding_id"],
            document_content_sha256=row["document_content_sha256"],
            document_head_sha256=row["document_head_sha256"],
            document_provenance_sha256=row["document_provenance_sha256"],
            document_prepare_receipt_id=row["document_prepare_receipt_id"],
            document_admission_prepare_receipt_id=row[
                "document_admission_prepare_receipt_id"
            ],
            interaction_contract_id=row["interaction_contract_id"],
            interaction_contract_revision=row["interaction_contract_revision"],
            interaction_contract_digest=row["interaction_contract_digest"],
            activation_state=row["activation_state"],
            activation_revision=row["activation_revision"],
            admission_receipt_id=row["admission_receipt_id"],
            task_receipt_id=row["task_receipt_id"],
            decision_payload_json=row["decision_payload_json"],
            decision_sha256=row["coordinator_decision_sha256"],
            error_code=row["error_code"],
            recovery_attempts=int(row["recovery_attempts"]),
            last_recovery_at=row["last_recovery_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            decided_at=row["decided_at"],
            admitted_at=row["admitted_at"],
            published_at=row["published_at"],
            aborted_at=row["aborted_at"],
        )

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()


def verify_published_task_creation_decision(
    store: TaskStore,
    *,
    store_id: str,
    document_id: str,
    binding_id: str | None,
    coordinator_decision_id: str,
    coordinator_decision_sha256: str,
    task_id: str | None = None,
) -> PublishedTaskCreationDecision:
    """Verify one scoped-store admission against immutable TaskStore state.

    This is intentionally a query-only cross-store check.  It never creates or
    migrates the task database, and every absence or disagreement is an
    unavailable decision rather than evidence that a document was published.
    """

    supplied = {
        "store_id": str(store_id).strip(),
        "document_id": str(document_id).strip(),
        "coordinator_decision_id": str(coordinator_decision_id).strip(),
        "coordinator_decision_sha256": str(coordinator_decision_sha256).strip(),
    }
    if not all(supplied.values()):
        raise TaskCreationDecisionVerificationError(
            "Task creation decision identity is incomplete."
        )
    try:
        conn = store.connect_readonly()
    except (FileNotFoundError, OSError, sqlite3.Error) as exc:
        raise TaskCreationDecisionVerificationError(
            "TaskStore is unavailable for decision verification."
        ) from exc
    try:
        try:
            row = conn.execute(
                "SELECT * FROM task_creation_intents "
                "WHERE coordinator_decision_id=?",
                (supplied["coordinator_decision_id"],),
            ).fetchone()
        except sqlite3.Error as exc:
            raise TaskCreationDecisionVerificationError(
                "TaskStore has no published-decision ledger."
            ) from exc
        if row is None or str(row["status"]) != "published":
            raise TaskCreationDecisionVerificationError(
                "The task aggregate has not been published."
            )
        try:
            TaskCreationCoordinator._assert_committed_decision(row)
        except TaskCreationIntentError as exc:
            raise TaskCreationDecisionVerificationError(str(exc)) from exc
        expected_binding = None if row["binding_id"] is None else str(row["binding_id"])
        if any(
            (
                str(row["store_id"] or "") != supplied["store_id"],
                str(row["document_id"] or "") != supplied["document_id"],
                expected_binding != binding_id,
                str(row["coordinator_decision_sha256"] or "")
                != supplied["coordinator_decision_sha256"],
                task_id is not None and str(row["task_id"]) != str(task_id),
            )
        ):
            raise TaskCreationDecisionVerificationError(
                "The scoped document does not match the published task aggregate."
            )
        receipt_id = str(row["task_receipt_id"] or "").strip()
        published_at = str(row["published_at"] or "").strip()
        if not receipt_id or not published_at:
            raise TaskCreationDecisionVerificationError(
                "The published task receipt is incomplete."
            )
        task_row = conn.execute(
            "SELECT task_id FROM task_metadata WHERE task_id=?",
            (str(row["task_id"]),),
        ).fetchone()
        link = conn.execute(
            "SELECT task_id,store_id,document_id,binding_id "
            "FROM task_document_links WHERE task_id=?",
            (str(row["task_id"]),),
        ).fetchone()
        if task_row is None or link is None or any(
            (
                str(link["store_id"]) != supplied["store_id"],
                str(link["document_id"]) != supplied["document_id"],
                str(link["binding_id"]) != str(binding_id or ""),
            )
        ):
            raise TaskCreationDecisionVerificationError(
                "The published task/document binding is unavailable."
            )
        return PublishedTaskCreationDecision(
            intent_id=str(row["intent_id"]),
            task_id=str(row["task_id"]),
            coordinator_decision_id=supplied["coordinator_decision_id"],
            coordinator_decision_sha256=supplied[
                "coordinator_decision_sha256"
            ],
            task_receipt_id=receipt_id,
            published_at=published_at,
        )
    finally:
        conn.close()


__all__ = [
    "FieldDerivation",
    "PublishedTaskCreationDecision",
    "TaskCreationCoordinator",
    "TaskCreationDecisionVerificationError",
    "TaskCreationIntent",
    "TaskCreationIntentError",
    "verify_published_task_creation_decision",
]
