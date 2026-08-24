"""Typed records for the SQLite-authoritative task domain.

These types intentionally depend only on the Python standard library.  They
are safe to import when every vault, bridge, and Markdown integration is
absent.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence


TaskState = Literal[
    "inbox", "mit", "focused", "active", "waiting", "snoozed", "done"
]
TaskUrgency = Literal["low", "medium", "high"]
AttentionState = Literal["inbox", "mit", "focused", "active", "waiting"]

VALID_TASK_STATES = frozenset(
    {"inbox", "mit", "focused", "active", "waiting", "snoozed", "done"}
)
VALID_ATTENTION_STATES = frozenset({"inbox", "mit", "focused", "active", "waiting"})
VALID_URGENCIES = frozenset({"low", "medium", "high"})
VALID_COMPLEXITIES = frozenset({"simple", "moderate", "complex"})


def _optional_json_array(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded)


@dataclass(frozen=True, slots=True)
class Tag:
    name: str
    is_namespace: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "is_namespace": self.is_namespace}


@dataclass(frozen=True, slots=True)
class TaskActionItem:
    id: int
    task_id: str
    sequence: int
    description: str
    state: str
    authorship: str
    created_at: str
    updated_at: str
    risk_profile_json: str | None = None
    agent_required_contexts: tuple[str, ...] = ()
    user_required_contexts: tuple[str, ...] = ()
    definition_of_done: str | None = None
    completed_at: str | None = None
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskActionItem":
        values = dict(row)
        return cls(
            id=int(values["id"]),
            task_id=str(values["task_id"]),
            sequence=int(values["sequence"]),
            description=str(values["description"]),
            state=str(values["state"]),
            authorship=str(values.get("authorship") or "agent_unapproved"),
            created_at=str(values["created_at"]),
            updated_at=str(values["updated_at"]),
            risk_profile_json=values.get("risk_profile_json"),
            agent_required_contexts=_optional_json_array(values.get("agent_required_contexts")),
            user_required_contexts=_optional_json_array(values.get("user_required_contexts")),
            definition_of_done=values.get("definition_of_done"),
            completed_at=values.get("completed_at"),
            deleted_at=values.get("deleted_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["agent_required_contexts"] = list(self.agent_required_contexts)
        value["user_required_contexts"] = list(self.user_required_contexts)
        return value


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    description: str
    state: str
    urgency: str
    revision: int
    created_at: str
    updated_at: str
    complexity: str | None = None
    contract: str | None = None
    note_uuid: str | None = None
    due_date: str | None = None
    deadline_date: str | None = None
    has_deadline: bool = False
    snooze_until: str | None = None
    snooze_resume_state: str | None = None
    completed_at: str | None = None
    archived_at: str | None = None
    deleted_at: str | None = None
    restored_at: str | None = None
    task_kind: str = "task"
    density: str = "sparse"
    outcome_text: str | None = None
    summary_text: str | None = None
    next_action_text: str | None = None
    definition_of_done: str | None = None
    creation_effort: str = "developed"
    user_involvement: str = "high"
    creation_provenance: str = "manual"
    has_dependency: bool = False
    dependency_hint: str | None = None
    risk_profile_json: str | None = None
    automation_tier_achievable: int | None = None
    last_actor: str | None = None
    agent_required_contexts: tuple[str, ...] = ()
    user_required_contexts: tuple[str, ...] = ()
    required_contexts_source: str | None = None
    current_action_item_id: int | None = None
    created_by_session: str | None = None
    legacy_import_receipt_id: str | None = None
    dependencies: tuple[str, ...] = ()
    tags: tuple[Tag, ...] = ()
    action_items: tuple[TaskActionItem, ...] = ()

    @classmethod
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        tags: Sequence[Tag] = (),
        action_items: Sequence[TaskActionItem] = (),
    ) -> "Task":
        values = dict(row)
        return cls(
            task_id=str(values["task_id"]),
            description=str(values.get("description") or ""),
            state=str(values.get("state") or "inbox"),
            urgency=str(values.get("urgency") or "medium"),
            revision=int(values.get("revision") or 1),
            created_at=str(values["created_at"]),
            updated_at=str(values["updated_at"]),
            complexity=values.get("complexity"),
            contract=values.get("contract"),
            note_uuid=values.get("note_uuid"),
            due_date=values.get("due_date"),
            deadline_date=values.get("deadline_date"),
            has_deadline=bool(values.get("has_deadline")),
            snooze_until=values.get("snooze_until"),
            snooze_resume_state=values.get("snooze_resume_state"),
            completed_at=values.get("completed_at"),
            archived_at=values.get("archived_at"),
            deleted_at=values.get("deleted_at"),
            restored_at=values.get("restored_at"),
            task_kind=str(values.get("task_kind") or "task"),
            density=str(values.get("density") or "sparse"),
            outcome_text=values.get("outcome_text"),
            summary_text=values.get("summary_text"),
            next_action_text=values.get("next_action_text"),
            definition_of_done=values.get("definition_of_done"),
            creation_effort=str(values.get("creation_effort") or "developed"),
            user_involvement=str(values.get("user_involvement") or "high"),
            creation_provenance=str(values.get("creation_provenance") or "manual"),
            has_dependency=bool(values.get("has_dependency")),
            dependency_hint=values.get("dependency_hint"),
            risk_profile_json=values.get("risk_profile_json"),
            automation_tier_achievable=values.get("automation_tier_achievable"),
            last_actor=values.get("last_actor"),
            agent_required_contexts=_optional_json_array(values.get("agent_required_contexts")),
            user_required_contexts=_optional_json_array(values.get("user_required_contexts")),
            required_contexts_source=values.get("required_contexts_source"),
            current_action_item_id=values.get("current_action_item_id"),
            created_by_session=values.get("created_by_session"),
            legacy_import_receipt_id=values.get("legacy_import_receipt_id"),
            dependencies=_optional_json_array(values.get("dependencies_json")),
            tags=tuple(tags),
            action_items=tuple(action_items),
        )

    @property
    def namespace_tags(self) -> tuple[str, ...]:
        return tuple(tag.name for tag in self.tags if tag.is_namespace)

    @property
    def project(self) -> str | None:
        for tag in self.tags:
            if tag.name.casefold().startswith("projects/"):
                remainder = tag.name.split("/", 1)[1]
                return remainder.split("/", 1)[0] or None
        return None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tags"] = [tag.name for tag in self.tags]
        result["tag_records"] = [tag.to_dict() for tag in self.tags]
        result["action_items"] = [item.to_dict() for item in self.action_items]
        result["namespace_tags"] = list(self.namespace_tags)
        result["project"] = self.project
        result["agent_required_contexts"] = list(self.agent_required_contexts)
        result["user_required_contexts"] = list(self.user_required_contexts)
        result["completed"] = self.state == "done"
        result["summary"] = self.summary_text
        result["desired_outcome"] = self.outcome_text
        result["next_action"] = self.next_action_text
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Task":
        raw_task = dict(value)
        tag_records = raw_task.pop("tag_records", None)
        raw_tags = raw_task.pop("tags", ())
        raw_action_items = raw_task.pop("action_items", ())
        for derived in (
            "namespace_tags",
            "project",
            "completed",
            "summary",
            "desired_outcome",
            "next_action",
        ):
            raw_task.pop(derived, None)
        tags = (
            tuple(Tag(str(item["name"]), bool(item.get("is_namespace"))) for item in tag_records)
            if tag_records
            else tuple(Tag(str(item)) for item in raw_tags)
        )
        raw_task["tags"] = tags
        raw_task["action_items"] = tuple(
            TaskActionItem(
                **{
                    **dict(item),
                    "agent_required_contexts": tuple(item.get("agent_required_contexts") or ()),
                    "user_required_contexts": tuple(item.get("user_required_contexts") or ()),
                }
            )
            for item in raw_action_items
        )
        raw_task["agent_required_contexts"] = tuple(raw_task.get("agent_required_contexts") or ())
        raw_task["user_required_contexts"] = tuple(raw_task.get("user_required_contexts") or ())
        raw_task["dependencies"] = tuple(raw_task.get("dependencies") or ())
        return cls(**raw_task)


@dataclass(frozen=True, slots=True)
class TaskQuery:
    state: str | None = None
    urgency: str | None = None
    project: str | None = None
    namespace: str | None = None
    due: str | None = None
    text: str | None = None
    include_done: bool = False
    include_archived: bool = False
    include_deleted: bool = False
    include_snoozed: bool = False
    limit: int = 500
    offset: int = 0


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    receipt_id: str
    client_mutation_id: str
    actor: str
    session_id: str | None
    mutation: str
    request_hash: str
    status: str
    created_at: str
    completed_at: str | None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MutationReceipt":
        return cls(**{name: value.get(name) for name in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MutationResult:
    task: Task
    collection_revision: int
    receipt: MutationReceipt
    changed: bool = True
    replayed: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, replayed: bool = False) -> "MutationResult":
        return cls(
            task=Task.from_dict(value["task"]),
            collection_revision=int(value["collection_revision"]),
            receipt=MutationReceipt.from_dict(value["receipt"]),
            changed=bool(value.get("changed", True)),
            replayed=replayed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "collection_revision": self.collection_revision,
            "receipt": self.receipt.to_dict(),
            "changed": self.changed,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class BatchMutationResult:
    tasks: tuple[Task, ...]
    collection_revision: int
    receipt: MutationReceipt
    replayed: bool = False

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        replayed: bool = False,
    ) -> "BatchMutationResult":
        return cls(
            tasks=tuple(Task.from_dict(item) for item in value["tasks"]),
            collection_revision=int(value["collection_revision"]),
            receipt=MutationReceipt.from_dict(value["receipt"]),
            replayed=replayed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [task.to_dict() for task in self.tasks],
            "collection_revision": self.collection_revision,
            "receipt": self.receipt.to_dict(),
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class TaskHistoryEntry:
    id: int
    task_id: str
    old_state: str | None
    new_state: str
    changed_at: str
    reason: str | None
    mutation: str | None
    actor: str | None
    session_id: str | None
    receipt_id: str | None
    task_revision: int | None
    collection_revision: int | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskDocumentLink:
    task_id: str
    note_uuid: str
    store_id: str
    document_id: str
    binding_id: str
    lifecycle: str
    created_at: str
    updated_at: str
    retired_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskSystemState:
    authority_epoch: str
    cowork_task_store_id: str | None
    cutover_receipt_id: str | None
    rollback_fence: bool
    process_generation: int
    updated_at: str
