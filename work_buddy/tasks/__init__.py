"""Obsidian-free task domain.

The neutral package is the new task-row authority boundary.  Legacy adapters
may call it during rollout; it never calls back into a vault integration.
"""

from .errors import (
    TaskDeletedError,
    TaskDomainError,
    TaskIdempotencyConflict,
    TaskNotFound,
    TaskRevisionConflict,
    TaskTransitionError,
    TaskValidationError,
)
from .models import (
    BatchMutationResult,
    MutationReceipt,
    MutationResult,
    Tag,
    Task,
    TaskActionItem,
    TaskDocumentLink,
    TaskHistoryEntry,
    TaskQuery,
    TaskSystemState,
)
from .queries import TaskQueryService
from .service import TaskApplicationService
from .store import TaskStore, default_task_db_path

__all__ = [
    "MutationReceipt",
    "BatchMutationResult",
    "MutationResult",
    "Tag",
    "Task",
    "TaskActionItem",
    "TaskApplicationService",
    "TaskDeletedError",
    "TaskDocumentLink",
    "TaskDomainError",
    "TaskHistoryEntry",
    "TaskIdempotencyConflict",
    "TaskNotFound",
    "TaskQuery",
    "TaskQueryService",
    "TaskRevisionConflict",
    "TaskSystemState",
    "TaskTransitionError",
    "TaskValidationError",
    "TaskStore",
    "default_task_db_path",
]
