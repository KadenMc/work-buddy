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
from .creation import (
    FieldDerivation,
    PublishedTaskCreationDecision,
    TaskCreationCoordinator,
    TaskCreationDecisionVerificationError,
    TaskCreationIntent,
    TaskCreationIntentError,
    verify_published_task_creation_decision,
)
from .aggregate_creation import (
    TaskAggregateCreationService,
    reconcile_task_creation_intents,
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
    "FieldDerivation",
    "PublishedTaskCreationDecision",
    "MutationResult",
    "Tag",
    "Task",
    "TaskActionItem",
    "TaskApplicationService",
    "TaskAggregateCreationService",
    "TaskCreationCoordinator",
    "TaskCreationDecisionVerificationError",
    "TaskCreationIntent",
    "TaskCreationIntentError",
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
    "reconcile_task_creation_intents",
    "verify_published_task_creation_decision",
]
