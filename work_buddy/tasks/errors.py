"""Domain errors for the neutral task application boundary."""

from __future__ import annotations

from typing import Any


class TaskDomainError(RuntimeError):
    """Base class carrying a stable machine-readable error code."""

    code = "task_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        field_errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": dict(self.field_errors),
            "retryable": self.retryable,
        }


class TaskNotFound(TaskDomainError):
    code = "task_not_found"

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task {task_id!r} does not exist.")


class TaskValidationError(TaskDomainError):
    code = "task_validation_error"

    def __init__(self, field_errors: dict[str, str]) -> None:
        super().__init__("One or more task fields are invalid.", field_errors=field_errors)


class TaskRevisionConflict(TaskDomainError):
    code = "task_revision_conflict"

    def __init__(self, *, expected: int, current: int, current_task: dict[str, Any]) -> None:
        self.expected_revision = expected
        self.current_revision = current
        self.current_task = current_task
        super().__init__("This task changed while you were editing it.")

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            current_revision=self.current_revision,
            current_task=self.current_task,
        )
        return result


class TaskIdempotencyConflict(TaskDomainError):
    code = "task_idempotency_conflict"

    def __init__(self, client_mutation_id: str) -> None:
        self.client_mutation_id = client_mutation_id
        super().__init__(
            "That client mutation ID was already used for a different request."
        )


class TaskTransitionError(TaskDomainError):
    code = "task_invalid_transition"


class TaskDeletedError(TaskTransitionError):
    code = "task_deleted"

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task {task_id!r} is in Trash; restore it before editing.")


class TaskMutationFenced(TaskDomainError):
    """Raised while the migration/cutover maintenance fence is armed."""

    code = "task_mutation_fenced"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Task editing is temporarily paused for maintenance.")


class TaskAuthorityUnavailable(TaskDomainError):
    """The authority ledger exists but cannot be read safely."""

    code = "task_authority_unavailable"
    retryable = True

    def __init__(self) -> None:
        super().__init__(
            "Task editing is temporarily unavailable because setup could not be verified."
        )


class TaskReplayAuthorityMismatch(TaskDomainError):
    """A durable retry belongs to the other side of the cutover boundary."""

    code = "task_replay_authority_mismatch"

    def __init__(self) -> None:
        super().__init__(
            "This queued task update was created for an older task-system version "
            "and cannot be applied safely."
        )


class TaskLegacyEffectRetired(TaskDomainError):
    """A native task operation surfaced a legacy Markdown-effect carrier."""

    code = "task_legacy_effect_retired"

    def __init__(self) -> None:
        super().__init__(
            "This task update uses an unsupported compatibility path and cannot "
            "be applied."
        )
