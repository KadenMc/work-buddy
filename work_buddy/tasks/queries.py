"""Read-only query facade shared by dashboard, MCP, and integrations."""

from __future__ import annotations

from .models import Task, TaskHistoryEntry, TaskQuery, TaskSystemState
from .store import TaskStore


class TaskQueryService:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

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

    def history(self, task_id: str) -> list[TaskHistoryEntry]:
        return self.store.history(task_id)

    def collection_revision(self) -> int:
        return self.store.collection_revision()

    def pending_outbox(self, *, limit: int = 100) -> list[dict]:
        return self.store.pending_outbox(limit=limit)

    def system_state(self) -> TaskSystemState:
        return self.store.system_state()
