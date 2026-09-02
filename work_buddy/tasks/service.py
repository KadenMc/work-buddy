"""Transactional application service for SQLite-authoritative tasks."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from .creation import FieldDerivation, TaskCreationCoordinator
from .errors import (
    TaskAuthorityUnavailable,
    TaskDeletedError,
    TaskDomainError,
    TaskIdempotencyConflict,
    TaskMutationFenced,
    TaskNotFound,
    TaskRevisionConflict,
    TaskTransitionError,
    TaskValidationError,
)
from .events import invalidation_payload
from .models import (
    BatchMutationResult,
    MutationReceipt,
    MutationResult,
    Tag,
    Task,
    TaskDocumentLink,
    TaskQuery,
    VALID_ATTENTION_STATES,
    VALID_COMPLEXITIES,
    VALID_URGENCIES,
)
from .store import TaskStore

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]*$", re.IGNORECASE)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACTION_STATES = frozenset({"pending", "in_progress", "done", "skipped"})
_AUTHORSHIP = frozenset({"user", "agent_approved", "agent_unapproved"})
_TASK_KINDS = frozenset({"task", "periodic", "habit"})
_DENSITIES = frozenset({"sparse", "developed", "dense"})
_CREATION_EFFORTS = frozenset({"sparse", "medium", "developed"})
_USER_INVOLVEMENTS = frozenset({"low", "medium", "high"})
_CONTEXT_SOURCES = frozenset({"agent_inferred", "user_authored"})


@dataclass(frozen=True, slots=True)
class _Change:
    task_id: str
    old_state: str | None
    new_state: str
    changed: bool
    details: dict[str, Any]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedCreate:
    fields: dict[str, Any]
    dependencies: tuple[str, ...]
    tags: tuple[Tag, ...]
    effective_task_id: str
    derivations: tuple[FieldDerivation, ...]
    request: dict[str, Any]


class TaskApplicationService:
    """Only supported write boundary for native tasks.

    Each successful change increments the entity revision and the monotonic
    collection revision, then appends history and an outbox event in the same
    SQLite transaction.  A completed mutation receipt makes response-loss
    retries deterministic.
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        clock: Callable[[], datetime | str] | None = None,
        id_factory: Callable[[], str] | None = None,
        receipt_id_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: "t-" + uuid.uuid4().hex[:8])
        self._receipt_id_factory = receipt_id_factory or (
            lambda: "tmr_" + uuid.uuid4().hex
        )
        self._event_id_factory = event_id_factory or (
            lambda: "te_" + uuid.uuid4().hex
        )

    # -- reads -----------------------------------------------------

    def get(self, task_id: str, *, include_deleted: bool = False) -> Task | None:
        return self.store.get(task_id, include_deleted=include_deleted)

    def list(self, query: TaskQuery | None = None) -> list[Task]:
        return self.store.list(query)

    def search(
        self,
        text: str,
        *,
        limit: int = 50,
        include_done: bool = True,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> list[Task]:
        return self.store.search(
            text,
            limit=limit,
            include_done=include_done,
            include_archived=include_archived,
            include_deleted=include_deleted,
        )

    def tasks_for_session(self, session_id: str) -> list[dict[str, str]]:
        return self.store.get_tasks_for_session(session_id)

    # -- public mutations -----------------------------------------

    def create(
        self,
        *,
        description: str,
        client_mutation_id: str,
        actor: str,
        session_id: str | None = None,
        task_id: str | None = None,
        state: str = "inbox",
        urgency: str = "medium",
        tags: Iterable[str | Tag | tuple[str, bool]] = (),
        complexity: str | None = None,
        contract: str | None = None,
        due_date: str | None = None,
        deadline_date: str | None = None,
        task_kind: str = "task",
        density: str = "sparse",
        summary_text: str | None = None,
        outcome_text: str | None = None,
        next_action_text: str | None = None,
        definition_of_done: str | None = None,
        creation_effort: str = "developed",
        user_involvement: str = "high",
        creation_provenance: str = "manual",
        has_dependency: bool = False,
        dependencies: Sequence[str] = (),
        dependency_hint: str | None = None,
        risk_profile_json: str | None = None,
        automation_tier_achievable: int | None = None,
        agent_required_contexts: Sequence[str] = (),
        user_required_contexts: Sequence[str] = (),
        required_contexts_source: str | None = None,
        legacy_import_receipt_id: str | None = None,
        creation_intent_id: str | None = None,
        initial_document: TaskDocumentLink | None = None,
        field_derivations: Sequence[FieldDerivation] = (),
    ) -> MutationResult:
        prepared = self._prepare_create(
            description=description,
            client_mutation_id=client_mutation_id,
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            state=state,
            urgency=urgency,
            tags=tags,
            complexity=complexity,
            contract=contract,
            due_date=due_date,
            deadline_date=deadline_date,
            task_kind=task_kind,
            density=density,
            summary_text=summary_text,
            outcome_text=outcome_text,
            next_action_text=next_action_text,
            definition_of_done=definition_of_done,
            creation_effort=creation_effort,
            user_involvement=user_involvement,
            creation_provenance=creation_provenance,
            has_dependency=has_dependency,
            dependencies=dependencies,
            dependency_hint=dependency_hint,
            risk_profile_json=risk_profile_json,
            automation_tier_achievable=automation_tier_achievable,
            agent_required_contexts=agent_required_contexts,
            user_required_contexts=user_required_contexts,
            required_contexts_source=required_contexts_source,
            legacy_import_receipt_id=legacy_import_receipt_id,
            creation_intent_id=creation_intent_id,
            initial_document=initial_document,
            field_derivations=field_derivations,
        )
        fields = prepared.fields
        summary_text = fields["summary_text"]
        outcome_text = fields["outcome_text"]
        next_action_text = fields["next_action_text"]
        definition_of_done = fields["definition_of_done"]
        normalized_dependencies = prepared.dependencies
        normalized_tags = prepared.tags
        effective_task_id = prepared.effective_task_id
        normalized_derivations = prepared.derivations
        request = prepared.request

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            if conn.execute(
                "SELECT 1 FROM task_metadata WHERE task_id = ?", (effective_task_id,)
            ).fetchone():
                raise TaskValidationError({"task_id": "That task ID already exists."})
            # The coordinator reserves task IDs in this same SQLite authority.
            # Only the participant carrying that exact intent may consume one.
            reservation = conn.execute(
                "SELECT intent_id FROM task_creation_intents "
                "WHERE task_id=? AND status!='aborted'",
                (effective_task_id,),
            ).fetchone()
            if reservation is not None and str(reservation["intent_id"]) != str(
                creation_intent_id or ""
            ):
                raise TaskValidationError(
                    {
                        "task_id": (
                            "That task ID is reserved by an aggregate creation."
                        )
                    }
                )
            self._insert_task_record(
                conn,
                task_id=effective_task_id,
                now=now,
                actor=actor,
                session_id=session_id,
                fields=fields,
                tags=normalized_tags,
                contract=contract,
                summary_text=summary_text,
                outcome_text=outcome_text,
                next_action_text=next_action_text,
                definition_of_done=definition_of_done,
                creation_provenance=creation_provenance,
                has_dependency=has_dependency,
                dependencies=normalized_dependencies,
                dependency_hint=dependency_hint,
                risk_profile_json=risk_profile_json,
                automation_tier_achievable=automation_tier_achievable,
                agent_required_contexts=agent_required_contexts,
                user_required_contexts=user_required_contexts,
                required_contexts_source=required_contexts_source,
                legacy_import_receipt_id=legacy_import_receipt_id,
            )
            if initial_document is not None:
                self.store.upsert_task_document_link(
                    initial_document,
                    connection=conn,
                )
                conn.execute(
                    "UPDATE task_metadata SET note_uuid=? WHERE task_id=?",
                    (initial_document.note_uuid, effective_task_id),
                )
            return _Change(
                task_id=effective_task_id,
                old_state=None,
                new_state=fields["state"],
                changed=True,
                details={
                    "created": True,
                    "tags": [tag.to_dict() for tag in normalized_tags],
                    "document": initial_document.to_dict() if initial_document else None,
                    "creation_intent_id": creation_intent_id,
                },
                reason="created",
            )

        def finalize(
            conn: sqlite3.Connection,
            now: str,
            receipt_id: str,
            _change: _Change,
        ) -> None:
            if creation_intent_id is None:
                return
            TaskCreationCoordinator.publish_in_connection(
                conn,
                intent_id=creation_intent_id,
                task_id=effective_task_id,
                actor=actor,
                task_receipt_id=receipt_id,
                document=initial_document,
                field_derivations=normalized_derivations,
                now=now,
            )

        return self._execute(
            mutation="create",
            client_mutation_id=client_mutation_id,
            actor=actor,
            session_id=session_id,
            task_id=effective_task_id,
            request=request,
            operation=operation,
            finalize=finalize,
        )

    def validate_create(self, **values: Any) -> None:
        """Validate a create request without reserving or publishing any state.

        Aggregate writers use this before opening their cross-store intent.  It
        deliberately shares the exact preparation path used by :meth:`create`,
        so a request that cannot become a task cannot leave a document saga to
        recover later.  An existing task is accepted only when this client
        mutation already owns an aggregate intent; the coordinator immediately
        following this check remains the authority for replay versus conflict.
        """

        prepared = self._prepare_create(**values)
        client_mutation_id = str(values["client_mutation_id"])
        conn = self.store.connect()
        try:
            task_exists = conn.execute(
                "SELECT 1 FROM task_metadata WHERE task_id=?",
                (prepared.effective_task_id,),
            ).fetchone()
            if task_exists is None:
                return
            aggregate_replay = conn.execute(
                "SELECT 1 FROM task_creation_intents WHERE client_mutation_id=?",
                (client_mutation_id,),
            ).fetchone()
        finally:
            conn.close()
        if aggregate_replay is None:
            raise TaskValidationError({"task_id": "That task ID already exists."})

    def _prepare_create(
        self,
        *,
        description: str,
        client_mutation_id: str,
        actor: str,
        session_id: str | None = None,
        task_id: str | None = None,
        state: str = "inbox",
        urgency: str = "medium",
        tags: Iterable[str | Tag | tuple[str, bool]] = (),
        complexity: str | None = None,
        contract: str | None = None,
        due_date: str | None = None,
        deadline_date: str | None = None,
        task_kind: str = "task",
        density: str = "sparse",
        summary_text: str | None = None,
        outcome_text: str | None = None,
        next_action_text: str | None = None,
        definition_of_done: str | None = None,
        creation_effort: str = "developed",
        user_involvement: str = "high",
        creation_provenance: str = "manual",
        has_dependency: bool = False,
        dependencies: Sequence[str] = (),
        dependency_hint: str | None = None,
        risk_profile_json: str | None = None,
        automation_tier_achievable: int | None = None,
        agent_required_contexts: Sequence[str] = (),
        user_required_contexts: Sequence[str] = (),
        required_contexts_source: str | None = None,
        legacy_import_receipt_id: str | None = None,
        creation_intent_id: str | None = None,
        initial_document: TaskDocumentLink | None = None,
        field_derivations: Sequence[FieldDerivation] = (),
    ) -> _PreparedCreate:
        fields = self._validate_create_fields(
            description=description,
            state=state,
            urgency=urgency,
            complexity=complexity,
            due_date=due_date,
            deadline_date=deadline_date,
            task_kind=task_kind,
            density=density,
            creation_effort=creation_effort,
            user_involvement=user_involvement,
            required_contexts_source=required_contexts_source,
            outcome_text=outcome_text,
            next_action_text=next_action_text,
            definition_of_done=definition_of_done,
            summary_text=summary_text,
        )
        try:
            normalized_dependencies = tuple(
                json.loads(self._json_array(dependencies))
            )
        except (TypeError, ValueError) as exc:
            raise TaskValidationError(
                {"dependencies": "Dependencies must be a list of non-empty strings."}
            ) from exc
        for field_name, contexts in (
            ("agent_required_contexts", agent_required_contexts),
            ("user_required_contexts", user_required_contexts),
        ):
            try:
                self._json_array(contexts)
            except (TypeError, ValueError) as exc:
                raise TaskValidationError(
                    {field_name: "Contexts must be a list of non-empty strings."}
                ) from exc
        normalized_tags = self._normalize_tags(tags)
        if initial_document is not None and initial_document.task_id != (
            task_id or initial_document.task_id
        ):
            raise TaskValidationError(
                {"initial_document": "The document link belongs to another task."}
            )
        if creation_intent_id is None and field_derivations:
            raise TaskValidationError(
                {"field_derivations": "Field derivations require a creation intent."}
            )
        requested_task_id = task_id
        effective_task_id = task_id or self._id_factory()
        self._validate_task_id(effective_task_id)
        if initial_document is not None and initial_document.task_id != effective_task_id:
            raise TaskValidationError(
                {"initial_document": "The document link belongs to another task."}
            )
        normalized_derivations = tuple(field_derivations)
        for derivation in normalized_derivations:
            derivation.validate()
        self._validate_authority(client_mutation_id, actor)
        request = {
            "requested_task_id": requested_task_id,
            **fields,
            "tags": [tag.to_dict() for tag in normalized_tags],
            "contract": contract,
            "outcome_text": fields["outcome_text"],
            "summary_text": fields["summary_text"],
            "next_action_text": fields["next_action_text"],
            "definition_of_done": fields["definition_of_done"],
            "creation_provenance": creation_provenance,
            "has_dependency": bool(has_dependency),
            "dependencies": list(normalized_dependencies),
            "dependency_hint": dependency_hint,
            "risk_profile_json": risk_profile_json,
            "automation_tier_achievable": automation_tier_achievable,
            "agent_required_contexts": list(agent_required_contexts),
            "user_required_contexts": list(user_required_contexts),
            "legacy_import_receipt_id": legacy_import_receipt_id,
            "creation_intent_id": creation_intent_id,
            "initial_document": initial_document.to_dict() if initial_document else None,
            "field_derivations": [
                asdict(derivation) for derivation in normalized_derivations
            ],
        }
        return _PreparedCreate(
            fields=fields,
            dependencies=normalized_dependencies,
            tags=normalized_tags,
            effective_task_id=effective_task_id,
            derivations=normalized_derivations,
            request=request,
        )

    def batch_create(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        client_mutation_id: str,
        actor: str,
        session_id: str | None = None,
    ) -> BatchMutationResult:
        """Atomically create a validated batch with deterministic child IDs."""
        self._validate_authority(client_mutation_id, actor)
        if not items:
            raise TaskValidationError({"items": "At least one task is required."})
        if len(items) > 500:
            raise TaskValidationError({"items": "A batch can contain at most 500 tasks."})
        prepared = [
            self._prepare_batch_item(item, index, client_mutation_id)
            for index, item in enumerate(items)
        ]
        request = {"items": [item["request"] for item in prepared]}
        request_hash = self._request_hash("batch.create", request)
        now = self._now()
        with self.store.transaction() as conn:
            self._assert_native_mutation_authority(conn)
            existing = conn.execute(
                "SELECT * FROM task_mutation_receipts WHERE client_mutation_id = ?",
                (client_mutation_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash or existing["actor"] != actor:
                    raise TaskIdempotencyConflict(client_mutation_id)
                if existing["status"] != "completed" or not existing["result_json"]:
                    raise TaskDomainError("That batch is reserved but has no completed result yet.")
                return BatchMutationResult.from_dict(
                    json.loads(existing["result_json"]), replayed=True
                )
            receipt_id = self._receipt_id_factory()
            conn.execute(
                """
                INSERT INTO task_mutation_receipts (
                    receipt_id, client_mutation_id, actor, session_id, mutation,
                    task_id, request_hash, status, created_at
                ) VALUES (?, ?, ?, ?, 'batch.create', NULL, ?, 'pending', ?)
                """,
                (receipt_id, client_mutation_id, actor, session_id, request_hash, now),
            )
            for item in prepared:
                task_id = item["task_id"]
                if conn.execute(
                    "SELECT 1 FROM task_metadata WHERE task_id = ?", (task_id,)
                ).fetchone():
                    raise TaskValidationError(
                        {"items": f"Task ID {task_id!r} already exists."}
                    )
                if conn.execute(
                    "SELECT 1 FROM task_creation_intents "
                    "WHERE task_id=? AND status!='aborted'",
                    (task_id,),
                ).fetchone():
                    # Batch items never carry aggregate participant authority;
                    # the whole transaction must yield to the live reservation.
                    raise TaskValidationError(
                        {
                            "items": (
                                f"Task ID {task_id!r} is reserved by an "
                                "aggregate creation."
                            )
                        }
                    )
                self._insert_task_record(
                    conn,
                    task_id=task_id,
                    now=now,
                    actor=actor,
                    session_id=session_id,
                    **item["insert"],
                )
            tasks: list[Task] = []
            collection_revision = self.store.collection_revision_in_connection(conn)
            for item in prepared:
                task = self.store.get_in_connection(
                    conn, item["task_id"], include_deleted=True
                )
                assert task is not None
                collection_revision = self._next_collection_revision(conn, now)
                change = _Change(
                    task.task_id,
                    None,
                    task.state,
                    True,
                    {"created": True, "batch_index": item["index"]},
                    "batch created",
                )
                self._append_history(
                    conn,
                    task=task,
                    change=change,
                    mutation="batch.create",
                    actor=actor,
                    session_id=session_id,
                    receipt_id=receipt_id,
                    collection_revision=collection_revision,
                    now=now,
                )
                self._append_outbox(
                    conn,
                    task=task,
                    mutation="batch.create",
                    collection_revision=collection_revision,
                    now=now,
                )
                tasks.append(task)
            receipt = MutationReceipt(
                receipt_id=receipt_id,
                client_mutation_id=client_mutation_id,
                actor=actor,
                session_id=session_id,
                mutation="batch.create",
                request_hash=request_hash,
                status="completed",
                created_at=now,
                completed_at=now,
            )
            result = BatchMutationResult(
                tasks=tuple(tasks),
                collection_revision=collection_revision,
                receipt=receipt,
            )
            conn.execute(
                "UPDATE task_mutation_receipts SET status = 'completed', result_json = ?, "
                "completed_at = ? WHERE receipt_id = ?",
                (self._canonical_json(result.to_dict()), now, receipt_id),
            )
            return result

    def update(
        self,
        task_id: str,
        *,
        expected_revision: int,
        client_mutation_id: str,
        actor: str,
        changes: Mapping[str, Any],
        tags: Iterable[str | Tag | tuple[str, bool]] | None = None,
        state: str | None = None,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> MutationResult:
        expected_revision = self._authority_revision(
            {"expected_revision": expected_revision}
        )
        normalized = self._validate_update_fields(changes)
        normalized_tags = self._normalize_tags(tags) if tags is not None else None
        if state is not None and state not in VALID_ATTENTION_STATES:
            raise TaskValidationError(
                {"state": "Update accepts only an attention state; use lifecycle actions for Done or Snoozed."}
            )

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected_revision)
            current = self._raw_task_row(conn, task_id)
            changed_fields = {
                key: value
                for key, value in normalized.items()
                if current[key] != value
            }
            target_state = state or task.state
            state_changed = state is not None and target_state != task.state
            if state_changed and task.state == "done":
                raise TaskTransitionError(
                    "Reopen a completed task before changing its attention state."
                )
            if state == "focused":
                merged_handoff = (
                    normalized.get("summary_text", task.summary_text),
                    normalized.get("outcome_text", task.outcome_text),
                    normalized.get("next_action_text", task.next_action_text),
                    normalized.get("definition_of_done", task.definition_of_done),
                )
                has_document = conn.execute(
                    "SELECT 1 FROM task_document_links WHERE task_id = ? "
                    "AND lifecycle NOT IN ('retired', 'deleted') LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not any(value and str(value).strip() for value in merged_handoff) and has_document is None:
                    raise TaskValidationError(
                        {"state": "Focused tasks need handoff-quality details or a linked document."}
                    )
            if state_changed:
                changed_fields["state"] = target_state
                if task.state == "snoozed":
                    changed_fields["snooze_until"] = None
                    changed_fields["snooze_resume_state"] = None
            tag_changed = normalized_tags is not None and tuple(
                sorted(task.tags, key=lambda item: item.name)
            ) != normalized_tags
            if not changed_fields and not tag_changed:
                return _Change(task_id, task.state, task.state, False, {})
            if "deadline_date" in changed_fields:
                changed_fields["has_deadline"] = int(
                    changed_fields["deadline_date"] is not None
                )
            if tag_changed:
                conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
                if normalized_tags:
                    conn.executemany(
                        "INSERT INTO task_tags (task_id, tag, is_namespace) VALUES (?, ?, ?)",
                        [(task_id, tag.name, int(tag.is_namespace)) for tag in normalized_tags],
                    )
            self._update_task_cas(
                conn,
                task_id,
                expected_revision,
                now,
                actor,
                **changed_fields,
            )
            return _Change(
                task_id,
                task.state,
                target_state,
                True,
                {
                    "fields": {
                        name: {"old": current[name], "new": value}
                        for name, value in changed_fields.items()
                        if name != "has_deadline"
                    },
                    "tags": [tag.to_dict() for tag in normalized_tags]
                    if tag_changed and normalized_tags is not None
                    else None,
                },
                reason,
            )

        return self._execute(
            mutation="update",
            client_mutation_id=client_mutation_id,
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            request={
                "task_id": task_id,
                "expected_revision": expected_revision,
                "changes": normalized,
                "state": state,
                "tags": [tag.to_dict() for tag in normalized_tags]
                if normalized_tags is not None
                else None,
                "reason": reason,
            },
            operation=operation,
        )

    def complete(
        self,
        task_id: str,
        *,
        done_date: str | None = None,
        **authority: Any,
    ) -> MutationResult:
        """Complete a task, optionally preserving a retroactive done date.

        ``done_date`` is the MCP compatibility contract used by
        ``task_toggle``.  Native tasks store completion truth in
        ``completed_at``; a supplied ISO date is therefore persisted there
        verbatim instead of being silently replaced with the dispatch time.
        Keeping it in the lifecycle request also makes a reused idempotency
        key with a different completion date fail closed.
        """
        normalized_done_date = self._validate_temporal("done_date", done_date)
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state == "done":
                return _Change(task_id, "done", "done", False, {})
            completed_at = normalized_done_date or now
            resume = (
                task.snooze_resume_state
                if task.state == "snoozed" and task.snooze_resume_state in VALID_ATTENTION_STATES
                else task.state
            )
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state="done",
                completed_at=completed_at,
                snooze_until=None,
                snooze_resume_state=None,
            )
            return _Change(
                task_id,
                task.state,
                "done",
                True,
                {"resume_state": resume, "completed_at": completed_at},
                "completed",
            )

        return self._lifecycle(
            "complete",
            task_id,
            authority,
            operation,
            extra={"done_date": normalized_done_date},
        )

    def reopen(self, task_id: str, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state != "done":
                return _Change(task_id, task.state, task.state, False, {})
            resume = self._completion_resume_state(conn, task_id)
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state=resume,
                completed_at=None,
            )
            return _Change(task_id, "done", resume, True, {"restored_state": resume}, "reopened")

        return self._lifecycle("reopen", task_id, authority, operation)

    def toggle(
        self,
        task_id: str,
        *,
        done_date: str | None = None,
        **authority: Any,
    ) -> MutationResult:
        """Atomically toggle completion under one idempotent mutation name.

        Choosing ``complete`` versus ``reopen`` before entering the receipt
        transaction makes a response-loss replay state-dependent: after the
        first commit, the same call chooses the opposite lifecycle operation.
        Keeping the choice inside one ``toggle`` operation makes the receipt
        replay stable and closes that race.
        """
        normalized_done_date = self._validate_temporal("done_date", done_date)
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state == "done":
                resume = self._completion_resume_state(conn, task_id)
                self._update_task_cas(
                    conn,
                    task_id,
                    expected,
                    now,
                    authority["actor"],
                    state=resume,
                    completed_at=None,
                )
                return _Change(
                    task_id,
                    "done",
                    resume,
                    True,
                    {"restored_state": resume},
                    "reopened",
                )

            completed_at = normalized_done_date or now
            resume = (
                task.snooze_resume_state
                if task.state == "snoozed"
                and task.snooze_resume_state in VALID_ATTENTION_STATES
                else task.state
            )
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state="done",
                completed_at=completed_at,
                snooze_until=None,
                snooze_resume_state=None,
            )
            return _Change(
                task_id,
                task.state,
                "done",
                True,
                {"resume_state": resume, "completed_at": completed_at},
                "completed",
            )

        return self._lifecycle(
            "toggle",
            task_id,
            authority,
            operation,
            extra={"done_date": normalized_done_date},
        )

    def set_state(self, task_id: str, *, state: str, **authority: Any) -> MutationResult:
        if state not in VALID_ATTENTION_STATES:
            raise TaskValidationError(
                {"state": "Use complete/reopen or snooze/resume for lifecycle states."}
            )
        if state == "focused":
            return self.focus(task_id, **authority)
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state == "done":
                raise TaskTransitionError("Reopen a completed task before changing attention state.")
            if task.state == state:
                return _Change(task_id, task.state, state, False, {})
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state=state,
                snooze_until=None,
                snooze_resume_state=None,
            )
            return _Change(task_id, task.state, state, True, {}, "attention state changed")

        return self._lifecycle("state", task_id, authority, operation, extra={"state": state})

    def focus(self, task_id: str, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state == "done":
                raise TaskTransitionError("Reopen a completed task before focusing it.")
            if not self._has_handoff_context(conn, task):
                raise TaskValidationError(
                    {
                        "state": (
                            "Focused tasks need an outcome, next action, definition of done, "
                            "or linked knowledge document."
                        )
                    }
                )
            if task.state == "focused":
                return _Change(task_id, "focused", "focused", False, {})
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state="focused",
                snooze_until=None,
                snooze_resume_state=None,
            )
            return _Change(task_id, task.state, "focused", True, {}, "focused")

        return self._lifecycle("focus", task_id, authority, operation)

    def snooze(self, task_id: str, *, until: str, **authority: Any) -> MutationResult:
        normalized_until = self._validate_temporal("snooze_until", until, allow_datetime=True)
        assert normalized_until is not None
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state == "done":
                raise TaskTransitionError("Completed tasks cannot be snoozed.")
            resume = (
                task.snooze_resume_state
                if task.state == "snoozed" and task.snooze_resume_state in VALID_ATTENTION_STATES
                else task.state
            )
            if task.state == "snoozed" and task.snooze_until == normalized_until:
                return _Change(task_id, "snoozed", "snoozed", False, {})
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state="snoozed",
                snooze_until=normalized_until,
                snooze_resume_state=resume,
            )
            return _Change(
                task_id,
                task.state,
                "snoozed",
                True,
                {"until": normalized_until, "resume_state": resume},
                "snoozed",
            )

        return self._lifecycle("snooze", task_id, authority, operation, extra={"until": normalized_until})

    def resume(self, task_id: str, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if task.state != "snoozed":
                return _Change(task_id, task.state, task.state, False, {})
            resume = task.snooze_resume_state if task.snooze_resume_state in VALID_ATTENTION_STATES else "inbox"
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                state=resume,
                snooze_until=None,
                snooze_resume_state=None,
            )
            return _Change(task_id, "snoozed", resume, True, {"restored_state": resume}, "snooze ended")

        return self._lifecycle("resume", task_id, authority, operation)

    def archive(self, task_id: str, **authority: Any) -> MutationResult:
        return self._timestamp_lifecycle(task_id, "archive", "archived_at", True, authority)

    def unarchive(self, task_id: str, **authority: Any) -> MutationResult:
        return self._timestamp_lifecycle(task_id, "unarchive", "archived_at", False, authority)

    def delete(self, task_id: str, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected, allow_deleted=True)
            if task.deleted_at is not None:
                return _Change(task_id, task.state, task.state, False, {})
            self._update_task_cas(conn, task_id, expected, now, authority["actor"], deleted_at=now)
            return _Change(task_id, task.state, task.state, True, {"deleted_at": now}, "deleted")

        return self._lifecycle("delete", task_id, authority, operation)

    def restore(self, task_id: str, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected, allow_deleted=True)
            if task.deleted_at is None:
                return _Change(task_id, task.state, task.state, False, {})
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                deleted_at=None,
                restored_at=now,
            )
            return _Change(task_id, task.state, task.state, True, {"restored_at": now}, "restored")

        return self._lifecycle("restore", task_id, authority, operation)

    def replace_tags(
        self,
        task_id: str,
        *,
        tags: Iterable[str | Tag | tuple[str, bool]],
        **authority: Any,
    ) -> MutationResult:
        normalized = self._normalize_tags(tags)
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            current = tuple(sorted(task.tags, key=lambda item: item.name))
            if current == normalized:
                return _Change(task_id, task.state, task.state, False, {})
            conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
            if normalized:
                conn.executemany(
                    "INSERT INTO task_tags (task_id, tag, is_namespace) VALUES (?, ?, ?)",
                    [(task_id, tag.name, int(tag.is_namespace)) for tag in normalized],
                )
            self._update_task_cas(conn, task_id, expected, now, authority["actor"])
            return _Change(
                task_id,
                task.state,
                task.state,
                True,
                {
                    "old_tags": [item.to_dict() for item in current],
                    "new_tags": [item.to_dict() for item in normalized],
                },
                "tags replaced",
            )

        return self._lifecycle(
            "tags",
            task_id,
            authority,
            operation,
            extra={"tags": [tag.to_dict() for tag in normalized]},
        )

    def attach_document(
        self,
        task_id: str,
        link: TaskDocumentLink,
        *,
        expected_revision: int,
        client_mutation_id: str,
        actor: str,
        session_id: str | None = None,
    ) -> MutationResult:
        """Atomically attach a provisioned document to its task row."""
        expected_revision = self._authority_revision(
            {"expected_revision": expected_revision}
        )
        if link.task_id != task_id:
            raise TaskValidationError(
                {"task_id": "Document link belongs to a different task."}
            )
        authority = {
            "expected_revision": expected_revision,
            "client_mutation_id": client_mutation_id,
            "actor": actor,
            "session_id": session_id,
        }

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected_revision)
            row = conn.execute(
                "SELECT * FROM task_document_links WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is not None and dict(row) == link.to_dict():
                return _Change(task_id, task.state, task.state, False, {})
            self.store.upsert_task_document_link(link, connection=conn)
            self._update_task_cas(
                conn,
                task_id,
                expected_revision,
                now,
                actor,
                note_uuid=link.note_uuid,
            )
            return _Change(
                task_id,
                task.state,
                task.state,
                True,
                {
                    "document": {
                        "note_uuid": link.note_uuid,
                        "store_id": link.store_id,
                        "document_id": link.document_id,
                        "binding_id": link.binding_id,
                        "lifecycle": link.lifecycle,
                    }
                },
                "document attached",
            )

        return self._lifecycle(
            "document.attach",
            task_id,
            authority,
            operation,
            extra={"link": link.to_dict()},
        )

    def assign(
        self,
        task_id: str,
        session_id: str,
        *,
        expected_revision: int,
        client_mutation_id: str,
        actor: str,
        actor_session_id: str | None = None,
    ) -> MutationResult:
        """Assign a task to a session under the same mutation guarantees."""
        expected_revision = self._authority_revision(
            {"expected_revision": expected_revision}
        )
        if not isinstance(session_id, str) or not session_id.strip():
            raise TaskValidationError({"session_id": "An assignee session ID is required."})
        assignee = session_id.strip()
        authority = {
            "expected_revision": expected_revision,
            "client_mutation_id": client_mutation_id,
            "actor": actor,
            "session_id": actor_session_id,
        }

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected_revision)
            exists = conn.execute(
                "SELECT 1 FROM task_sessions WHERE task_id = ? AND session_id = ?",
                (task_id, assignee),
            ).fetchone()
            if exists is not None:
                return _Change(task_id, task.state, task.state, False, {})
            conn.execute(
                "INSERT INTO task_sessions (task_id, session_id, assigned_at) "
                "VALUES (?, ?, ?)",
                (task_id, assignee, now),
            )
            self._update_task_cas(conn, task_id, expected_revision, now, actor)
            return _Change(
                task_id,
                task.state,
                task.state,
                True,
                {"assigned_session_id": assignee},
                "assigned",
            )

        return self._lifecycle(
            "assign",
            task_id,
            authority,
            operation,
            extra={"assigned_session_id": assignee},
        )

    # -- action items ----------------------------------------------

    def create_action_item(
        self,
        task_id: str,
        *,
        description: str,
        definition_of_done: str | None = None,
        authorship: str = "user",
        risk_profile_json: str | None = None,
        agent_required_contexts: Sequence[str] = (),
        user_required_contexts: Sequence[str] = (),
        **authority: Any,
    ) -> MutationResult:
        prepared = self._validate_action_changes(
            {
                "description": description,
                "definition_of_done": definition_of_done,
                "authorship": authorship,
                "risk_profile_json": risk_profile_json,
                "agent_required_contexts": agent_required_contexts,
                "user_required_contexts": user_required_contexts,
            }
        )
        description = prepared["description"]
        definition_of_done = prepared["definition_of_done"]
        authorship = prepared["authorship"]
        risk_profile_json = prepared["risk_profile_json"]
        agent_contexts_json = prepared["agent_required_contexts"]
        user_contexts_json = prepared["user_required_contexts"]
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_action_items "
                    "WHERE task_id = ? AND deleted_at IS NULL",
                    (task_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO task_action_items (
                    task_id, sequence, description, state, risk_profile_json,
                    agent_required_contexts, user_required_contexts,
                    definition_of_done, authorship, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    sequence,
                    description,
                    risk_profile_json,
                    agent_contexts_json,
                    user_contexts_json,
                    definition_of_done,
                    authorship,
                    now,
                    now,
                ),
            )
            action_id = int(cursor.lastrowid)
            self._update_task_cas(conn, task_id, expected, now, authority["actor"])
            return _Change(task_id, task.state, task.state, True, {"action_item_id": action_id, "operation": "created"})

        return self._lifecycle("action_item.create", task_id, authority, operation, extra={"description": description})

    def update_action_item(
        self,
        task_id: str,
        action_item_id: int,
        *,
        changes: Mapping[str, Any],
        _mutation: str = "action_item.update",
        **authority: Any,
    ) -> MutationResult:
        normalized = self._validate_action_changes(changes)
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            row = self._require_action_item(conn, task_id, action_item_id)
            changed = {key: value for key, value in normalized.items() if row[key] != value}
            if not changed:
                return _Change(task_id, task.state, task.state, False, {})
            if "state" in changed:
                changed["completed_at"] = now if changed["state"] == "done" else None
            sets = [f"{key} = ?" for key in changed]
            conn.execute(
                f"UPDATE task_action_items SET {', '.join(sets)}, updated_at = ? "
                "WHERE id = ? AND task_id = ?",
                [*changed.values(), now, action_item_id, task_id],
            )
            self._update_task_cas(conn, task_id, expected, now, authority["actor"])
            return _Change(
                task_id,
                task.state,
                task.state,
                True,
                {"action_item_id": action_item_id, "fields": changed},
            )

        return self._lifecycle(
            _mutation,
            task_id,
            authority,
            operation,
            extra={"action_item_id": action_item_id, "changes": normalized},
        )

    def reorder_action_items(
        self,
        task_id: str,
        *,
        action_item_ids: Sequence[int],
        **authority: Any,
    ) -> MutationResult:
        ordered_ids = [int(item) for item in action_item_ids]
        if len(ordered_ids) != len(set(ordered_ids)):
            raise TaskValidationError({"action_item_ids": "Action item IDs must be unique."})
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            rows = conn.execute(
                "SELECT id, sequence FROM task_action_items WHERE task_id = ? "
                "AND deleted_at IS NULL ORDER BY sequence, id",
                (task_id,),
            ).fetchall()
            current = [int(row["id"]) for row in rows]
            if set(current) != set(ordered_ids):
                raise TaskValidationError(
                    {"action_item_ids": "Reorder must contain every live action item exactly once."}
                )
            if current == ordered_ids:
                return _Change(task_id, task.state, task.state, False, {})
            for position, item_id in enumerate(ordered_ids, 1):
                conn.execute(
                    "UPDATE task_action_items SET sequence = ?, updated_at = ? WHERE id = ?",
                    (-position, now, item_id),
                )
            for position, item_id in enumerate(ordered_ids, 1):
                conn.execute(
                    "UPDATE task_action_items SET sequence = ? WHERE id = ?",
                    (position, item_id),
                )
            self._update_task_cas(conn, task_id, expected, now, authority["actor"])
            return _Change(task_id, task.state, task.state, True, {"old_order": current, "new_order": ordered_ids})

        return self._lifecycle("action_item.reorder", task_id, authority, operation, extra={"action_item_ids": ordered_ids})

    def set_current_action_item(
        self,
        task_id: str,
        *,
        action_item_id: int | None,
        **authority: Any,
    ) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            if action_item_id is not None:
                self._require_action_item(conn, task_id, int(action_item_id))
            if task.current_action_item_id == action_item_id:
                return _Change(task_id, task.state, task.state, False, {})
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                authority["actor"],
                current_action_item_id=action_item_id,
            )
            return _Change(task_id, task.state, task.state, True, {"current_action_item_id": action_item_id})

        return self._lifecycle("action_item.current", task_id, authority, operation, extra={"action_item_id": action_item_id})

    def approve_action_item(self, task_id: str, action_item_id: int, **authority: Any) -> MutationResult:
        return self.update_action_item(
            task_id,
            action_item_id,
            changes={"authorship": "agent_approved"},
            _mutation="action_item.approve",
            **authority,
        )

    def delete_action_item(self, task_id: str, action_item_id: int, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            row = self._require_action_item(conn, task_id, action_item_id, allow_deleted=True)
            if row["deleted_at"] is not None:
                return _Change(task_id, task.state, task.state, False, {})
            conn.execute(
                "UPDATE task_action_items SET deleted_at = ?, updated_at = ?, sequence = ? "
                "WHERE id = ?",
                (now, now, -1_000_000_000 - action_item_id, action_item_id),
            )
            values: dict[str, Any] = {}
            if task.current_action_item_id == action_item_id:
                values["current_action_item_id"] = None
            self._update_task_cas(conn, task_id, expected, now, authority["actor"], **values)
            return _Change(task_id, task.state, task.state, True, {"action_item_id": action_item_id, "operation": "deleted"})

        return self._lifecycle("action_item.delete", task_id, authority, operation, extra={"action_item_id": action_item_id})

    def restore_action_item(self, task_id: str, action_item_id: int, **authority: Any) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            row = self._require_action_item(conn, task_id, action_item_id, allow_deleted=True)
            if row["deleted_at"] is None:
                return _Change(task_id, task.state, task.state, False, {})
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_action_items "
                    "WHERE task_id = ? AND deleted_at IS NULL",
                    (task_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE task_action_items SET deleted_at = NULL, updated_at = ?, sequence = ? "
                "WHERE id = ?",
                (now, sequence, action_item_id),
            )
            self._update_task_cas(conn, task_id, expected, now, authority["actor"])
            return _Change(task_id, task.state, task.state, True, {"action_item_id": action_item_id, "operation": "restored"})

        return self._lifecycle("action_item.restore", task_id, authority, operation, extra={"action_item_id": action_item_id})

    # -- transaction machinery ------------------------------------

    def _assert_native_mutation_authority(self, conn: sqlite3.Connection) -> None:
        """Fence every native write from the transaction's locked snapshot.

        ``TaskStore.transaction`` has already acquired ``BEGIN IMMEDIATE`` at
        this point.  Reading the authority row on that same connection makes
        the fence/epoch decision part of the exact snapshot that receives the
        receipt, entity, history, and outbox writes; a cutover or rollback CAS
        cannot slip between this assertion and the commit.  The runtime check
        additionally validates the independent installation latch against the
        locked row, so SQLite cannot vouch for its own native authority.
        """

        row = conn.execute(
            "SELECT authority_epoch, rollback_fence "
            "FROM task_system_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise TaskAuthorityUnavailable()
        if bool(row["rollback_fence"]):
            raise TaskMutationFenced()

        from .runtime import (
            is_native_authority_epoch,
            native_task_mutation_authority,
        )

        if not is_native_authority_epoch(row["authority_epoch"]):
            raise TaskAuthorityUnavailable()
        if not native_task_mutation_authority(self.store.path):
            raise TaskAuthorityUnavailable()

    def _execute(
        self,
        *,
        mutation: str,
        client_mutation_id: str,
        actor: str,
        session_id: str | None,
        task_id: str,
        request: Mapping[str, Any],
        operation: Callable[[sqlite3.Connection, str], _Change],
        finalize: Callable[[sqlite3.Connection, str, str, _Change], None] | None = None,
    ) -> MutationResult:
        self._validate_authority(client_mutation_id, actor)
        request_hash = self._request_hash(mutation, request)
        now = self._now()
        with self.store.transaction() as conn:
            self._assert_native_mutation_authority(conn)
            existing = conn.execute(
                "SELECT * FROM task_mutation_receipts WHERE client_mutation_id = ?",
                (client_mutation_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash or existing["actor"] != actor:
                    raise TaskIdempotencyConflict(client_mutation_id)
                if existing["status"] != "completed" or not existing["result_json"]:
                    raise TaskDomainError("That mutation is reserved but has no completed result yet.")
                return MutationResult.from_dict(
                    json.loads(existing["result_json"]), replayed=True
                )

            receipt_id = self._receipt_id_factory()
            conn.execute(
                """
                INSERT INTO task_mutation_receipts (
                    receipt_id, client_mutation_id, actor, session_id, mutation,
                    task_id, request_hash, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    receipt_id,
                    client_mutation_id,
                    actor,
                    session_id,
                    mutation,
                    task_id,
                    request_hash,
                    now,
                ),
            )
            change = operation(conn, now)
            if finalize is not None:
                finalize(conn, now, receipt_id, change)
            if change.changed:
                collection_revision = self._next_collection_revision(conn, now)
                task = self.store.get_in_connection(conn, change.task_id, include_deleted=True)
                assert task is not None
                self._append_history(
                    conn,
                    task=task,
                    change=change,
                    mutation=mutation,
                    actor=actor,
                    session_id=session_id,
                    receipt_id=receipt_id,
                    collection_revision=collection_revision,
                    now=now,
                )
                self._append_outbox(
                    conn,
                    task=task,
                    mutation=mutation,
                    collection_revision=collection_revision,
                    now=now,
                )
            else:
                collection_revision = self.store.collection_revision_in_connection(conn)
                task = self.store.get_in_connection(conn, change.task_id, include_deleted=True)
                assert task is not None
            receipt = MutationReceipt(
                receipt_id=receipt_id,
                client_mutation_id=client_mutation_id,
                actor=actor,
                session_id=session_id,
                mutation=mutation,
                request_hash=request_hash,
                status="completed",
                created_at=now,
                completed_at=now,
            )
            result = MutationResult(
                task=task,
                collection_revision=collection_revision,
                receipt=receipt,
                changed=change.changed,
            )
            conn.execute(
                "UPDATE task_mutation_receipts SET status = 'completed', result_json = ?, "
                "completed_at = ? WHERE receipt_id = ?",
                (self._canonical_json(result.to_dict()), now, receipt_id),
            )
            return result

    def _lifecycle(
        self,
        mutation: str,
        task_id: str,
        authority: Mapping[str, Any],
        operation: Callable[[sqlite3.Connection, str], _Change],
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> MutationResult:
        expected = self._authority_revision(authority)
        return self._execute(
            mutation=mutation,
            client_mutation_id=str(authority["client_mutation_id"]),
            actor=str(authority["actor"]),
            session_id=authority.get("session_id"),
            task_id=task_id,
            request={"task_id": task_id, "expected_revision": expected, **dict(extra or {})},
            operation=operation,
        )

    def _timestamp_lifecycle(
        self,
        task_id: str,
        mutation: str,
        column: str,
        enabled: bool,
        authority: Mapping[str, Any],
    ) -> MutationResult:
        expected = self._authority_revision(authority)

        def operation(conn: sqlite3.Connection, now: str) -> _Change:
            task = self._require_task(conn, task_id, expected)
            current = getattr(task, column)
            if bool(current) == enabled:
                return _Change(task_id, task.state, task.state, False, {})
            value = now if enabled else None
            self._update_task_cas(
                conn,
                task_id,
                expected,
                now,
                str(authority["actor"]),
                **{column: value},
            )
            return _Change(task_id, task.state, task.state, True, {column: value}, mutation)

        return self._lifecycle(mutation, task_id, authority, operation)

    # -- low-level helpers -----------------------------------------

    def _insert_task_record(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        now: str,
        actor: str,
        session_id: str | None,
        fields: Mapping[str, Any],
        tags: Sequence[Tag],
        contract: str | None = None,
        summary_text: str | None = None,
        outcome_text: str | None = None,
        next_action_text: str | None = None,
        definition_of_done: str | None = None,
        creation_provenance: str = "manual",
        has_dependency: bool = False,
        dependencies: Sequence[str] = (),
        dependency_hint: str | None = None,
        risk_profile_json: str | None = None,
        automation_tier_achievable: int | None = None,
        agent_required_contexts: Sequence[str] = (),
        user_required_contexts: Sequence[str] = (),
        required_contexts_source: str | None = None,
        legacy_import_receipt_id: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "task_id": task_id,
            "state": fields["state"],
            "urgency": fields["urgency"],
            "complexity": fields["complexity"],
            "contract": contract,
            "created_at": now,
            "updated_at": now,
            "task_kind": fields["task_kind"],
            "density": fields["density"],
            "summary_text": summary_text,
            "outcome_text": outcome_text,
            "next_action_text": next_action_text,
            "definition_of_done": definition_of_done,
            "creation_effort": fields["creation_effort"],
            "user_involvement": fields["user_involvement"],
            "creation_provenance": creation_provenance,
            "has_deadline": int(fields["deadline_date"] is not None),
            "deadline_date": fields["deadline_date"],
            "has_dependency": int(bool(has_dependency or dependencies)),
            "dependencies_json": self._json_array(dependencies),
            "dependency_hint": dependency_hint,
            "description": fields["description"],
            "risk_profile_json": risk_profile_json,
            "automation_tier_achievable": automation_tier_achievable,
            "last_actor": self._last_actor(actor),
            "agent_required_contexts": self._json_array(agent_required_contexts),
            "user_required_contexts": self._json_array(user_required_contexts),
            "required_contexts_source": required_contexts_source,
            "created_by_session": session_id,
            "revision": 1,
            "due_date": fields["due_date"],
            "legacy_import_receipt_id": legacy_import_receipt_id,
        }
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        conn.execute(
            f"INSERT INTO task_metadata ({columns}) VALUES ({placeholders})",
            tuple(record.values()),
        )
        if tags:
            conn.executemany(
                "INSERT INTO task_tags (task_id, tag, is_namespace) VALUES (?, ?, ?)",
                [(task_id, tag.name, int(tag.is_namespace)) for tag in tags],
            )

    def _require_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expected_revision: int,
        *,
        allow_deleted: bool = False,
    ) -> Task:
        task = self.store.get_in_connection(conn, task_id, include_deleted=True)
        if task is None:
            raise TaskNotFound(task_id)
        if task.revision != expected_revision:
            raise TaskRevisionConflict(
                expected=expected_revision,
                current=task.revision,
                current_task=task.to_dict(),
            )
        if task.deleted_at is not None and not allow_deleted:
            raise TaskDeletedError(task_id)
        return task

    def _raise_revision_conflict(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expected_revision: int,
    ) -> None:
        task = self.store.get_in_connection(conn, task_id, include_deleted=True)
        if task is None:
            raise TaskNotFound(task_id)
        raise TaskRevisionConflict(
            expected=expected_revision,
            current=task.revision,
            current_task=task.to_dict(),
        )

    def _update_task_cas(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expected_revision: int,
        now: str,
        actor: str,
        **values: Any,
    ) -> None:
        assignments = [f"{key} = ?" for key in values]
        params = list(values.values())
        assignments.extend(["revision = revision + 1", "updated_at = ?", "last_actor = ?"])
        params.extend([now, self._last_actor(actor), task_id, expected_revision])
        cursor = conn.execute(
            f"UPDATE task_metadata SET {', '.join(assignments)} "
            "WHERE task_id = ? AND revision = ?",
            params,
        )
        if cursor.rowcount != 1:
            self._raise_revision_conflict(conn, task_id, expected_revision)

    @staticmethod
    def _raw_task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM task_metadata WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return row

    @staticmethod
    def _require_action_item(
        conn: sqlite3.Connection,
        task_id: str,
        action_item_id: int,
        *,
        allow_deleted: bool = False,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM task_action_items WHERE id = ? AND task_id = ?",
            (action_item_id, task_id),
        ).fetchone()
        if row is None or (row["deleted_at"] is not None and not allow_deleted):
            raise TaskValidationError({"action_item_id": "Action item was not found."})
        return row

    @staticmethod
    def _next_collection_revision(conn: sqlite3.Connection, now: str) -> int:
        conn.execute(
            "UPDATE task_collection_state SET revision = revision + 1, updated_at = ? WHERE id = 1",
            (now,),
        )
        return int(conn.execute("SELECT revision FROM task_collection_state WHERE id = 1").fetchone()[0])

    def _append_history(
        self,
        conn: sqlite3.Connection,
        *,
        task: Task,
        change: _Change,
        mutation: str,
        actor: str,
        session_id: str | None,
        receipt_id: str,
        collection_revision: int,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_state_history (
                task_id, old_state, new_state, changed_at, reason, mutation,
                actor, session_id, receipt_id, task_revision,
                collection_revision, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id,
                change.old_state,
                change.new_state,
                now,
                change.reason,
                mutation,
                actor,
                session_id,
                receipt_id,
                task.revision,
                collection_revision,
                self._canonical_json(change.details),
            ),
        )

    def _append_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        task: Task,
        mutation: str,
        collection_revision: int,
        now: str,
    ) -> None:
        payload = invalidation_payload(
            task_id=task.task_id,
            mutation=mutation,
            collection_revision=collection_revision,
        )
        conn.execute(
            """
            INSERT INTO task_event_outbox (
                event_id, task_id, mutation, task_revision,
                collection_revision, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._event_id_factory(),
                task.task_id,
                mutation,
                task.revision,
                collection_revision,
                self._canonical_json(payload),
                now,
            ),
        )

    @staticmethod
    def _completion_resume_state(conn: sqlite3.Connection, task_id: str) -> str:
        row = conn.execute(
            "SELECT old_state, details_json FROM task_state_history "
            "WHERE task_id = ? AND mutation IN ('complete', 'toggle') "
            "AND new_state = 'done' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return "inbox"
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        candidate = details.get("resume_state") if isinstance(details, dict) else None
        if candidate not in VALID_ATTENTION_STATES:
            candidate = row["old_state"]
        return str(candidate) if candidate in VALID_ATTENTION_STATES else "inbox"

    @staticmethod
    def _has_handoff_context(conn: sqlite3.Connection, task: Task) -> bool:
        if any(
            value and value.strip()
            for value in (
                task.summary_text,
                task.outcome_text,
                task.next_action_text,
                task.definition_of_done,
            )
        ):
            return True
        return (
            conn.execute(
                "SELECT 1 FROM task_document_links WHERE task_id = ? "
                "AND lifecycle NOT IN ('retired', 'deleted') LIMIT 1",
                (task.task_id,),
            ).fetchone()
            is not None
        )

    # -- validation ------------------------------------------------

    def _prepare_batch_item(
        self,
        item: Mapping[str, Any],
        index: int,
        client_mutation_id: str,
    ) -> dict[str, Any]:
        if not isinstance(item, Mapping):
            raise TaskValidationError({f"items.{index}": "Each item must be an object."})
        description = item.get("description", item.get("title"))
        summary_text = item.get("summary_text", item.get("summary"))
        outcome_text = item.get("outcome_text", item.get("desired_outcome"))
        next_action_text = item.get("next_action_text", item.get("next_action"))
        definition_of_done = item.get("definition_of_done")
        dependencies = item.get("dependencies") or ()
        if isinstance(dependencies, (str, bytes)):
            raise TaskValidationError(
                {f"items.{index}.dependencies": "Dependencies must be a list."}
            )
        supplied_tags = item.get("tags") or ()
        if isinstance(supplied_tags, (str, bytes)):
            raise TaskValidationError(
                {f"items.{index}.tags": "Tags must be a list."}
            )
        raw_tags: list[str | Tag | tuple[str, bool]] = []
        for raw in supplied_tags:
            if isinstance(raw, Mapping):
                raw_tags.append(Tag(str(raw.get("name") or ""), bool(raw.get("is_namespace"))))
            else:
                raw_tags.append(raw)
        project = item.get("project")
        if project:
            raw_tags.append(f"projects/{str(project).strip().strip('#/')}")
        supplied_namespaces = item.get("namespaces") or ()
        if isinstance(supplied_namespaces, (str, bytes)):
            raise TaskValidationError(
                {f"items.{index}.namespaces": "Namespaces must be a list."}
            )
        for namespace in supplied_namespaces:
            raw_tags.append(Tag(str(namespace), True))
        tags = self._normalize_tags(raw_tags)
        fields = self._validate_create_fields(
            description=description,
            state=item.get("state", "inbox"),
            urgency=item.get("urgency", "medium"),
            complexity=item.get("complexity"),
            due_date=item.get("due_date"),
            deadline_date=item.get("deadline_date"),
            task_kind=item.get("task_kind", "task"),
            density=item.get("density", "sparse"),
            creation_effort=item.get("creation_effort", "developed"),
            user_involvement=item.get("user_involvement", "high"),
            required_contexts_source=item.get("required_contexts_source"),
            summary_text=summary_text,
            outcome_text=outcome_text,
            next_action_text=next_action_text,
            definition_of_done=definition_of_done,
        )
        explicit_id = item.get("task_id")
        task_id = str(explicit_id) if explicit_id else "t-" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"work-buddy:task-batch:{client_mutation_id}:{index}",
        ).hex[:8]
        self._validate_task_id(task_id)
        insert = {
            "fields": fields,
            "tags": tags,
            "contract": item.get("contract"),
            "summary_text": fields["summary_text"],
            "outcome_text": fields["outcome_text"],
            "next_action_text": fields["next_action_text"],
            "definition_of_done": fields["definition_of_done"],
            "creation_provenance": item.get("creation_provenance", "manual"),
            "has_dependency": bool(item.get("has_dependency", False)),
            "dependencies": tuple(dependencies),
            "dependency_hint": item.get("dependency_hint"),
            "risk_profile_json": item.get("risk_profile_json"),
            "automation_tier_achievable": item.get("automation_tier_achievable"),
            "agent_required_contexts": tuple(item.get("agent_required_contexts") or ()),
            "user_required_contexts": tuple(item.get("user_required_contexts") or ()),
            "required_contexts_source": item.get("required_contexts_source"),
            "legacy_import_receipt_id": item.get("legacy_import_receipt_id"),
        }
        # Validate every sequence now so the transaction cannot fail halfway
        # through merely because a late row had malformed authoring metadata.
        try:
            self._json_array(insert["dependencies"])
            self._json_array(insert["agent_required_contexts"])
            self._json_array(insert["user_required_contexts"])
        except (TypeError, ValueError) as exc:
            raise TaskValidationError(
                {f"items.{index}": "Dependencies and contexts must be lists of non-empty strings."}
            ) from exc
        request = {
            "task_id": task_id,
            **fields,
            **{
                key: value
                for key, value in insert.items()
                if key not in {"fields", "tags"}
            },
            "tags": [tag.to_dict() for tag in tags],
        }
        return {
            "index": index,
            "task_id": task_id,
            "insert": insert,
            "request": request,
        }

    def _validate_create_fields(self, **values: Any) -> dict[str, Any]:
        errors: dict[str, str] = {}
        try:
            description = self._validate_description(values["description"], "description")
        except TaskValidationError as exc:
            errors.update(exc.field_errors)
            description = ""
        state = str(values["state"])
        if state not in VALID_ATTENTION_STATES:
            errors["state"] = (
                "New tasks must start in Inbox, MIT, Focused, Active, or Waiting."
            )
        urgency = str(values["urgency"])
        if urgency not in VALID_URGENCIES:
            errors["urgency"] = "Urgency must be low, medium, or high."
        complexity = values["complexity"]
        if complexity is not None and complexity not in VALID_COMPLEXITIES:
            errors["complexity"] = "Complexity must be simple, moderate, complex, or null."
        if values["task_kind"] not in _TASK_KINDS:
            errors["task_kind"] = "Unsupported task kind."
        if values["density"] not in _DENSITIES:
            errors["density"] = "Unsupported density."
        if values["creation_effort"] not in _CREATION_EFFORTS:
            errors["creation_effort"] = "Unsupported creation effort."
        if values["user_involvement"] not in _USER_INVOLVEMENTS:
            errors["user_involvement"] = "Unsupported user involvement."
        source = values["required_contexts_source"]
        if source is not None and source not in _CONTEXT_SOURCES:
            errors["required_contexts_source"] = "Unsupported context provenance."
        prose: dict[str, str | None] = {}
        for field in (
            "summary_text",
            "outcome_text",
            "next_action_text",
            "definition_of_done",
        ):
            value = values[field]
            if value is None or value == "":
                prose[field] = None
            elif not isinstance(value, str):
                errors[field] = "Use text or null."
                prose[field] = None
            else:
                cleaned = value.strip()
                if len(cleaned) > 100_000:
                    errors[field] = "Text exceeds 100,000 characters."
                prose[field] = cleaned or None
        try:
            due = self._validate_temporal("due_date", values["due_date"])
        except TaskValidationError as exc:
            errors.update(exc.field_errors)
            due = None
        try:
            deadline = self._validate_temporal("deadline_date", values["deadline_date"])
        except TaskValidationError as exc:
            errors.update(exc.field_errors)
            deadline = None
        if state == "focused" and not any(
            value and str(value).strip()
            for value in (
                prose["outcome_text"],
                prose["next_action_text"],
                prose["definition_of_done"],
                prose["summary_text"],
            )
        ):
            errors["state"] = "A new Focused task needs handoff-quality details."
        if errors:
            raise TaskValidationError(errors)
        return {
            "description": description,
            "state": state,
            "urgency": urgency,
            "complexity": complexity,
            "due_date": due,
            "deadline_date": deadline,
            "task_kind": values["task_kind"],
            "density": values["density"],
            "creation_effort": values["creation_effort"],
            "user_involvement": values["user_involvement"],
            **prose,
        }

    def _validate_update_fields(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "description",
            "urgency",
            "complexity",
            "contract",
            "due_date",
            "deadline_date",
            "summary_text",
            "task_kind",
            "density",
            "outcome_text",
            "next_action_text",
            "definition_of_done",
            "creation_effort",
            "user_involvement",
            "creation_provenance",
            "has_dependency",
            "dependency_hint",
            "risk_profile_json",
            "automation_tier_achievable",
            "agent_required_contexts",
            "user_required_contexts",
            "required_contexts_source",
            "dependencies",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise TaskValidationError(
                {key: "This field cannot be changed through task update." for key in sorted(unknown)}
            )
        normalized = dict(changes)
        errors: dict[str, str] = {}
        if "description" in normalized:
            try:
                normalized["description"] = self._validate_description(normalized["description"], "description")
            except TaskValidationError as exc:
                errors.update(exc.field_errors)
        if "urgency" in normalized and normalized["urgency"] not in VALID_URGENCIES:
            errors["urgency"] = "Urgency must be low, medium, or high."
        if "complexity" in normalized and normalized["complexity"] is not None and normalized["complexity"] not in VALID_COMPLEXITIES:
            errors["complexity"] = "Complexity must be simple, moderate, complex, or null."
        for field, choices in (
            ("task_kind", _TASK_KINDS),
            ("density", _DENSITIES),
            ("creation_effort", _CREATION_EFFORTS),
            ("user_involvement", _USER_INVOLVEMENTS),
            ("required_contexts_source", _CONTEXT_SOURCES),
        ):
            if field in normalized and normalized[field] is not None and normalized[field] not in choices:
                errors[field] = "Unsupported value."
        for field in ("due_date", "deadline_date"):
            if field in normalized:
                try:
                    normalized[field] = self._validate_temporal(field, normalized[field])
                except TaskValidationError as exc:
                    errors.update(exc.field_errors)
        for field in ("agent_required_contexts", "user_required_contexts"):
            if field in normalized:
                try:
                    normalized[field] = self._json_array(normalized[field])
                except (TypeError, ValueError):
                    errors[field] = "Contexts must be a list of strings."
        for field in (
            "summary_text",
            "outcome_text",
            "next_action_text",
            "definition_of_done",
        ):
            if field not in normalized or normalized[field] is None:
                continue
            if not isinstance(normalized[field], str):
                errors[field] = "Use text or null."
            else:
                normalized[field] = normalized[field].strip() or None
        if "dependencies" in normalized:
            try:
                normalized["dependencies_json"] = self._json_array(
                    normalized.pop("dependencies")
                )
            except (TypeError, ValueError):
                errors["dependencies"] = "Dependencies must be a list of strings."
        if "has_dependency" in normalized:
            normalized["has_dependency"] = int(bool(normalized["has_dependency"]))
        if errors:
            raise TaskValidationError(errors)
        return normalized

    def _validate_action_changes(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "description",
            "state",
            "risk_profile_json",
            "agent_required_contexts",
            "user_required_contexts",
            "definition_of_done",
            "authorship",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise TaskValidationError({key: "Unsupported action-item field." for key in sorted(unknown)})
        result = dict(changes)
        errors: dict[str, str] = {}
        if "description" in result:
            try:
                result["description"] = self._validate_description(
                    result["description"], "description"
                )
            except TaskValidationError as exc:
                errors.update(exc.field_errors)
        if "state" in result and result["state"] not in _ACTION_STATES:
            errors["state"] = "Unsupported action-item state."
        if "authorship" in result and result["authorship"] not in _AUTHORSHIP:
            errors["authorship"] = "Unsupported action-item authorship."
        for field in ("agent_required_contexts", "user_required_contexts"):
            if field in result:
                try:
                    result[field] = self._json_array(result[field])
                except (TypeError, ValueError):
                    errors[field] = "Contexts must be a list of non-empty strings."
        if "definition_of_done" in result:
            value = result["definition_of_done"]
            if value is not None and not isinstance(value, str):
                errors["definition_of_done"] = "Use text or null."
            elif isinstance(value, str):
                result["definition_of_done"] = value.strip() or None
        if "risk_profile_json" in result:
            value = result["risk_profile_json"]
            if value is not None and not isinstance(value, str):
                errors["risk_profile_json"] = "Use a JSON object string or null."
            elif isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    errors["risk_profile_json"] = "Use a valid JSON object string."
                else:
                    if not isinstance(parsed, dict):
                        errors["risk_profile_json"] = "Risk profile must be a JSON object."
                    else:
                        result["risk_profile_json"] = json.dumps(
                            parsed,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
        if errors:
            raise TaskValidationError(errors)
        return result

    @staticmethod
    def _validate_description(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TaskValidationError({field: "A non-empty description is required."})
        normalized = value.strip()
        if len(normalized) > 10_000:
            raise TaskValidationError({field: "Description exceeds 10,000 characters."})
        return normalized

    @staticmethod
    def _validate_temporal(field: str, value: Any, *, allow_datetime: bool = False) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise TaskValidationError({field: "Use an ISO-8601 date."})
        candidate = value.strip()
        try:
            if allow_datetime and ("T" in candidate or " " in candidate):
                datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            else:
                date.fromisoformat(candidate)
        except ValueError as exc:
            raise TaskValidationError({field: "Use an ISO-8601 date or timestamp."}) from exc
        return candidate

    @staticmethod
    def _normalize_tags(tags: Iterable[str | Tag | tuple[str, bool]]) -> tuple[Tag, ...]:
        normalized: dict[str, Tag] = {}
        errors: dict[str, str] = {}
        for index, raw in enumerate(tags):
            if isinstance(raw, Tag):
                name, is_namespace = raw.name, raw.is_namespace
            elif isinstance(raw, tuple) and len(raw) == 2:
                name, is_namespace = raw[0], bool(raw[1])
            else:
                name, is_namespace = raw, False
            if not isinstance(name, str):
                errors[f"tags.{index}"] = "Tags must be strings."
                continue
            clean = name.strip().lstrip("#").strip().casefold()
            if not clean or not _TAG_RE.fullmatch(clean):
                errors[f"tags.{index}"] = "Use letters, digits, '-', '_', and '/' only."
                continue
            if clean not in normalized:
                normalized[clean] = Tag(clean, bool(is_namespace))
            elif is_namespace:
                normalized[clean] = Tag(clean, True)
        if errors:
            raise TaskValidationError(errors)
        return tuple(sorted(normalized.values(), key=lambda item: item.name))

    @staticmethod
    def _json_array(values: Sequence[str]) -> str:
        if isinstance(values, (str, bytes)):
            raise TypeError("expected sequence, not string")
        result = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("contexts must be non-empty strings")
            result.append(value.strip())
        return json.dumps(result, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise TaskValidationError({"task_id": "Task ID has an unsupported format."})

    @staticmethod
    def _validate_authority(client_mutation_id: str, actor: str) -> None:
        errors: dict[str, str] = {}
        if not isinstance(client_mutation_id, str) or not client_mutation_id.strip():
            errors["client_mutation_id"] = "A client mutation ID is required."
        elif len(client_mutation_id) > 200:
            errors["client_mutation_id"] = "Client mutation ID is too long."
        if not isinstance(actor, str) or not actor.strip():
            errors["actor"] = "An authenticated actor is required."
        if errors:
            raise TaskValidationError(errors)

    @staticmethod
    def _authority_revision(authority: Mapping[str, Any]) -> int:
        if "expected_revision" not in authority:
            raise TaskValidationError({"expected_revision": "Expected revision is required."})
        try:
            revision = int(authority["expected_revision"])
        except (TypeError, ValueError) as exc:
            raise TaskValidationError({"expected_revision": "Expected revision must be a positive integer."}) from exc
        if revision < 1:
            raise TaskValidationError({"expected_revision": "Expected revision must be positive."})
        return revision

    @staticmethod
    def _last_actor(actor: str) -> str:
        lowered = actor.casefold()
        return "user" if "user" in lowered or lowered.startswith("dashboard") else "agent"

    def _now(self) -> str:
        value = self._clock()
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    @classmethod
    def _request_hash(cls, mutation: str, request: Mapping[str, Any]) -> str:
        body = cls._canonical_json({"mutation": mutation, "request": request})
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
